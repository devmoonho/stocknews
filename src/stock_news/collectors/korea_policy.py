"""정책브리핑(korea.kr) 정책뉴스 수집 — 정부 부처가 직접 낸 정책 기사.

목록: https://www.korea.kr/news/policyNewsList.do?pageIndex=N  (최신순, 페이지당 10건, 날짜만 표시 YYYY.MM.DD)
  - 페이지 맨 위 '대표 기사' 블록(첫 번째 list_type)은 매 페이지 반복 → 두 번째 list_type만 본다.
  - 목록의 날짜가 시간대 시작일보다 이전인 기사만 남은 페이지가 나오면 중단.
본문: https://www.korea.kr/news/policyNewsView.do?newsId=...
  - 발행 시각: <meta property="article:published_time" content="2026-11-12T17:25:00Z">  ※ 'Z'가 붙어 있지만 실제는 KST
  - 부제: .article_head h2 / 본문: class="view_cont" ~ class="article_footer"
robots.txt: 전체 허용(2026-11 확인). 그래도 요청 간격 0.5초 유지.
"""
from __future__ import annotations

import datetime as dt
import html
import re

from .. import db
from ..health import Warnings, check_volume
from ..http import Fetcher
from ..timewindow import Window, now_kst

SOURCE = "korea_policy"
BASE = "https://www.korea.kr"
LIST_URL = f"{BASE}/news/policyNewsList.do"
VIEW_URL = f"{BASE}/news/policyNewsView.do"
MAX_PAGES = 40

_ITEM = re.compile(
    r'policyNewsView\.do\?newsId=(\d+)"[^>]*>.*?<strong>(.*?)</strong>(.*?)<span class="source">\s*'
    r'<span>([\d.]+)</span>\s*<span>(.*?)</span>', re.S)
_STITLE = re.compile(r'<span class="stitle">(.*?)</span>', re.S)
_LEAD = re.compile(r'<span class="lead">(.*?)</span>', re.S)
_PUBLISHED = re.compile(r'<meta property="article:published_time" content="([^"]+)"')
_HEAD = re.compile(r'class="article_head">\s*<h2>(.*?)</h2>', re.S)


def _clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_list(page: str) -> list[dict]:
    """목록 페이지 → [{news_id, title, date, dept, lead}]. 대표 기사 블록은 제외."""
    starts = [m.start() for m in re.finditer(r'class="list_type"', page)]
    if not starts:
        return []
    seg = page[starts[-1]:]                 # 마지막 list_type이 본 목록
    end = seg.find('class="paging"')
    seg = seg[:end] if end > 0 else seg
    out = []
    for nid, title, mid, date_s, dept in _ITEM.findall(seg):
        lead = _LEAD.search(mid)
        stitle = _STITLE.search(mid)
        try:
            d = dt.datetime.strptime(date_s, "%Y.%m.%d").date()
        except ValueError:
            continue
        out.append({"news_id": nid, "title": _clean(title), "date": d, "dept": _clean(dept),
                    "stitle": _clean(stitle.group(1)) if stitle else "",
                    "lead": _clean(lead.group(1)) if lead else ""})
    return out


def parse_view(page: str) -> dict:
    m = _PUBLISHED.search(page)
    published = None
    if m:
        try:   # 'Z'가 붙어 있지만 KST 값이다(예: 오후 기사가 17:25Z). UTC로 해석하면 미래 시각이 된다.
            published = dt.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    i = page.find('class="view_cont"')
    body = None
    if i >= 0:
        j = page.find('class="article_footer"', i)
        body = _clean(page[page.find(">", i) + 1: j if j > 0 else i + 60000])
    head = _HEAD.search(page)
    return {"published": published, "body": body, "subtitle": _clean(head.group(1)) if head else ""}


def collect(conn, window: Window, fetch_bodies: bool = True) -> dict:
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 정책브리핑 정책뉴스 수집: {window}")
    fetcher = Fetcher(min_interval=0.5)

    # 1) 목록: 시작일 이전 날짜만 남을 때까지 페이지 넘김
    cands: dict[str, dict] = {}
    for page_no in range(1, MAX_PAGES + 1):
        try:
            items = parse_list(fetcher.get_text(LIST_URL, {"pageIndex": page_no}, referer=LIST_URL))
        except Exception as e:
            warn.add(f"{SOURCE}: 목록 {page_no}페이지 조회 실패 — {e}")
            break
        if not items:
            if page_no == 1:
                warn.add(f"{SOURCE}: 목록 파싱 0건 — 페이지 구조 변경 의심")
            break
        for it in items:
            if window.start.date() <= it["date"] <= window.end.date():
                cands.setdefault(it["news_id"], it)
        if max(it["date"] for it in items) < window.start.date():
            break
    else:
        warn.add(f"{SOURCE}: {MAX_PAGES}페이지까지 읽었는데 시간대 시작일에 못 미침 — 일부 누락 가능")
    print(f"  목록에서 날짜 범위 해당 {len(cands)}건")

    # 2) 본문 + 발행 시각 → 시간대로 거르기 (목록에는 날짜만 있어서 시작일 기사는 본문을 봐야 판단 가능)
    collected_at = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    in_window, inserted, no_time = 0, 0, 0
    for nid, it in cands.items():
        exists = conn.execute("SELECT published_at, body FROM articles WHERE office_id='korea.kr' AND article_id=?",
                              (nid,)).fetchone()
        if exists and exists["body"]:
            published = dt.datetime.strptime(exists["published_at"], "%Y-%m-%d %H:%M:%S")
            if window.contains(published):
                in_window += 1
            continue
        try:
            view = parse_view(fetcher.get_text(VIEW_URL, {"newsId": nid}, referer=LIST_URL))
        except Exception as e:
            warn.add(f"{SOURCE}: {nid} 본문 조회 실패 — {e}")
            continue
        published = view["published"]
        if published is None:
            no_time += 1
            if it["date"] <= window.start.date():
                continue                         # 시작일 기사는 시각 없으면 장중일 수 있어 제외
            published = dt.datetime.combine(it["date"], dt.time(0, 0))
        if published.date() != it["date"]:
            warn.add(f"{SOURCE}: {nid} 목록 날짜 {it['date']} ≠ 발행 시각 {published} — 시각 형식 변경 의심")
        if published > now_kst() + dt.timedelta(minutes=30):
            warn.add(f"{SOURCE}: {nid} 발행 시각이 미래({published}) — 시간대(KST/UTC) 표기 변경 의심")
        if not window.contains(published):
            continue
        in_window += 1
        summary = " / ".join(x for x in (it["stitle"] or view["subtitle"].replace("\n", " / "), it["lead"][:300]) if x)
        row = {"source": SOURCE, "office_id": "korea.kr", "article_id": nid, "office_name": it["dept"],
               "title": it["title"], "summary": summary, "published_at": f"{published:%Y-%m-%d %H:%M:%S}",
               "url": f"{VIEW_URL}?newsId={nid}", "collected_at": collected_at}
        inserted += db.insert_article(conn, row)
        if fetch_bodies and view["body"]:
            db.set_article_body(conn, "korea.kr", nid, view["body"], collected_at)
    conn.commit()
    print(f"  시간대 안 {in_window}건 (신규 {inserted}건)")
    if no_time > max(2, len(cands) * 0.3):
        warn.add(f"{SOURCE}: 발행 시각(meta) 없는 기사 {no_time}건 — 본문 페이지 구조 변경 의심")

    check_volume(conn, SOURCE, in_window, warn)
    db.log_run(conn, SOURCE, f"{started:%Y-%m-%d %H:%M:%S}", f"{now_kst():%Y-%m-%d %H:%M:%S}",
               window, in_window, inserted, warn)
    conn.commit()
    return {"source": SOURCE, "fetched": in_window, "inserted": inserted, "warnings": list(warn)}
