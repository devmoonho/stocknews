"""부처 보도자료 수집 — 11개 부처 누리집의 보도자료 목록·본문·첨부파일 링크·엠바고(보도 시점).

부처마다 페이지 구조가 달라 부처별 파서(list + detail)를 따로 둔다. 공통 결과 형식:
  목록 항목 {id, title, date(date), dept, url, posted_at(datetime|None), attachments[{name,url}], body}
  상세     {body, posted_at, attachments, dept}

게시 시각 (press_meta.time_basis / time_source = rss | page | snapshot | date | embargo)
  - rss      : 부처 RSS의 시각(시·분까지 — 국토부·식약처·복지부·해수부·기후부). 가장 우선
  - page     : 누리집이 시각까지 보여 줌(국토부·복지부 상세, 원안위 목록 JSON)
  - snapshot : 날짜만 있는 부처 — 그날 15:30 스냅샷(`--snapshot`, cron)에 없던 글 = 15:30 이후 게시.
               published_at은 하한(그날 15:30)으로 둔다.
  - before   : 15:30 스냅샷에 이미 있던 글 = 장중 게시 → 시간대 밖(그날 00:00으로 저장)
  - date     : 시간대 시작일보다 뒤 날짜(밤사이·주말 게시) → 그날 00:00(시간대 안)
  - unknown  : 시작일 글인데 스냅샷이 없어 판단 불가 → 엠바고 시각이 15:30 이후면 그 시각, 아니면 제외
엠바고: 제목·첨부파일명·본문 앞부분의 '보도시점 11.14.(토) 06:00', '13(금) 18시엠바고', '즉시', '조간/석간' 표기.
robots.txt(2026-11-14 확인): 11곳 모두 보도자료 목록·상세 허용(복지부는 이 게시판만 Allow, 나머지 /board.es 차단).
요청 간격 0.7초, 부처당 목록 최대 MAX_PAGES쪽.
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import db
from ..health import Warnings
from ..http import Fetcher
from ..timewindow import Window, now_kst

SOURCE = "ministry_press"
MAX_PAGES = 6
BUDGET_SEC = 120          # 부처당 시간 상한 — 한 누리집이 느려도 아침·저녁 수집 전체가 밀리지 않게
SNAPSHOT_TIME = "15:30"
INTRADAY_FROM = dt.time(7, 0)    # 아침 리포트(07:00) 뒤: 07:00~08:00 '장 전', 08:00~15:30 '장중'
EXACT_BASES = ("rss", "page")    # 시각을 정확히 아는 출처
TIME_SOURCE = {"rss": "rss", "page": "page", "snapshot": "snapshot", "before": "snapshot", "date": "date",
               "embargo": "embargo", "unknown": "unknown"}

# ---------------------------------------------------------------- 공통 도우미

_WS = re.compile(r"[ \t\r\f\v]+")


def clean(s: str | None) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>|</p>|</tr>|</li>|</h\d>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(html.unescape(s)).replace("\xa0", " ").replace("​", "")
    lines = [_WS.sub(" ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def between(s: str, start: str, end: str | None = None, maxlen: int = 80000) -> str:
    """start 정규식 뒤 ~ end 정규식 앞 (없으면 '')."""
    m = re.search(start, s, re.S)
    if not m:
        return ""
    rest = s[m.end(): m.end() + maxlen]
    if end:
        e = re.search(end, rest, re.S)
        if e:
            rest = rest[: e.start()]
    return rest


def parse_date(s: str) -> dt.date | None:
    s = s.strip()
    m = re.search(r"(20\d\d)\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (20\d\d)", s)       # 'Nov 13, 2026' (과기정통부)
    if m:
        return dt.datetime.strptime(m.group(0), "%b %d, %Y").date()
    return None


def parse_dt(s: str) -> dt.datetime | None:
    m = re.search(r"(20\d\d)\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", s or "")
    if not m:
        return None
    return dt.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))


def absurl(base: str, href: str) -> str:
    href = html.unescape(href.strip())
    href = re.sub(r";jsessionid=[^?#]*", "", href)
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return base + "/" + href.lstrip("./")


def _fname(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    for _ in range(2):
        name = re.sub(r"\s*(?:다운로드|파일다운로드|\[[\d.,]+\s*[KMG]?B\]|\(\s*[\d.,]+\s*[KMG]?(?:B|byte)\s*\))\s*$", "",
                      name, flags=re.I).strip()
    return re.sub(r"^(?:pdf|한글|hwp|hwpx|워드|엑셀|이미지)\s*파일\s+", "", name, flags=re.I)


def links(seg: str, base: str, href_pat: str, name_from: str = "text") -> list[dict]:
    """첨부파일 링크: href가 href_pat에 맞는 <a>. 이름은 title(…다운로드 제거) 또는 링크 글자."""
    out, seen = [], set()
    for m in re.finditer(r'<a\b([^>]*?)href="([^"]*)"([^>]*)>(.*?)</a>', seg, re.S):
        attrs, href, attrs2, text = m.group(1) + m.group(3), m.group(2), m.group(3), m.group(4)
        if not re.search(href_pat, href):
            continue
        t = re.search(r'title="([^"]*)"', attrs)
        text = re.sub(r'<span class="(?:blind|hd-element|sr-only)">.*?</span>', "", text, flags=re.S)
        name = clean(t.group(1)) if (name_from == "title" and t) else clean(text)
        name = _fname(name)
        url = absurl(base, href)
        if url in seen or not name:
            continue
        seen.add(url)
        out.append({"name": name, "url": url})
    return out


# ---------------------------------------------------------------- 엠바고(보도 시점)

_DOW = r"\(?\s*[월화수목금토일]\s*\)?"
_EMB_PATTERNS = [
    # 보도시점 : 2026. 11. 14.(토) 06:00 / 보도일시 11.14.(토) 12:00 / 11. 14.(금) 12:00 이후 보도
    re.compile(r"(?:보도\s*시점|보도\s*일시|보도\s*가능\s*시점|엠바고)\s*[:：|]?\s*\|?\s*(?:(20\d\d)\s*\.\s*)?(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*"
               + _DOW + r"?\s*(\d{1,2})\s*(?::|시)\s*(\d{2})?"),
    re.compile(r"(?:(20\d\d)\s*\.\s*)?(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*" + _DOW + r"\s*(\d{1,2})\s*(?::|시)\s*(\d{2})?\s*(?:분\s*)?"
               r"(?:이후|부터)\s*(?:보도|게재|사용)"),
]
_EMB_DAYHOUR = re.compile(r"(\d{1,2})\s*" + _DOW + r"\s*(\d{1,2})\s*시\s*(?:이후\s*)?엠바고")   # 13(금) 18시엠바고 (산업부 첨부명)
_EMB_PAPER = re.compile(r"(?:(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*" + _DOW + r"?\s*)?(조간|석간)")
_EMB_NOW = re.compile(r"(?:보도\s*시점\s*[:：|]?\s*\|?\s*배포\s*시|배포\s*즉시|즉시\s*보도|^\d{6}\s*즉시|\b즉시\s*\()")


def _resolve(ref: dt.date, month: int | None, day: int, year: int | None = None) -> dt.date | None:
    """월이 없으면 기준일 근처의 그 '일'. 연도가 없으면 기준일 연도(12월→1월 넘김 보정)."""
    try:
        if month is None:
            cands = []
            for k in (-1, 0, 1):
                y, mth = ref.year, ref.month + k
                if mth < 1:
                    y, mth = y - 1, 12
                elif mth > 12:
                    y, mth = y + 1, 1
                try:
                    cands.append(dt.date(y, mth, day))
                except ValueError:
                    pass
            return min(cands, key=lambda d: abs((d - ref).days)) if cands else None
        y = year or ref.year
        d = dt.date(y, month, day)
        if not year and (d - ref).days < -200:
            d = dt.date(y + 1, month, day)
        return d
    except ValueError:
        return None


def parse_embargo(texts: list[str], ref: dt.date) -> tuple[str | None, dt.datetime | None]:
    """→ (표기 원문, 보도 가능 시각). 조간·석간·즉시는 시각 없이 표기만."""
    for t in texts:
        if not t:
            continue
        t = t[:1500]
        for p in _EMB_PATTERNS:
            m = p.search(t)
            if m:
                d = _resolve(ref, int(m[2]), int(m[3]), int(m[1]) if m[1] else None)
                if d:
                    hh, mm = int(m[4]), int(m[5] or 0)
                    if 0 <= hh <= 24 and 0 <= mm < 60:
                        at = dt.datetime.combine(d, dt.time(0)) + dt.timedelta(hours=hh, minutes=mm)
                        return re.sub(r"\s+", " ", m.group(0)).strip(), at
        m = _EMB_DAYHOUR.search(t)
        if m:
            d = _resolve(ref, None, int(m[1]))
            if d and 0 <= int(m[2]) <= 24:
                return re.sub(r"\s+", " ", m.group(0)).strip(), dt.datetime.combine(d, dt.time(0)) + dt.timedelta(hours=int(m[2]))
    for t in texts:
        if not t:
            continue
        m = _EMB_PAPER.search(t[:600])
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip(), None
        if _EMB_NOW.search(t[:300]):
            return "즉시", None
    return None, None


# ---------------------------------------------------------------- 부처별 파서


@dataclass
class Ministry:
    key: str
    name: str                       # 리포트 표시명([정책:부처명])
    base: str
    list_url: str
    parse_list: Callable[[str], list[dict]]
    page_args: Callable[[int], dict]
    detail: Callable[[Fetcher, dict], dict] | None = None
    fetch_list: Callable[[Fetcher, "Ministry", int], str] | None = None
    cookies: bool = False
    need_detail_for_time: bool = False      # 상세에만 시각이 있음 → 시작일 글은 상세를 봐야 판단
    rss_url: str | None = None              # 보도자료 RSS (시각이 나오면 15:30 스냅샷·상세보다 우선)
    rss_post: bool = False                  # 산업부: RSS를 POST로만 준다
    rss_id: str | None = None               # RSS link에서 글 번호를 뽑는 정규식(그룹 1)
    rss_exact: bool = False                 # RSS 시각이 시·분까지 나오나 (2026-11-14 확인)
    notes: str = ""
    extra: dict = field(default_factory=dict)


def _get(f: Fetcher, url: str, params: dict | None = None, referer: str | None = None, **kw) -> str:
    return f.get_text(url, params, referer=referer, **kw)


# 1) 산업통상부(산업부) — 목록 표, 첨부는 목록에도 있음. 상세 .detail-cont
def _motir_list(page: str) -> list[dict]:
    out = []
    body = between(page, r"<tbody>", r"</tbody>")
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        m = re.search(r'href="(/kor/article/ATCL3f49a5a8c/(\d+)/view)[^"]*"[^>]*>\s*<i>(.*?)</i>', tr, re.S)
        if not m:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        date = next((parse_date(clean(x)) for x in tds if re.fullmatch(r"\s*20\d\d-\d\d-\d\d\s*", clean(x))), None)
        dept = clean(re.search(r"<td><i>(.*?)</i></td>", tr, re.S).group(1)) if re.search(r"<td><i>(.*?)</i></td>", tr, re.S) else ""
        cat = re.search(r'badge-category"><i>(.*?)</i>', tr)
        out.append({"id": m[2], "title": clean(m[3]), "date": date, "dept": dept,
                    "url": f"https://www.motir.go.kr{m[1]}", "category": clean(cat.group(1)) if cat else "",
                    "attachments": links(tr, "https://www.motir.go.kr", r"/attach/down/", "title")})
    return out


def _motir_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["motir"].list_url)
    body = clean(between(p, r'class="detail-cont[^"]*">', r'<div class="(?:btn-wrap|board-btn|view-btn|article-nav|prev-next)|<ul class="(?:prev|board-nav)'))
    info = between(p, r'class="info-down"', r"</li>")
    return {"body": body, "attachments": links(info, "https://www.motir.go.kr", r"/attach/down/", "title")}


# 2) 과학기술정보통신부 — 목록 날짜는 JS(td_REG_DT_n), 본문은 첨부 .md(본문 마크다운)가 가장 충실
def _msit_list(page: str) -> list[dict]:
    dates = {m[1]: parse_date(m[2]) for m in re.finditer(r"\$\('#td_'\+'REG_DT'\+'_(\d+)'\)\.html\('([^']+)'\)", page)}
    depts = {m[1]: m[2] for m in re.finditer(r"\$\('#td_'\+'CHRG_DEPT_NM'\+'_(\d+)'\)\.html\('([^']*)'\)", page)}
    out = []
    for m in re.finditer(r'href="(/bbs/view\.do\?[^"]*nttSeqNo=(\d+))"[^>]*>.*?id="td_NTT_SJ_(\d+)"\s*>(.*?)</p>', page, re.S):
        idx = m[3]
        out.append({"id": m[2], "title": clean(m[4]), "date": dates.get(idx), "dept": clean(depts.get(idx, "")),
                    "url": "https://www.msit.go.kr" + html.unescape(m[1])})
    return out


def _msit_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["msit"].list_url)
    dept = re.search(r'부서</dt>\s*<dd class="con">(.*?)</dd>', p, re.S)
    files = []
    for m in re.finditer(r'href="(/ssm/file/fileDown\.do\?atchFileNo=\d+&amp;fileOrd=\d+)"[^>]*>\s*<span class="blind">(.*?)</span>', p, re.S):
        files.append({"name": clean(m[2]), "url": absurl("https://www.msit.go.kr", m[1])})
    body = clean(between(p, r'<div class="board_notcon">', r"</div>"))
    md = next((x for x in files if x["name"].lower().endswith(".md")), None)
    if md:                                   # 보도자료 본문 마크다운(첨부) — 1회 더 요청
        try:
            txt = f.get(md["url"], referer=it["url"]).decode("utf-8", "replace")
            if txt.strip() and "<html" not in txt[:200].lower():
                body = "\n".join(ln for ln in txt.splitlines() if not ln.startswith("> ")).strip()
        except Exception:  # noqa: BLE001 — 본문 보강 실패는 무시(목록 본문 유지)
            pass
    return {"body": body, "attachments": files, "dept": clean(dept.group(1)) if dept else ""}


# 3) 국토교통부 — 쿠키 필요(307→쿠키→200). 상세에 '등록일 YYYY-MM-DD HH:MM'
def _molit_list(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", between(page, r"<tbody>", r"</tbody>"), re.S):
        m = re.search(r'href="dtl\.jsp\?([^"]*?id=(\d+))"[^>]*>(.*?)</a>', tr, re.S)
        d = re.search(r'class="bd_date">(.*?)</td>', tr)
        if not m:
            continue
        field_ = re.search(r'class="bd_field">(.*?)</td>', tr)
        out.append({"id": m[2], "title": clean(re.sub(r"<i>.*?</i>", "", m[3], flags=re.S)),
                    "date": parse_date(d.group(1)) if d else None, "dept": "", "category": clean(field_.group(1)) if field_ else "",
                    "url": f"https://www.molit.go.kr/USR/NEWS/m_71/dtl.jsp?id={m[2]}"})
    return out


def _molit_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["molit"].list_url)
    reg = re.search(r"등록일</strong>\s*<span>(.*?)</span>", p)
    dept = re.search(r"담당부서</strong>\s*<span>(.*?)</span>", p, re.S)
    files_seg = between(p, r'<li class="file">', r"</li>")
    # 본문은 문서 뷰어(iframe, JS 렌더)라 HTML로는 못 읽는다 → 제목·첨부명만. 본문은 정책브리핑 중복 기사에서 보강.
    seg = between(p, r'<div class="bd_view_cont">', r"</div>")
    body = "" if "<iframe" in seg else clean(seg)
    return {"body": body, "posted_at": parse_dt(reg.group(1)) if reg else None,
            "dept": clean(dept.group(1)) if dept else "",
            "attachments": links(files_seg, "https://www.molit.go.kr", r"Download")}


# 4) 방위사업청 — fn_selectDoc('번호') → selectDoc.do
def _dapa_list(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", between(page, r"<tbody>", r"</tbody>"), re.S):
        m = re.search(r"fn_selectDoc\('(\d+)'\).*?<p class=\"text\">(.*?)</p>", tr, re.S)
        if not m:
            continue
        d = next((parse_date(x) for x in re.findall(r"<td>\s*(20\d\d-\d\d-\d\d)\s*</td>", tr)), None)
        out.append({"id": m[1], "title": clean(m[2]), "date": d, "dept": "",
                    "url": f"https://www.dapa.go.kr/dapa/doc/selectDoc.do?docSeq={m[1]}&menuSeq=3069&bbsSeq=326"})
    return out


def _dapa_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["dapa"].list_url)
    info = clean(between(p, r'<ul class="view-info', r"</ul>"))
    dept = re.search(r"(?:담당부서|부서)\s*\n?\s*([^\n]+)", info)
    return {"body": clean(between(p, r'<div class="view-cont">', r'<ul class="view-file">|<div class="view-btn-wrap">')),
            "dept": dept.group(1).strip() if dept else "",
            "attachments": links(between(p, r'<ul class="view-file">', r"</ul>"), "https://www.dapa.go.kr", r"[Dd]own")}


# 5) 식품의약품안전처 — 목록에 첨부명([26.11.13.(금) 보도참고자료] …)과 날짜
def _mfds_list(page: str) -> list[dict]:
    out = []
    for li in re.split(r"<li>\s*<div class=\"num\">", page)[1:]:
        m = re.search(r'href="\./view\.do\?seq=(\d+)[^"]*"[^>]*>(.*?)</a>', li, re.S)
        d = re.search(r'class="right_column">\s*(.*?)\s*</div>', li)
        if not m:
            continue
        dept = re.search(r"담당부서\s*\|\s*(.*?)\s*</p>", li, re.S)
        files = [{"name": clean(n), "url": ""} for n in re.findall(r'<div class="bbs_file_list_header">\s*<span>(.*?)</span>', li, re.S)]
        out.append({"id": m[1], "title": clean(m[2]), "date": parse_date(d.group(1)) if d else None,
                    "dept": clean(dept.group(1)) if dept else "",
                    "url": f"https://www.mfds.go.kr/brd/m_99/view.do?seq={m[1]}", "attachments": files})
    return out


def _mfds_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["mfds"].list_url)
    files = []
    for m in re.finditer(r"<strong>([^<]+)</strong>\s*<a href=\"(\./down\.do\?[^\"]+)\"", p):
        files.append({"name": clean(m[1]), "url": "https://www.mfds.go.kr/brd/m_99/" + html.unescape(m[2])[2:]})
    body = clean(between(p, r'<div class="bv_cont">|<div class="bbs_view_cont">|<div class="view_cont">', r"<!-- s: 첨부파일|<div class=\"bbs_file"))
    return {"body": body, "attachments": files}


# 6) 보건복지부 — 상세에 '작성일 YYYY-MM-DD HH:MM'. robots: 이 게시판(mid=a10503010100&bid=0027)만 허용
def _mohw_list(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", between(page, r"<tbody>", r"</tbody>"), re.S):
        m = re.search(r'href="/board\.es\?mid=a10503010100&amp;bid=0027&amp;act=view&amp;list_no=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        d = re.search(r'data-label="등록일">(.*?)</td>', tr)
        dept = re.search(r'data-label="담당부서">(.*?)</td>', tr, re.S)
        title = clean(re.sub(r'<span class="sr_only">.*?</span>', "", m[2]))
        out.append({"id": m[1], "title": title, "date": parse_date(d.group(1)) if d else None,
                    "dept": clean(dept.group(1)) if dept else "",
                    "url": f"https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027&act=view&list_no={m[1]}"})
    return out


def _mohw_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["mohw"].list_url)
    reg = re.search(r"작성일</strong>.*?<span>(20[^<]+)</span>", p, re.S)
    body = clean(between(p, r'<div class="viewArea">', r'<div class="(?:list_btn|btn_area|view_navi|file)|<ul class="(?:file|view_navi)'))
    files = [{"name": _fname(clean(m[2])), "url": absurl("https://www.mohw.go.kr", m[1])}
             for m in re.finditer(r'href="(/boardDownload\.es\?[^"]*)"[^>]*title="([^"]*)"', p)]
    return {"body": body, "posted_at": parse_dt(reg.group(1)) if reg else None, "attachments": files}


# 7) 해양수산부 — fn_selectDoc('번호'), 첨부는 목록에도 있음
def _mof_list(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", between(page, r"<tbody>", r"</tbody>"), re.S):
        m = re.search(r"fn_selectDoc\('(\d+)'\)\"\s*title=\"\[게시글 바로가기\]\s*(.*?)\"", tr, re.S)
        if not m:
            continue
        d = re.search(r'class="t-date">(.*?)</td>', tr)
        dept = re.search(r'class="ts">(.*?)</td>', tr, re.S)
        out.append({"id": m[1], "title": clean(m[2]), "date": parse_date(d.group(1)) if d else None,
                    "dept": clean(dept.group(1)) if dept else "",
                    "url": f"https://www.mof.go.kr/doc/ko/selectDoc.do?docSeq={m[1]}&menuSeq=971&bbsSeq=10",
                    "attachments": links(tr, "https://www.mof.go.kr", r"readDownloadFile")})
    return out


def _mof_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["mof"].list_url)
    return {"body": clean(between(p, r'<div class="bod-detail-view">', r'<div class="(?:bod-btn|btn-box|bod-detail-footer|prev-next)|<ul class="prev')),
            "attachments": links(between(p, r'<ul class="attach-list">', r"</ul>"), "https://www.mof.go.kr", r"readDownloadFile")}


# 8) 기후에너지환경부 — 표 목록, read.do?boardId=
def _mcee_list(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", between(page, r"<tbody>", r"</tbody>"), re.S):
        m = re.search(r'<a title="([^"]*)" href="(/home/web/board/read\.do[^"]*?boardId=(\d+)[^"]*)"', tr, re.S)
        if not m:
            continue
        tds = [clean(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        d = next((parse_date(x) for x in tds if re.fullmatch(r"20\d\d-\d\d-\d\d", x)), None)
        dept = tds[2] if len(tds) > 4 else ""
        out.append({"id": m[3], "title": clean(m[1]), "date": d, "dept": dept,
                    "url": f"https://mcee.go.kr/home/web/board/read.do?menuId=10598&boardId={m[3]}&boardMasterId=939"})
    return out


def _mcee_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["mcee"].list_url)
    dept = re.search(r"부서명</dt>\s*<dd>(.*?)</dd>", p, re.S)
    body = clean(between(p, r'<div class="view_con[^"]*"[^>]*>|<div class="bbs_view_con[^"]*">|<div id="boardContents[^"]*"[^>]*>',
                         r'<div class="(?:file_list|view_file|btn_|board_btn|bbs_btn)|<ul class="(?:file|prev)'))
    if not body:
        seg = between(p, r'id="boardViewListForRead_0"', r'<div class="(?:btn_|board_btn|bbs_btn)|<ul class="(?:prev|bbs_prev)')
        body = clean(re.sub(r"<dl>.*?</dl>", " ", seg, flags=re.S))
    files = links(p, "https://mcee.go.kr", r"(?:fileDown|download|FileDown)")
    return {"body": body, "dept": clean(dept.group(1)) if dept else "", "attachments": files}


# 9) 원자력안전위원회 — 목록이 JSON(POST /ajaxf/FR_BBS_SVC/BBSViewList.do), SORT=게시 시각, CONTENTS=본문
NSSC_FORM = {"pagePerCnt": "15", "MENU_ID": "190", "SITE_NO": "2", "BOARD_SEQ": "5", "CATE_SEQ": "", "SEARCH_FLD": "", "SEARCH": ""}


def _nssc_fetch(f: Fetcher, m: "Ministry", page_no: int) -> str:
    return _get(f, "https://www.nssc.go.kr/ajaxf/FR_BBS_SVC/BBSViewList.do", referer=m.list_url,
                data={**NSSC_FORM, "pageNo": str(page_no)}, headers={"X-Requested-With": "XMLHttpRequest"})


def _nssc_list(page: str) -> list[dict]:
    data = json.loads(page)["data"]["list"]
    out = []
    for x in data:
        posted = parse_dt(x.get("SORT") or "")
        d = posted.date() if posted else parse_date(x.get("WRITE_DATE") or "")
        sid = str(x["BBS_SEQ"])
        out.append({"id": sid, "title": clean(x.get("SUBJECT")), "date": d, "dept": clean(x.get("DEPT_NM")),
                    "posted_at": posted, "body": clean(x.get("CONTENTS")),
                    "category": {1: "보도자료", 2: "사실은이렇습니다"}.get(x.get("CATE_SEQ"), ""),
                    "url": f"https://www.nssc.go.kr/ko/cms/FR_BBS_CON/BoardView.do?MENU_ID=190&SITE_NO=2&BOARD_SEQ=5&BBS_SEQ={sid}"})
    return out


def _nssc_detail(f: Fetcher, it: dict) -> dict:
    raw = _get(f, "https://www.nssc.go.kr/ajaxf/FR_BBS_SVC/BBSViewAttachList.do", referer=MINISTRIES["nssc"].list_url,
               data={**NSSC_FORM, "BBS_SEQ": it["id"], "TYPE_CODE": "C0501"}, headers={"X-Requested-With": "XMLHttpRequest"})
    files = []
    try:
        for x in json.loads(raw) or []:
            files.append({"name": x.get("FILE_ORG_NM", ""),
                          "url": ("https://www.nssc.go.kr/ajaxfile/FR_SVC/FileDown.do?GBN=B01"
                                  f"&BOARD_SEQ={x.get('BOARD_SEQ')}&BBS_SEQ={x.get('BBS_SEQ')}&FILE_SEQ={x.get('FILE_SEQ')}")})
    except (ValueError, AttributeError):
        pass
    return {"attachments": files}


# 10) 기획재정부(재정경제부) — fn_egov_select('MOSF_…'), 본문은 hidden input#nttCn(HTML 이스케이프)
def _mofe_list(page: str) -> list[dict]:
    out = []
    for li in re.findall(r"<li>\s*<h3>(.*?)</li>", between(page, r'<ul class="boardType3', r'<div class="(?:paging|boardPaging|pagination)'), re.S):
        m = re.search(r"fn_egov_select\('(MOSF_\d+)'\);\">(.*?)</a>", li, re.S)
        if not m:
            continue
        d = re.search(r'<span class="date">(.*?)</span>', li)
        dept = re.search(r'<span class="depart">(.*?)</span>', li)
        out.append({"id": m[1], "title": clean(m[2]), "date": parse_date(d.group(1)) if d else None,
                    "dept": clean(dept.group(1)) if dept else "",
                    "url": ("https://mofe.go.kr/nw/nes/detailNesDtaView.do?searchBbsId1=MOSFBBS_000000000028"
                            f"&searchNttId1={m[1]}&menuNo=4010100"),
                    "attachments": links(li, "https://mofe.go.kr", r"FileDown\.do")})
    return out


def _mofe_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["mofe"].list_url)
    m = re.search(r'id="nttCn" value="([^"]*)"', p)
    body = clean(html.unescape(m.group(1))) if m else ""
    names = {}
    for m in re.finditer(r'href="/com/synap/synapView\.do[^"]*?atchFileId=(\w+)&amp;fileSn=(\d+)"[^>]*title="([^"]+?) 파일 새 창 열림"', p):
        names[(m[1], m[2])] = clean(m[3])
    files, seen = [], set()
    for m in re.finditer(r'href="(/com/cmm/fms/FileDown\.do[^"]*?atchFileId=(\w+)&(?:amp;)?fileSn=(\d+))"', p):
        if (m[2], m[3]) in seen:
            continue
        seen.add((m[2], m[3]))
        files.append({"name": names.get((m[2], m[3]), f"첨부{len(files) + 1}"), "url": absurl("https://mofe.go.kr", m[1])})
    return {"body": body, "attachments": files}


# 11) 금융위원회 — /no010101/번호, 목록에 첨부(getFile)·담당부서
def _fsc_list(page: str) -> list[dict]:
    out = []
    for li in re.split(r'<div class="inner">\s*<div class="count">', page)[1:]:
        m = re.search(r'<a href="/no010101/(\d+)\?[^"]*"[^>]*>(.*?)</a>', li, re.S)
        if not m:
            continue
        d = re.search(r'<div class="day">(.*?)</div>', li)
        dept = re.search(r"담당부서\s*:\s*(.*?)</span>", li, re.S)
        out.append({"id": m[1], "title": clean(m[2]), "date": parse_date(d.group(1)) if d else None,
                    "dept": clean(dept.group(1)) if dept else "",
                    "url": f"https://www.fsc.go.kr/no010101/{m[1]}",
                    "attachments": links(li, "https://www.fsc.go.kr", r"/comm/getFile\?.*fileNo=\d+$", "title")})
    return out


def _fsc_detail(f: Fetcher, it: dict) -> dict:
    p = _get(f, it["url"], referer=MINISTRIES["fsc"].list_url)
    body = clean(between(p, r'<div class="board-view-wrap">.*?<div class="cont">', r'<div class="(?:file|btn-board|board-file)'))
    return {"body": body, "attachments": links(p, "https://www.fsc.go.kr", r"/comm/getFile\?.*fileNo=\d+$", "title")}


MINISTRIES: dict[str, Ministry] = {m.key: m for m in [
    Ministry("motir", "산업부", "https://www.motir.go.kr", "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c",
             _motir_list, lambda n: {"pageIndex": n}, _motir_detail, notes="첨부명에 '13(금) 18시엠바고' 표기",
             rss_url="https://www.motir.go.kr/kor/article/ATCL3f49a5a8c/rss", rss_post=True, rss_id=r"/(\d+)/view"),
    Ministry("msit", "과기정통부", "https://www.msit.go.kr", "https://www.msit.go.kr/bbs/list.do",
             _msit_list, lambda n: {"sCode": "user", "mPid": "208", "mId": "307", "pageIndex": n}, _msit_detail,
             notes="본문은 첨부 .md", rss_url="https://www.msit.go.kr/user/rss/rss.do?bbsSeqNo=94", rss_id=r"nttSeqNo=(\d+)"),
    Ministry("molit", "국토부", "https://www.molit.go.kr", "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp",
             _molit_list, lambda n: {"lcmspage": n}, _molit_detail, cookies=True, need_detail_for_time=True,
             rss_url="https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=NEWS", rss_id=r"[?&]id=(\d+)", rss_exact=True),
    Ministry("dapa", "방위사업청", "https://www.dapa.go.kr", "https://www.dapa.go.kr/dapa/doc/selectDocList.do",
             _dapa_list, lambda n: {"menuSeq": "3069", "bbsSeq": "326", "paginationInfo.currentPageNo": n}, _dapa_detail),
    Ministry("mfds", "식약처", "https://www.mfds.go.kr", "https://www.mfds.go.kr/brd/m_99/list.do",
             _mfds_list, lambda n: {"page": n}, _mfds_detail,
             rss_url="https://www.mfds.go.kr/www/rss/brd.do?brdId=ntc0021", rss_id=r"seq=(\d+)", rss_exact=True),
    Ministry("mohw", "보건복지부", "https://www.mohw.go.kr", "https://www.mohw.go.kr/board.es",
             _mohw_list, lambda n: {"mid": "a10503010100", "bid": "0027", "nPage": n}, _mohw_detail,
             need_detail_for_time=True, rss_url="https://www.mohw.go.kr/rss/board.es?mid=a10503000000&bid=0027",
             rss_id=r"list_no=(\d+)", rss_exact=True),
    Ministry("mof", "해양수산부", "https://www.mof.go.kr", "https://www.mof.go.kr/doc/ko/selectDocList.do",
             _mof_list, lambda n: {"menuSeq": "971", "bbsSeq": "10", "paginationInfo.currentPageNo": n}, _mof_detail,
             rss_url="https://www.mof.go.kr/doc/ko/rssFeed.do?bbsSeq=10", rss_id=r"docSeq=(\d+)", rss_exact=True),
    Ministry("mcee", "기후에너지환경부", "https://mcee.go.kr", "https://mcee.go.kr/home/web/index.do",
             _mcee_list, lambda n: {"menuId": "10598", "pagerOffset": (n - 1) * 10}, _mcee_detail,
             rss_url="https://mcee.go.kr/home/web/board/rss.do?menuId=10598&boardMasterId=939", rss_id=r"boardId=(\d+)",
             rss_exact=True, notes="RSS 시각이 정각(예약 게시로 보임)"),
    Ministry("nssc", "원자력안전위원회", "https://www.nssc.go.kr", "https://www.nssc.go.kr/ko/cms/FR_CON/index.do?MENU_ID=190",
             _nssc_list, lambda n: {}, _nssc_detail, fetch_list=_nssc_fetch, cookies=True),
    Ministry("mofe", "기획재정부", "https://mofe.go.kr", "https://mofe.go.kr/nw/nes/nesdta.do",
             _mofe_list, lambda n: {"bbsId": "MOSFBBS_000000000028", "menuNo": "4010100", "pageIndex": n}, _mofe_detail,
             cookies=True, rss_url="https://mofe.go.kr/com/detailRssTagService.do?bbsId=MOSFBBS_000000000028",
             rss_id=r"(MOSF_\d+)"),
    Ministry("fsc", "금융위원회", "https://www.fsc.go.kr", "https://www.fsc.go.kr/no010101",
             _fsc_list, lambda n: {"curPage": n}, _fsc_detail,
             notes="RSS 안내(http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111)는 있으나 403/503(2026-11-14)"),
]}
NAMES = {m.key: m.name for m in MINISTRIES.values()}
GOV_SOURCES = ("korea_policy", SOURCE)      # 정부 정책 재료로 다루는 기사 소스
# 정책브리핑(korea.kr)의 부처 표기 → 수집 부처 표시명 (부처별 집계를 한 이름으로 모으기 위해)
ALIASES = {"산업통상부": "산업부", "산업통상자원부": "산업부", "과학기술정보통신부": "과기정통부", "국토교통부": "국토부",
           "식품의약품안전처": "식약처", "재정경제부": "기획재정부", "기후에너지환경부": "기후에너지환경부",
           "환경부": "기후에너지환경부", "원안위": "원자력안전위원회", "금융위": "금융위원회", "방사청": "방위사업청",
           "복지부": "보건복지부", "해수부": "해양수산부"}


def ministry_name(office: str | None) -> str:
    o = (office or "").strip()
    return ALIASES.get(o, o) or "정책브리핑"


def policy_label(office: str | None) -> str:
    """근거 표시: [정책:부처명]"""
    return f"정책:{ministry_name(office)}"


# ---------------------------------------------------------------- RSS (정확한 게시 시각)

KST = dt.timezone(dt.timedelta(hours=9))


def parse_rss_time(s: str) -> tuple[dt.datetime | None, bool]:
    """RSS 날짜 → (KST naive datetime, 시·분까지 있나). GMT 표기는 실제 UTC(복지부 상세 14:04 = RSS 05:04 GMT로 확인)."""
    import email.utils
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s or "").strip()
    if not s:
        return None, False
    try:
        t = email.utils.parsedate_to_datetime(s)            # RFC 822: 'Fri, 13 Nov 2026 06:57:39 GMT' / '+0900'
        if t.tzinfo:
            t = t.astimezone(KST).replace(tzinfo=None)
        return t, True
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) KST (\d{4})", s)   # 기후부
    if m:
        d = dt.datetime.strptime(f"{m[1]} {m[2]} {m[6]} {m[3]}:{m[4]}:{m[5]}", "%b %d %Y %H:%M:%S")
        return d, True
    d = parse_date(s)
    if d:
        t = parse_dt(s)
        if t and (t.hour, t.minute, t.second) != (0, 0, 0):     # '2026-11-13 00:00:00.0'(기재부)은 날짜만
            return t, True
        return dt.datetime.combine(d, dt.time(0)), False
    return None, False


def parse_rss(xml: str, m: "Ministry") -> list[dict]:
    out = []
    for it in re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.S):
        def tag(name):
            x = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", it, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", x.group(1))).strip() if x else ""
        link = tag("link") or tag("guid")
        mid = re.search(m.rss_id, link) if m.rss_id else None
        t, exact = parse_rss_time(tag("pubDate") or tag("dc:date"))
        out.append({"id": mid.group(1) if mid else None, "title": clean(tag("title")), "link": link,
                    "posted_at": t, "exact": exact})
    return out


def rss_times(f: Fetcher, m: "Ministry") -> tuple[dict[str, dt.datetime], dict[str, dt.datetime], str | None]:
    """→ (글 번호 → 게시 시각, 정규화 제목 → 게시 시각, 오류). 시·분이 있는 RSS만."""
    if not (m.rss_url and m.rss_exact):
        return {}, {}, None
    try:
        raw = f.get_text(m.rss_url, referer=m.list_url, **({"data": {}} if m.rss_post else {}))
        items = parse_rss(raw, m)
    except Exception as e:  # noqa: BLE001
        return {}, {}, f"RSS 조회 실패 — {e}"
    if not items:
        return {}, {}, "RSS 항목 0건 — 주소·형식 변경 의심"
    by_id = {x["id"]: x["posted_at"] for x in items if x["id"] and x["exact"] and x["posted_at"]}
    by_title = {norm_title(x["title"]): x["posted_at"] for x in items if x["exact"] and x["posted_at"]}
    return by_id, by_title, None


def apply_rss(items: list[dict], by_id: dict, by_title: dict) -> int:
    """목록 항목에 RSS 시각을 붙인다(글 번호 → 제목 순으로 맞춤). RSS 날짜가 목록 날짜와 다르면 쓰지 않는다."""
    n = 0
    for it in items:
        t = by_id.get(it["id"]) or by_title.get(norm_title(it["title"]))
        if t and t.date() == it["date"]:
            it["posted_at"], it["time_source"] = t, "rss"
            n += 1
    return n


def fetch_list_page(f: Fetcher, m: Ministry, page_no: int) -> list[dict]:
    if m.fetch_list:
        if page_no == 1 and m.cookies:
            _get(f, m.list_url)                       # 세션 쿠키
        raw = m.fetch_list(f, m, page_no)
    else:
        raw = _get(f, m.list_url, m.page_args(page_no), referer=m.list_url)
    return m.parse_list(raw)


def list_since(f: Fetcher, m: Ministry, since: dt.date, max_pages: int = MAX_PAGES) -> tuple[list[dict], dict]:
    """since 날짜 이후 목록(최신순 페이지 넘김). 반환 (항목, 상태 {pages, parsed, error, newest})."""
    items: dict[str, dict] = {}
    st = {"pages": 0, "parsed": 0, "error": None, "newest": None, "no_date": 0}
    for page_no in range(1, max_pages + 1):
        try:
            got = fetch_list_page(f, m, page_no)
        except Exception as e:  # noqa: BLE001
            st["error"] = f"목록 {page_no}쪽 조회 실패 — {e}"
            break
        st["pages"] += 1
        st["parsed"] += len(got)
        if not got:
            if page_no == 1:
                st["error"] = "목록 파싱 0건 — 페이지 구조 변경 의심"
            break
        dated = [x for x in got if x.get("date")]
        st["no_date"] += len(got) - len(dated)
        if dated:
            st["newest"] = max(st["newest"] or dt.date.min, max(x["date"] for x in dated))
        for x in dated:
            if x["date"] >= since:
                items.setdefault(x["id"], x)
        if not dated or min(x["date"] for x in dated) < since:
            break
    return sorted(items.values(), key=lambda x: (x["date"], x["id"]), reverse=True), st


def enrich(f: Fetcher, m: Ministry, it: dict) -> dict:
    """상세 페이지로 본문·시각·첨부 보강 + 엠바고 파싱."""
    if m.detail:
        d = m.detail(f, it)
        for k, v in d.items():
            if k == "posted_at" and it.get("time_source") == "rss":
                continue                                # RSS 시각(초 단위)이 상세 표시 시각보다 우선
            if v and (k != "attachments" or len(v) >= len(it.get("attachments") or [])):
                it[k] = v
    texts = [it["title"]] + [a["name"] for a in it.get("attachments") or []] + [(it.get("body") or "")[:800]]
    it["embargo_text"], it["embargo_at"] = parse_embargo(texts, it["date"])
    return it


# ---------------------------------------------------------------- 게시 시각 판정

def snapshot_dates(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT snap_date FROM press_snapshots")}


def seen_in_snapshot(conn, ministry: str, item_id: str, date: str) -> bool:
    return conn.execute("SELECT 1 FROM press_seen WHERE ministry=? AND item_id=? AND first_seen_at<=?",
                        (ministry, item_id, f"{date} {SNAPSHOT_TIME}:59")).fetchone() is not None


def timing(conn, m: Ministry, it: dict, window: Window, snaps: set[str]) -> tuple[dt.datetime, str]:
    """→ (published_at, time_basis). published_at이 시간대 안이면 수집 대상."""
    d: dt.date = it["date"]
    if it.get("posted_at"):
        return it["posted_at"], it.get("time_source") or "page"
    start_d = window.start.date()
    if d >= window.end.date() and now_kst() - window.end > dt.timedelta(minutes=30):
        # 지난 시간대를 다시 받는 경우(백필): 끝 날짜 글은 시간대 끝 뒤에 올라왔을 수도 있다 → 시각 모르면 판단 불가
        emb = it.get("embargo_at")
        if emb and window.contains(emb):
            return emb, "embargo"
        return dt.datetime.combine(d, dt.time(0)), "unknown"
    if d > start_d:
        return max(dt.datetime.combine(d, dt.time(0)), window.start), "date"
    if d < start_d:
        return dt.datetime.combine(d, dt.time(0)), "date"
    ds = d.isoformat()
    if ds in snaps:
        if seen_in_snapshot(conn, m.key, it["id"], ds):
            return dt.datetime.combine(d, dt.time(0)), "before"
        return window.start, "snapshot"
    # 장 전 추가(07:50·08:00): 아침 07:00 실행이 이 부처 목록을 이미 봤다면, 그때 없던 글 = 그 뒤(장 전)에 올라온 글
    lo = f"{window.start - dt.timedelta(hours=3):%Y-%m-%d %H:%M:%S}"
    hi = f"{window.start:%Y-%m-%d %H:%M:%S}"
    if window.start.time() < dt.time(15, 30) and conn.execute(
            "SELECT 1 FROM press_seen WHERE ministry=? AND first_seen_at BETWEEN ? AND ? LIMIT 1", (m.key, lo, hi)).fetchone():
        if not conn.execute("SELECT 1 FROM press_seen WHERE ministry=? AND item_id=? AND first_seen_at<=?",
                            (m.key, it["id"], hi)).fetchone():
            return window.start, "snapshot"
    emb = it.get("embargo_at")
    if emb and emb >= window.start:
        return max(window.start, min(emb, window.end)), "embargo"
    return dt.datetime.combine(d, dt.time(0)), "unknown"


def intraday_start(window: Window) -> dt.datetime:
    """시간대 시작일의 장 전·장중 게시 구간 시작(07:00). 정책은 장 전·장중에 게시돼도 이후 리포트 후보에 남긴다
    (그날 장 전 추가(07:50·08:00)에서 이미 채점한 글은 candidates가 뺀다)."""
    s = dt.datetime.combine(window.start.date(), INTRADAY_FROM)
    return s if s < window.start else window.start


PRE_OPEN_END = dt.time(8, 0)
SESSION_LABELS = ("장 마감 후", "장 전", "장중")


def session_of(t: dt.datetime | str | None, trading_day: bool | None = None) -> str:
    """게시·접수 시각 → 구간: 장 마감 후(15:30~다음 07:00) / 장 전(07:00~08:00) / 장중(08:00~15:30).
    휴장일 낮 시각은 '장 마감 후'(다음 거래일 전 재료)."""
    if t is None:
        return "장 마감 후"
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t[:19])
    if trading_day is None:
        from ..timewindow import is_trading_day, load_holidays
        trading_day = is_trading_day(t.date(), load_holidays())
    if not trading_day:
        return "장 마감 후"
    if INTRADAY_FROM <= t.time() < PRE_OPEN_END:
        return "장 전"
    if PRE_OPEN_END <= t.time() < dt.time(15, 30):
        return "장중"
    return "장 마감 후"


def posted_session(published_at: str | None, time_basis: str | None = None) -> str:
    """정책 게시 구간(session_of). 15:30 스냅샷에 이미 있던 날짜만 아는 글(before)은 '장중'."""
    if time_basis == "before":
        return "장중"
    if not published_at:
        return "장 마감 후"
    if time_basis in ("date", None) and published_at[11:19] == "00:00:00":
        return "장 마감 후"              # 날짜만 아는 밤사이·휴일 글
    return session_of(published_at)


# ---------------------------------------------------------------- 중복 묶기(정책브리핑·부처 간 합동 보도자료)

_BRACKETS = re.compile(r"^\s*(?:[\[(〔【<]\s*[^\])〕】>]{1,20}[\])〕】>]\s*)+")


def norm_title(t: str) -> str:
    t = _BRACKETS.sub("", t or "")
    t = re.sub(r"[\s\"'“”‘’「」『』《》<>·…,.!?\-–—:;()\[\]]", "", t)
    return t


def _grams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", "", s or "")[:3000]
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def similarity(a: dict, b: dict) -> tuple[float, str]:
    """제목 유사도(SequenceMatcher)와 본문 3-gram 자카드. → (점수, 근거)"""
    ta, tb = norm_title(a["title"]), norm_title(b["title"])
    title = difflib.SequenceMatcher(None, ta, tb).ratio() if ta and tb else 0.0
    ga, gb = _grams(a.get("body") or a.get("summary") or ""), _grams(b.get("body") or b.get("summary") or "")
    body = len(ga & gb) / len(ga | gb) if ga and gb and min(len(ga), len(gb)) >= 60 else 0.0
    # 한쪽 제목이 다른 쪽에 통째로 들어 있으면(부제 붙은 정책브리핑 제목) 제목 일치로 본다
    if ta and tb and min(len(ta), len(tb)) >= 10 and (ta in tb or tb in ta):
        title = max(title, 0.9)
    score = max(title, body * 1.6, 0.6 * title + body)
    return round(score, 3), f"제목 {title:.2f} · 본문 {body:.2f}"


DUP_THRESHOLD = 0.72


def dedupe(conn, dates: list[str], log=print) -> list[dict]:
    """dates 범위(±1일)의 정책브리핑·부처 보도자료를 묶는다. 대표 = 부처 원문(없으면 먼저 게시된 쪽),
    대표의 published_at = 묶음에서 가장 먼저 게시된 시각(시각을 아는 것 중). 반환: 새로 묶인 쌍."""
    lo = (dt.date.fromisoformat(min(dates)) - dt.timedelta(days=1)).isoformat()
    hi = (dt.date.fromisoformat(max(dates)) + dt.timedelta(days=1)).isoformat() + " 23:59:59"
    rows = [dict(r) for r in conn.execute(
        "SELECT a.id, a.source, a.office_name, a.title, a.summary, a.body, a.published_at, "
        "       p.time_basis, p.posted_at, d.canonical_id "
        "FROM articles a LEFT JOIN press_meta p ON p.article_id=a.id LEFT JOIN policy_dups d ON d.article_id=a.id "
        "WHERE a.source IN ('korea_policy', ?) AND a.published_at BETWEEN ? AND ? ORDER BY a.published_at, a.id",
        (SOURCE, lo, hi))]
    exact = lambda r: r["source"] == "korea_policy" or r["time_basis"] in EXACT_BASES + ("before",)  # noqa: E731

    def key(r):          # 비교용 시각: before(15:30 스냅샷에 이미 있던 글)는 '그날 15:30 이전'
        if r["time_basis"] == "before":
            return r["published_at"][:10] + f" {SNAPSHOT_TIME}:00"
        return r["published_at"]
    parent = {r["id"]: r["canonical_id"] or r["id"] for r in rows}

    def root(x):
        while parent[x] != x and parent.get(parent[x], parent[x]) != parent[x]:
            parent[x] = parent[parent[x]]
        return parent[x] if parent[x] in parent else x
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["source"] == b["source"] == "korea_policy":
                continue                           # 정책브리핑끼리는 묶지 않음(서로 다른 기사)
            if abs((dt.datetime.fromisoformat(a["published_at"]) - dt.datetime.fromisoformat(b["published_at"])).days) > 2:
                continue
            ra, rb = root(a["id"]), root(b["id"])
            if ra == rb:
                continue
            s, why = similarity(a, b)
            if s >= DUP_THRESHOLD:
                parent[rb] = ra
                pairs.append({"a": a["id"], "b": b["id"], "score": s, "why": why})
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(root(r["id"]), []).append(r)
    stamp = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    why_of = {}
    for p in pairs:
        why_of[p["a"]] = why_of.get(p["a"]) or p
        why_of[p["b"]] = p
    for members in groups.values():
        if len(members) < 2:
            continue
        ministry = [r for r in members if r["source"] == SOURCE]
        timed = [r for r in members if exact(r)]
        first = min(timed or members, key=lambda r: (key(r), r["id"]))
        canon = min(ministry, key=lambda r: (not exact(r), key(r), r["id"])) if ministry else first
        earliest = first["published_at"]
        if canon["published_at"] != earliest:
            conn.execute("UPDATE articles SET published_at=? WHERE id=?", (earliest, canon["id"]))
        conn.execute("UPDATE press_meta SET merged_from=? WHERE article_id=?",
                     (json.dumps([{"id": r["id"], "source": r["source"], "office": r["office_name"],
                                   "published_at": r["published_at"]} for r in members if r["id"] != canon["id"]],
                                 ensure_ascii=False), canon["id"]))
        conn.execute("DELETE FROM policy_dups WHERE article_id=?", (canon["id"],))
        for r in members:
            if r["id"] == canon["id"]:
                continue
            p = why_of.get(r["id"], {})
            conn.execute("INSERT OR REPLACE INTO policy_dups VALUES (?, ?, ?, ?, ?)",
                         (r["id"], canon["id"], p.get("score"), p.get("why"), stamp))
    conn.commit()
    if pairs:
        log(f"  중복 묶음 {len(pairs)}쌍(정책브리핑·부처 합동 보도자료)")
    return pairs


# ---------------------------------------------------------------- 수집

def snapshot(conn, only: list[str] | None = None, log=print) -> dict:
    """15:30 스냅샷: 오늘 날짜 목록만 기록(본문 요청 없음). 날짜만 있는 부처의 '15:30 이후 게시' 판정 기준."""
    today = now_kst().date()
    stamp = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    res = {}
    for m in MINISTRIES.values():
        if only and m.key not in only:
            continue
        f = Fetcher(min_interval=0.7, cookies=m.cookies)
        items, st = list_since(f, m, today, max_pages=3)
        for it in items:
            conn.execute("INSERT OR IGNORE INTO press_seen VALUES (?, ?, ?, ?, ?)",
                         (m.key, it["id"], it["date"].isoformat(), it["title"], stamp))
        res[m.key] = {"items": len(items), "error": st["error"]}
        log(f"  {m.name}: 오늘 {len(items)}건" + (f" ⚠️ {st['error']}" if st["error"] else ""))
    ok = all(not r["error"] for r in res.values())
    conn.execute("INSERT OR REPLACE INTO press_snapshots VALUES (?, ?, ?)", (today.isoformat(), stamp, json.dumps(res, ensure_ascii=False)))
    conn.commit()
    return {"ok": ok, "ministries": res}


def collect(conn, window: Window, fetch_bodies: bool = True, only: list[str] | None = None) -> dict:
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 부처 보도자료 수집: {window}")
    collected_at = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    snaps = snapshot_dates(conn)
    since = window.start.date()
    per: dict[str, dict] = {}
    total_in, total_new = 0, 0
    for m in MINISTRIES.values():
        if only and m.key not in only:
            continue
        f = Fetcher(min_interval=0.7, cookies=m.cookies, timeout=15, retries=1)
        t_end = time.monotonic() + BUDGET_SEC
        items, st = list_since(f, m, since)
        row = {"name": m.name, "listed": len(items), "in_window": 0, "intraday": 0, "new": 0, "excluded_unknown": 0,
               "before_snapshot": 0, "detail_failed": 0, "rss_matched": 0, "error": st["error"]}
        per[m.key] = row
        if st["error"]:
            warn.add(f"{SOURCE}/{m.name}: {st['error']}")
            continue
        if items and m.rss_url and m.rss_exact:          # RSS 시각 우선(1회 요청)
            by_id, by_title, rerr = rss_times(f, m)
            if rerr:
                warn.add(f"{SOURCE}/{m.name}: {rerr} — 상세·15:30 스냅샷으로 대체")
            row["rss_matched"] = apply_rss(items, by_id, by_title)
        if st["no_date"]:
            warn.add(f"{SOURCE}/{m.name}: 날짜 못 읽은 항목 {st['no_date']}건 — 목록 구조 변경 의심")
        if st["newest"] and (window.end.date() - st["newest"]).days > 10:
            warn.add(f"{SOURCE}/{m.name}: 최신 글이 {st['newest']} — 10일 넘게 새 글 없음(주소·구조 변경 의심)")
        for it in items:
            conn.execute("INSERT OR IGNORE INTO press_seen VALUES (?, ?, ?, ?, ?)",
                         (m.key, it["id"], it["date"].isoformat(), it["title"], collected_at))
            have = conn.execute("SELECT a.id, a.published_at, p.time_basis FROM articles a JOIN press_meta p ON p.article_id=a.id "
                                "WHERE a.office_id=? AND a.article_id=?", (m.key, it["id"])).fetchone()
            upgrade = it.get("time_source") == "rss" and have is not None and have["time_basis"] != "rss"
            if have and not upgrade and have["time_basis"] in EXACT_BASES + ("snapshot", "date", "embargo", "before"):
                t = dt.datetime.fromisoformat(have["published_at"])
                if window.contains(t):
                    row["in_window"] += 1
                continue
            # 시각을 상세에서만 알 수 있는 부처는 시작일 글도 상세를 본다. 나머지는 판정 후 시간대 안만 상세.
            pub, basis = timing(conn, m, it, window, snaps)
            intra_s = intraday_start(window)
            is_intra = lambda p, b: (b in EXACT_BASES and intra_s <= p < window.start) or b == "before"  # noqa: E731
            need = (window.contains(pub) or is_intra(pub, basis) or basis == "unknown"
                    or (m.need_detail_for_time and it["date"] == since and basis not in EXACT_BASES))
            if need and fetch_bodies and time.monotonic() > t_end:
                row["detail_failed"] += 1
                if row["detail_failed"] == 1:
                    warn.add(f"{SOURCE}/{m.name}: {BUDGET_SEC}초 넘게 걸려 남은 상세 조회 생략(다음 실행 때 다시)")
                continue
            if need and fetch_bodies:
                try:
                    enrich(f, m, it)
                except Exception as e:  # noqa: BLE001
                    row["detail_failed"] += 1
                    if row["detail_failed"] <= 2:
                        warn.add(f"{SOURCE}/{m.name}: 상세 조회 실패 {it['id']} — {e}")
                    if m.need_detail_for_time and it["date"] == since and basis not in EXACT_BASES:
                        continue
                pub, basis = timing(conn, m, it, window, snaps)
            else:
                it.setdefault("embargo_text", None)
                it.setdefault("embargo_at", None)
            if basis == "before":
                row["before_snapshot"] += 1
            if basis == "unknown":
                row["excluded_unknown"] += 1
            inside = window.contains(pub)
            if not inside and basis not in EXACT_BASES + ("before",):
                continue                               # 시각 미확정(unknown)은 저장하지 않음 — 다음 실행 때 다시 판정
            # 시각을 아는 글·before(장중 게시)는 시간대 밖이어도 저장 — 장중 게시 정책도 후보에 남기고('장중 게시' 표시),
            # 같은 보도의 정책브리핑 기사를 '더 이른 시각'으로 묶는 기준이 된다
            a = {"source": SOURCE, "office_id": m.key, "article_id": it["id"], "office_name": m.name,
                 "title": it["title"], "summary": (it.get("body") or "")[:400], "published_at": f"{pub:%Y-%m-%d %H:%M:%S}",
                 "url": it["url"], "collected_at": collected_at}
            new = db.insert_article(conn, a)
            if not new:
                conn.execute("UPDATE articles SET published_at=?, title=?, summary=COALESCE(NULLIF(summary,''), ?) "
                             "WHERE office_id=? AND article_id=?", (a["published_at"], a["title"], a["summary"], m.key, it["id"]))
            if it.get("body"):
                db.set_article_body(conn, m.key, it["id"], it["body"], collected_at)
            aid = conn.execute("SELECT id FROM articles WHERE office_id=? AND article_id=?", (m.key, it["id"])).fetchone()[0]
            conn.execute(
                "INSERT INTO press_meta (article_id, ministry, ministry_name, item_id, dept, category, list_date, posted_at, "
                " time_basis, time_source, embargo_at, embargo_text, attachments, first_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(article_id) DO UPDATE SET dept=excluded.dept, posted_at=excluded.posted_at, "
                " time_basis=excluded.time_basis, time_source=excluded.time_source, embargo_at=excluded.embargo_at, "
                " embargo_text=excluded.embargo_text, attachments=excluded.attachments",
                (aid, m.key, m.name, it["id"], it.get("dept") or "", it.get("category") or "", it["date"].isoformat(),
                 f"{it['posted_at']:%Y-%m-%d %H:%M:%S}" if it.get("posted_at") else None, basis, TIME_SOURCE.get(basis, basis),
                 f"{it['embargo_at']:%Y-%m-%d %H:%M:%S}" if it.get("embargo_at") else None, it.get("embargo_text"),
                 json.dumps(it.get("attachments") or [], ensure_ascii=False), collected_at))
            if inside:
                row["in_window"] += 1
                row["new"] += int(new)
            elif is_intra(pub, basis):
                row["intraday"] += 1
        if row["detail_failed"] > 2:
            warn.add(f"{SOURCE}/{m.name}: 상세 조회 실패 총 {row['detail_failed']}건")
        conn.commit()
        total_in += row["in_window"]
        total_new += row["new"]
        print(f"  {m.name}: 목록 {row['listed']} · 시간대 안 {row['in_window']}(신규 {row['new']}) · 장중 게시 {row['intraday']}"
              + (f" · RSS 시각 {row['rss_matched']}" if row["rss_matched"] else "")
              + (f" · 시각 미상 제외 {row['excluded_unknown']}" if row["excluded_unknown"] else ""))
    unknown = sum(r["excluded_unknown"] for r in per.values())
    if unknown:
        warn.add(f"{SOURCE}: 게시 시각을 알 수 없어 제외 {unknown}건 — {since} 15:30 스냅샷 없음(평일 15:30 cron 확인)")
    try:
        dedupe(conn, [d.isoformat() for d in window.dates()])
    except Exception as e:  # noqa: BLE001
        warn.add(f"{SOURCE}: 중복 묶기 실패 — {e!r}")
    db.log_run(conn, SOURCE, f"{started:%Y-%m-%d %H:%M:%S}", f"{now_kst():%Y-%m-%d %H:%M:%S}",
               window, total_in, total_new, warn)
    conn.execute("UPDATE collect_runs SET detail=? WHERE id=(SELECT MAX(id) FROM collect_runs WHERE source=?)",
                 (json.dumps(per, ensure_ascii=False), SOURCE))
    conn.commit()
    return {"source": SOURCE, "fetched": total_in, "inserted": total_new, "warnings": list(warn), "ministries": per}


def probe(days: int = 3, only: list[str] | None = None, details: int = 2, log=print) -> list[dict]:
    """DB에 쓰지 않고 최근 days일 목록 + 상세 몇 건을 실제로 파싱해 본다(구조 점검용)."""
    since = now_kst().date() - dt.timedelta(days=days - 1)
    out = []
    for m in MINISTRIES.values():
        if only and m.key not in only:
            continue
        f = Fetcher(min_interval=0.7, cookies=m.cookies)
        items, st = list_since(f, m, since)
        r = {"key": m.key, "name": m.name, "listed": len(items), "error": st["error"], "pages": st["pages"],
             "by_date": {}, "detail_ok": 0, "detail_tried": 0, "body_chars": [], "attachments": 0, "posted_at": 0,
             "embargo": [], "sample": None, "detail_error": None}
        for it in items:
            r["by_date"][it["date"].isoformat()] = r["by_date"].get(it["date"].isoformat(), 0) + 1
        for it in items[:details]:
            r["detail_tried"] += 1
            try:
                enrich(f, m, it)
                r["detail_ok"] += 1
                r["body_chars"].append(len(it.get("body") or ""))
                r["attachments"] += len(it.get("attachments") or [])
                r["posted_at"] += int(bool(it.get("posted_at")))
                if it.get("embargo_text"):
                    r["embargo"].append(f"{it['embargo_text']}" + (f" → {it['embargo_at']:%m-%d %H:%M}" if it.get("embargo_at") else ""))
            except Exception as e:  # noqa: BLE001
                r["detail_error"] = str(e)[:120]
        if items:
            s = items[0]
            r["sample"] = {"title": s["title"], "date": s["date"].isoformat(), "dept": s.get("dept"),
                           "posted_at": f"{s['posted_at']:%Y-%m-%d %H:%M}" if s.get("posted_at") else None,
                           "body": (s.get("body") or "")[:120], "attachments": [a["name"] for a in (s.get("attachments") or [])][:3],
                           "embargo": s.get("embargo_text"), "url": s["url"]}
        out.append(r)
        log(f"  {m.name}: {len(items)}건 {r['by_date']} 상세 {r['detail_ok']}/{r['detail_tried']}"
            + (f" ⚠️ {st['error']}" if st["error"] else "") + (f" ⚠️ 상세 {r['detail_error']}" if r["detail_error"] else ""))
    return out
