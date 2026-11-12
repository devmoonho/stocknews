"""네이버 증권 '뉴스 포커스 > 시황·전망' 수집.

목록: https://stock.naver.com/api/domestic/news/focus?sid=401&page=N&pageSize=100&date=YYYYMMDD
  - 최신순. 요청 날짜의 기사가 끝나면 이전 날짜로 이어진다 → 시간대 시작 이전 기사가 나오면 중단.
  - 페이지 경계에서 기사 1건이 겹칠 수 있음 → (officeID, articleID)로 중복 제거.
본문: 기사 url(n.news.naver.com) 페이지의 #dic_area.
"""
from __future__ import annotations

import datetime as dt
import html
import re

from .. import db
from ..health import Warnings, check_fields, check_volume
from ..http import Fetcher
from ..timewindow import Window, now_kst

LIST_URL = "https://stock.naver.com/api/domestic/news/focus"
REFERER = "https://stock.naver.com/news/section?tab=market-outlook"
SECTIONS = {"401": "시황·전망", "402": "기업·종목분석", "403": "해외증시", "404": "채권·선물",
            "406": "공시·메모", "429": "환율"}
REQUIRED = ("officeID", "articleID", "title", "date")
PAGE_SIZE = 100
MAX_PAGES = 30


def _parse_dt(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s, "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def _fetch_day(fetcher: Fetcher, window: Window, sid: str, day: dt.date, warn: Warnings,
               out: dict[tuple[str, str], dict]) -> bool:
    """하루치를 페이지로 넘기며 수집. 형식 이상이면 False.

    API 특성(2026-11 확인): 요청 날짜의 기사가 끝나면 응답 date가 전날로 바뀌고, 그 뒤 페이지는
    전날 1페이지를 계속 반복한다 → 응답 date가 요청 날짜와 다르면 그 날짜는 끝난 것으로 본다.
    """
    ymd = f"{day:%Y%m%d}"
    for page in range(1, MAX_PAGES + 1):
        data = fetcher.get_json(LIST_URL, {"sid": sid, "page": page, "pageSize": PAGE_SIZE, "date": ymd},
                                referer=REFERER)
        items = data.get("articles") if isinstance(data, dict) else None
        if items is None:
            keys = list(data)[:10] if isinstance(data, dict) else type(data).__name__
            warn.add(f"naver_focus_{sid}: 응답에 'articles' 없음 — 형식 변경 의심. keys={keys}")
            return False
        if not check_fields(items, REQUIRED, f"naver_focus_{sid}", warn):
            return False
        oldest = None
        for it in items:
            t = _parse_dt(it.get("date"))
            if t is None:
                warn.add(f"naver_focus_{sid}: 날짜 형식 이상 {it.get('date')!r}")
                continue
            if t.date() != day:          # 날짜가 넘어가며 섞여 온 다른 날 기사는 그 날짜 차례에 받는다
                continue
            oldest = t if oldest is None or t < oldest else oldest
            if window.contains(t):
                out.setdefault((it["officeID"], it["articleID"]), {**it, "_dt": t})
        if not items or str(data.get("date")) != ymd or (oldest is not None and oldest < window.start):
            return True
    warn.add(f"naver_focus_{sid}: {day} {MAX_PAGES}페이지를 넘김 — 일부 누락 가능")
    return True


def fetch_list(fetcher: Fetcher, window: Window, sid: str, warn: Warnings) -> list[dict]:
    """시간대 안의 기사 목록(중복 제거, 최신순). 날짜별로 최신 날짜부터."""
    out: dict[tuple[str, str], dict] = {}
    for day in reversed(window.dates()):
        if not _fetch_day(fetcher, window, sid, day, warn, out):
            break
    return sorted(out.values(), key=lambda x: x["_dt"], reverse=True)


_DIC_AREA = re.compile(r'<article[^>]*id="dic_area"[^>]*>(.*?)</article>', re.S)


def fetch_body(fetcher: Fetcher, url: str) -> str | None:
    page = fetcher.get_text(url)
    m = _DIC_AREA.search(page)
    if not m:
        return None
    t = m.group(1)
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r'<(em|span) class="img_desc"[^>]*>.*?</\1>', " ", t, flags=re.S)   # 사진 설명
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def collect(conn, window: Window, sid: str = "401", fetch_bodies: bool = True) -> dict:
    source = f"naver_focus_{sid}"
    started = now_kst()
    warn = Warnings()
    print(f"[{source}] {SECTIONS.get(sid, sid)} 수집: {window}")
    try:
        items = fetch_list(Fetcher(min_interval=0.5), window, sid, warn)
    except Exception as e:  # 네트워크/파싱 실패도 경고로 남기고 계속
        warn.add(f"{source}: 목록 수집 실패 — {e}")
        items = []
    collected_at = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    inserted = 0
    for it in items:
        row = {
            "source": source, "office_id": it["officeID"], "article_id": it["articleID"],
            "office_name": it.get("officeHName"), "title": html.unescape(it["title"]).strip(),
            "summary": html.unescape(it.get("subcontent") or "").strip(),
            "published_at": f"{it['_dt']:%Y-%m-%d %H:%M:%S}",
            "url": it.get("url") or f"https://n.news.naver.com/mnews/article/{it['officeID']}/{it['articleID']}",
            "collected_at": collected_at,
        }
        inserted += db.insert_article(conn, row)
    conn.commit()
    print(f"  목록 {len(items)}건 (신규 {inserted}건)")

    if fetch_bodies:
        todo = conn.execute(
            "SELECT office_id, article_id, url FROM articles WHERE source=? AND body IS NULL "
            "AND published_at BETWEEN ? AND ?",
            (source, f"{window.start:%Y-%m-%d %H:%M:%S}", f"{window.end:%Y-%m-%d %H:%M:%S}")).fetchall()
        body_fetcher, failed = Fetcher(min_interval=0.4), 0
        for i, r in enumerate(todo, 1):
            try:
                body = fetch_body(body_fetcher, r["url"])
            except Exception:
                body = None
            if body:
                db.set_article_body(conn, r["office_id"], r["article_id"], body, f"{now_kst():%Y-%m-%d %H:%M:%S}")
            else:
                failed += 1     # 제목만 있는 속보 등 — 빈 문자열로 표시해 다음 실행 때 다시 시도하지 않음
                db.set_article_body(conn, r["office_id"], r["article_id"], "", f"{now_kst():%Y-%m-%d %H:%M:%S}")
            if i % 20 == 0:
                conn.commit()
                print(f"  본문 {i}/{len(todo)}")
        conn.commit()
        print(f"  본문 {len(todo) - failed}/{len(todo)}건 저장")
        if failed >= 5 and failed > len(todo) * 0.3:
            warn.add(f"{source}: 본문 추출 실패 {failed}/{len(todo)}건 — 기사 페이지 구조 변경 의심(#dic_area)")

    check_volume(conn, source, len(items), warn)
    db.log_run(conn, source, f"{started:%Y-%m-%d %H:%M:%S}", f"{now_kst():%Y-%m-%d %H:%M:%S}",
               window, len(items), inserted, warn)
    conn.commit()
    return {"source": source, "fetched": len(items), "inserted": inserted, "warnings": list(warn)}
