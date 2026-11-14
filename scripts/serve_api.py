#!/usr/bin/env python3
"""읽기 전용 REST API — 다른 PC 프로그램(RAS 등)이 뉴스·공시·채점 결과를 조회한다.

    python3 scripts/serve_api.py                 # 0.0.0.0:8770 에서 실행
    python3 scripts/serve_api.py --port 8770 --quiet-if-running   # cron 감시용(이미 떠 있으면 조용히 종료)

보안
  - 조회(GET)만 있다. 주문·수정·삭제 기능 없음. DB는 읽기 전용(mode=ro)으로 연다.
  - 모든 요청에 토큰 필요: 헤더 `Authorization: Bearer <토큰>` 또는 `X-API-Token: <토큰>` (.env의 API_TOKEN)
  - 내부망(사설 IP)·localhost에서 온 요청만 받는다. 더 넓히려면 .env API_ALLOW_NETS=192.168.0.0/24,100.64.0.0/10
  - .env 비밀값은 어떤 응답에도 넣지 않는다.
응답은 모두 JSON(UTF-8). 시각은 KST 'YYYY-MM-DD HH:MM:SS'. 엔드포인트는 docs/api.md 참고.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news.analysis.categories import label as category_label  # noqa: E402
from stock_news.config import DATA_DIR, DB_PATH, SESSIONS, WORK_DIR, load_env, report_path, session_dir  # noqa: E402
from stock_news import preopen  # noqa: E402
from stock_news.report import SOURCE_NAMES, basis_labels, basis_tags, collect_status, resolve_sources  # noqa: E402
from stock_news.timewindow import is_trading_day, load_holidays, now_kst  # noqa: E402
from stock_news.tracking import after_prices  # noqa: E402


def next_trading_day(date: str) -> str:
    hol = load_holidays()
    d = dt.date.fromisoformat(date) + dt.timedelta(days=1)
    while not is_trading_day(d, hol):
        d += dt.timedelta(days=1)
    return d.isoformat()

API_VERSION = 1
DEFAULT_PORT = 8770
MAX_LIMIT = 500
NEWS_SOURCES = {"naver": "naver_focus_401", "policy": "korea_policy", "ministry": "ministry_press"}
DEFAULT_NETS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---------------------------------------------------------------- 공통
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def q1(params: dict, key: str, default=None):
    v = params.get(key)
    return v[0].strip() if v and v[0].strip() else default


def q_int(params: dict, key: str, default: int, lo: int = 0, hi: int = MAX_LIMIT) -> int:
    v = q1(params, key)
    if v is None:
        return default
    try:
        return max(lo, min(hi, int(v)))
    except ValueError:
        raise ApiError(400, f"{key}는 숫자여야 합니다") from None


def q_time(params: dict, key: str, end: bool = False) -> str | None:
    """'2026-11-13' 또는 '2026-11-13 07:00' → 비교용 문자열. to(end)에 날짜만 주면 그날 끝까지."""
    v = q1(params, key)
    if v is None:
        return None
    v = v.replace("T", " ")
    if not DATE_RE.match(v):
        raise ApiError(400, f"{key} 형식: YYYY-MM-DD 또는 'YYYY-MM-DD HH:MM'")
    if len(v) == 10:
        return v + (" 23:59:59" if end else " 00:00:00")
    return v if len(v) == 19 else v + (":59" if end else ":00")


def check_code(code: str) -> str:
    if not CODE_RE.match(code or ""):
        raise ApiError(400, "종목코드는 6자리")
    return code


def check_date(date: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
        raise ApiError(400, "날짜 형식: YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)          # 2026-13-99 같은 없는 날짜
    except ValueError:
        raise ApiError(400, "없는 날짜") from None
    return date


def page(total: int, limit: int, offset: int, items: list) -> dict:
    return {"total": total, "limit": limit, "offset": offset, "count": len(items), "items": items}


def source_label(src: str) -> str:
    return SOURCE_NAMES.get(src, src)


def article_out(r: sqlite3.Row, with_body: bool = False) -> dict:
    d = {"id": r["id"], "source": {v: k for k, v in NEWS_SOURCES.items()}.get(r["source"], "naver"),
         "source_name": source_label(r["source"]), "office": r["office_name"], "title": r["title"],
         "summary": r["summary"], "published_at": r["published_at"], "url": r["url"]}
    if with_body:
        d["body"] = r["body"] or ""
    return d


def disclosure_out(r: sqlite3.Row, with_body: bool = False) -> dict:
    d = {"rcept_no": r["rcept_no"], "received_at": r["received_at"],
         "rcept_date": f"{r['rcept_dt'][:4]}-{r['rcept_dt'][4:6]}-{r['rcept_dt'][6:8]}",
         "corp_name": r["corp_name"], "stock_code": r["stock_code"], "market": {"Y": "KOSPI", "K": "KOSDAQ",
         "N": "KONEX"}.get(r["corp_cls"], r["corp_cls"]), "report_nm": r["report_nm"], "filer": r["flr_nm"],
         "signal": r["signal"], "confirmed": bool(r["confirmed"]), "has_body": bool(r["body"]), "url": r["url"]}
    if with_body:
        d["body"] = r["body"] or ""
    return d


# ---------------------------------------------------------------- 채점 결과
TABLE = {"morning": "scores", "evening": "evening_scores"}     # 아침 장전 / 저녁 종가매매


def q_session(params: dict) -> str:
    s = q1(params, "session", "morning")
    if s not in SESSIONS:
        raise ApiError(400, "session: morning(아침 장전) / evening(저녁 종가매매)")
    return s


def _quote_from_row(r) -> dict | None:
    if r["entry_price"] is None and r["krx_close"] is None:
        return None
    return {"as_of": r["quote_time"], "krx_close": r["krx_close"], "price": r["entry_price"],
            "after_hours_pct": r["after_hours_pct"], "since_disclosure_pct": r["since_disclosure_pct"],
            "after_value": r["after_value"], "nxt_enabled": None if r["nxt_enabled"] is None else bool(r["nxt_enabled"])}


def load_scored(conn, date: str, session: str = "morning") -> dict | None:
    path = session_dir(date, session) / "scored.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = conn.execute(f"SELECT * FROM {TABLE[session]} WHERE date=?", (date,)).fetchall()
    if not rows:
        return None
    items = [{"id": "S-" + r["stock_code"], "code": r["stock_code"], "name": r["stock_name"],
              "sentiment": r["sentiment"], "final": r["final"], "final_raw": r["final_raw"],
              "material": r["material"], "category": r["category"], "factors": json.loads(r["factors"] or "{}"),
              "sources": json.loads(r["sources"] or "[]"),
              **({"evening": _quote_from_row(r)} if session == "evening" else {})} for r in rows]
    fb = session == "evening" and any(r["fallback"] for r in rows)
    return {"date": date, "session": session, "window": None, "items": items, "reviewed_policies": [], "fallback": fb}


def score_item(conn, it: dict, rank: int | None, d0: str | None = None) -> dict:
    factors = {}
    for k, f in (it.get("factors") or {}).items():
        factors[k] = None if f is None else {"score": f.get("score"), "reason": f.get("reason")}
    return {"rank": rank, "code": it["code"], "name": it.get("name"), "sentiment": it.get("sentiment"),
            "final": it.get("final"), "final_raw": it.get("final_raw"), "material": it.get("material"),
            "category": it.get("category"), "category_name": category_label(it.get("category")),
            "basis": basis_tags(conn, it), "basis_labels": basis_labels(conn, it),   # ["공시", "정책:산업부", …]
            "caution": it.get("caution"), "factors": factors,
            "sources": [{"label": n, "url": u} for n, u in resolve_sources(conn, it.get("sources", []))],
            "item_id": it.get("id"),
            # 저녁 종가매매: 리포트 시각 시세(KRX 종가·NXT 가격·시간외 등락·공시 후 등락·시간외 거래대금·NXT 가능)
            "evening_quote": it.get("evening"),
            # 이후 주가(D0 = 아침은 그날, 저녁은 다음 거래일) — 종목별 /api/stock 조회 없이 바로 쓰도록
            "after": after_prices(conn, it["code"], d0)}


def report(conn, date: str, session: str = "morning") -> dict:
    sc = load_scored(conn, check_date(date), session)
    if sc is None:
        raise ApiError(404, f"{date} {'저녁 종가매매' if session == 'evening' else '아침'} 채점 결과 없음")
    good = sorted([i for i in sc["items"] if i.get("sentiment") == "호재"],
                  key=lambda i: (-(i.get("final") or 0), -(i.get("final_raw") or 0)))
    bad = [i for i in sc["items"] if i.get("sentiment") == "악재"]
    warnings = []
    if sc.get("window"):
        _, warnings = collect_status(conn, sc["window"])
    d0 = date if session == "morning" else next_trading_day(date)
    return {"date": date, "session": session, "window": sc.get("window"), "weights_version": sc.get("weights_version"),
            "fallback": bool(sc.get("fallback")), "fallback_reason": sc.get("fallback_reason"),
            "warnings": warnings,
            "after_d0": d0,
            "bullish": [score_item(conn, it, n, d0) for n, it in enumerate(good, 1)],
            "bearish": [score_item(conn, it, None, d0) for it in bad],
            "reviewed_policies": sc.get("reviewed_policies", []),
            # 아침 = 그날 최종 전망, 저녁 = 다음 거래일 1차 전망(그 저녁 리포트에 실린 것)
            "theme_outlook": (theme_outlook(conn, date, "morning") if session == "morning" else
                              (lambda tf: _outlook_out(conn, tf) if tf else None)(
                                  _outlook_file(session_dir(date, "evening") / "candidates.json"))),
            # 장 전 추가(07:50·08:00): 아침 리포트 뒤 07:00~ 새 공시·정책만 채점. 아침 리포트에만 붙는다.
            # status: 실행 전 / 없음 / 있음 / 실패 — '없음'이면 RAS에 "장 전 추가 없음"
            "pre_open_additions": preopen.load(conn, date) if session == "morning" else None,
            "report_md_url": f"/api/report/{date}/markdown" + ("?session=evening" if session == "evening" else "")}


# ---------------------------------------------------------------- 조회 함수
def ep_health(conn, _p, _m) -> dict:
    last = conn.execute("SELECT MAX(published_at) FROM articles").fetchone()[0]
    last_score = conn.execute("SELECT MAX(date) FROM scores").fetchone()[0]
    last_evening = conn.execute("SELECT MAX(date) FROM evening_scores").fetchone()[0]
    last_theme = conn.execute("SELECT MAX(dt) FROM theme_daily").fetchone()[0]
    last_outlook = conn.execute("SELECT MAX(date) FROM theme_forecasts").fetchone()[0]
    sc = sorted(WORK_DIR.glob("*/scenario.json"))
    return {"ok": True, "api_version": API_VERSION, "now": f"{now_kst():%Y-%m-%d %H:%M:%S}",
            "latest_article": last, "latest_report": last_score, "latest_evening_report": last_evening,
            "latest_theme_day": last_theme, "latest_theme_outlook": last_outlook,
            "latest_scenario": sc[-1].parent.name if sc else None}


def ep_dates(conn, p, _m) -> dict:
    limit = q_int(p, "limit", 60, 1)
    tbl = TABLE[q_session(p)]
    rows = conn.execute(
        f"SELECT date, SUM(sentiment='호재') AS bullish, SUM(sentiment='악재') AS bearish FROM {tbl} "
        "GROUP BY date ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        top = conn.execute(f"SELECT stock_code, stock_name, final FROM {tbl} WHERE date=? AND sentiment='호재' "
                           "ORDER BY final DESC, final_raw DESC LIMIT 3", (r["date"],)).fetchall()
        out.append({"date": r["date"], "bullish": r["bullish"], "bearish": r["bearish"],
                    "top": [{"code": t["stock_code"], "name": t["stock_name"], "final": t["final"]} for t in top]})
    return {"items": out}


def ep_report(conn, p, m) -> dict:
    session = q_session(p)
    date = m[0] if m[0] != "latest" else conn.execute(f"SELECT MAX(date) FROM {TABLE[session]}").fetchone()[0]
    if not date:
        raise ApiError(404, "채점 결과 없음")
    return report(conn, date, session)


def ep_report_md(conn, p, m) -> dict:
    path = report_path(check_date(m[0]), q_session(p))
    if not path.exists():
        raise ApiError(404, "리포트 파일 없음")
    return {"date": m[0], "markdown": path.read_text(encoding="utf-8")}


def _stock_name(conn, code: str) -> str | None:
    r = conn.execute("SELECT name FROM stocks WHERE code=?", (code,)).fetchone()
    return r["name"] if r else None


def ep_news(conn, p, _m) -> dict:
    where, args = [], []
    src = q1(p, "source", "all")
    if src != "all":
        if src not in NEWS_SOURCES:
            raise ApiError(400, "source: all / naver / policy / ministry")
        where.append("source=?")
        args.append(NEWS_SOURCES[src])
    frm, to = q_time(p, "from"), q_time(p, "to", end=True)
    if not frm and not to and not q1(p, "q") and not q1(p, "stock"):
        frm = f"{now_kst() - dt.timedelta(days=2):%Y-%m-%d %H:%M:%S}"   # 기본: 최근 2일
    if frm:
        where.append("published_at>=?")
        args.append(frm)
    if to:
        where.append("published_at<=?")
        args.append(to)
    words = []
    if q1(p, "q"):
        words.append(q1(p, "q"))
    if q1(p, "stock"):
        name = _stock_name(conn, check_code(q1(p, "stock")))
        if not name:
            raise ApiError(404, "종목 없음")
        words.append(name)
    for w in words:
        where.append("(title LIKE ? OR summary LIKE ? OR body LIKE ?)")
        args += [f"%{w}%"] * 3
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    limit, offset = q_int(p, "limit", 100, 1), q_int(p, "offset", 0, 0, 10**9)
    total = conn.execute(f"SELECT COUNT(*) FROM articles{sql_where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM articles{sql_where} ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    return page(total, limit, offset, [article_out(r) for r in rows])


def ep_news_one(conn, _p, m) -> dict:
    r = conn.execute("SELECT * FROM articles WHERE id=?", (int(m[0]),)).fetchone()
    if not r:
        raise ApiError(404, "기사 없음")
    d = article_out(r, with_body=True)
    d["scored_stocks"] = _scored_for_source(conn, f"article:{r['id']}")
    return d


def _scored_for_source(conn, src: str) -> list[dict]:
    rows = conn.execute("SELECT date, stock_code, stock_name, sentiment, final FROM scores "
                        "WHERE sources LIKE ? ORDER BY date DESC", (f'%"{src}"%',)).fetchall()
    return [{"date": r["date"], "code": r["stock_code"], "name": r["stock_name"], "sentiment": r["sentiment"],
             "final": r["final"]} for r in rows]


def ep_disclosures(conn, p, _m) -> dict:
    where, args = [], []
    when = "COALESCE(received_at, substr(rcept_dt,1,4)||'-'||substr(rcept_dt,5,2)||'-'||substr(rcept_dt,7,2)||' 00:00:00')"
    if q1(p, "date"):
        d = check_date(q1(p, "date"))
        where.append("rcept_dt=?")
        args.append(d.replace("-", ""))
    frm, to = q_time(p, "from"), q_time(p, "to", end=True)
    if not (q1(p, "date") or frm or to or q1(p, "stock") or q1(p, "q")):
        frm = f"{now_kst() - dt.timedelta(days=2):%Y-%m-%d %H:%M:%S}"
    if frm:
        where.append(f"{when}>=?")
        args.append(frm)
    if to:
        where.append(f"{when}<=?")
        args.append(to)
    if q1(p, "stock"):
        where.append("stock_code=?")
        args.append(check_code(q1(p, "stock")))
    if q1(p, "q"):
        where.append("(report_nm LIKE ? OR corp_name LIKE ?)")
        args += [f"%{q1(p, 'q')}%"] * 2
    if q1(p, "major") in ("1", "true"):
        where.append("body IS NOT NULL AND body != ''")
    if q1(p, "listed_only", "1") in ("1", "true"):
        where.append("stock_code IS NOT NULL AND stock_code != ''")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    limit, offset = q_int(p, "limit", 100, 1), q_int(p, "offset", 0, 0, 10**9)
    total = conn.execute(f"SELECT COUNT(*) FROM disclosures{sql_where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM disclosures{sql_where} ORDER BY {when} DESC, rcept_no DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    return page(total, limit, offset, [disclosure_out(r) for r in rows])


def ep_disclosure_one(conn, _p, m) -> dict:
    r = conn.execute("SELECT * FROM disclosures WHERE rcept_no=?", (m[0],)).fetchone()
    if not r:
        raise ApiError(404, "공시 없음")
    d = disclosure_out(r, with_body=True)
    d["scored_stocks"] = _scored_for_source(conn, f"rcept:{r['rcept_no']}")
    return d


def ep_stock(conn, p, m) -> dict:
    code = check_code(m[0])
    s = conn.execute("SELECT code, name, market, sector, audit_info, state, is_listed, is_excluded, exclude_reason "
                     "FROM stocks WHERE code=?", (code,)).fetchone()
    if not s:
        raise ApiError(404, "종목 없음")
    scores = []
    for r in conn.execute("SELECT * FROM scores WHERE stock_code=? ORDER BY date DESC LIMIT 30", (code,)):
        rx = conn.execute("SELECT gap_pct, oc_pct, oc_excess, ret_1d, ret_3d, ret_5d, complete FROM price_reactions "
                          "WHERE date=? AND stock_code=?", (r["date"], code)).fetchone()
        scores.append({"date": r["date"], "sentiment": r["sentiment"], "final": r["final"],
                       "final_raw": r["final_raw"], "material": r["material"],
                       "category_name": category_label(r["category"]),
                       "reaction": dict(rx) if rx else None})
    themes = [{"source": t["source"], "theme": t["name"], "reason": t["reason"]} for t in conn.execute(
        "SELECT ts.source, th.name, ts.reason FROM theme_stocks ts JOIN themes th "
        "ON th.source=ts.source AND th.theme_code=ts.theme_code WHERE ts.stock_code=?", (code,))]
    disc = conn.execute("SELECT * FROM disclosures WHERE stock_code=? ORDER BY rcept_dt DESC, rcept_no DESC LIMIT 30",
                        (code,)).fetchall()
    news = conn.execute("SELECT * FROM articles WHERE title LIKE ? OR body LIKE ? ORDER BY published_at DESC LIMIT 30",
                        (f"%{s['name']}%",) * 2).fetchall()
    evening = [{"date": r["date"], "sentiment": r["sentiment"], "final": r["final"], "final_raw": r["final_raw"],
                "material": r["material"], "category_name": category_label(r["category"]), "fallback": bool(r["fallback"]),
                "quote": _quote_from_row(r),
                # 다음 거래일 반응 (진입가 = 리포트 시각 NXT 가격 대비) — 다음날 20:10 이후 채워짐
                "next_day": None if r["next_close"] is None else {
                    "open_ret": r["next_open_ret"], "high_ret": r["next_high_ret"], "close_ret": r["next_close_ret"]}}
               for r in conn.execute("SELECT * FROM evening_scores WHERE stock_code=? ORDER BY date DESC LIMIT 30", (code,))]
    return {"stock": {**dict(s), "is_listed": bool(s["is_listed"]), "is_excluded": bool(s["is_excluded"])},
            "themes": themes, "scores": scores, "evening_scores": evening,
            "disclosures": [disclosure_out(r) for r in disc], "news": [article_out(r) for r in news]}


def ep_stocks(conn, p, _m) -> dict:
    """종목 검색(자동완성용): 이름 일부·코드 일부. 정확히 일치 → 앞부분 일치 → 포함 순, 같은 순위면 이름 짧은 순."""
    q = q1(p, "q")
    if not q:
        raise ApiError(400, "q(검색어)가 필요합니다")
    q = q[:30]
    limit = q_int(p, "limit", 20, 1, 100)
    cond = "" if q1(p, "include_excluded") in ("1", "true") else " AND is_listed=1 AND is_excluded=0"
    rows = conn.execute(
        "SELECT code, name, market, sector, is_excluded, exclude_reason, "
        "  CASE WHEN name=? OR code=? THEN 0 WHEN name LIKE ? OR code LIKE ? THEN 1 ELSE 2 END AS rk "
        f"FROM stocks WHERE (name LIKE ? OR code LIKE ? OR REPLACE(name,' ','') LIKE ?){cond} "
        "ORDER BY rk, LENGTH(name), name LIMIT ?",
        (q, q.upper(), f"{q}%", f"{q.upper()}%", f"%{q}%", f"%{q.upper()}%", f"%{q.replace(' ', '')}%", limit)
    ).fetchall()
    items = []
    for r in rows:
        last = conn.execute("SELECT date, sentiment, final FROM scores WHERE stock_code=? ORDER BY date DESC LIMIT 1",
                            (r["code"],)).fetchone()
        items.append({"code": r["code"], "name": r["name"], "market": r["market"], "sector": r["sector"],
                      "is_excluded": bool(r["is_excluded"]), "exclude_reason": r["exclude_reason"],
                      "last_score": dict(last) if last else None})
    return {"count": len(items), "items": items}


PHASES = ("evening", "morning")


def _outlook_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    tf = json.loads(path.read_text(encoding="utf-8")).get("theme_forecast")
    return tf if tf and tf.get("prev_day") else None


def _outlook_out(conn, tf: dict) -> dict:
    out = {k: tf.get(k) for k in ("date", "phase", "made_at", "prev_day", "leaders_yday", "small_yday", "hedge",
                                  "forecast", "baseline", "track")}
    out["phase"] = out.get("phase") or "morning"
    out["approx"] = any(it.get("approx") for it in tf.get("leaders_yday") or [])
    out["results"] = [dict(r) for r in conn.execute(
        "SELECT phase, kind, dedupe, n, avg_excess, top10_hits, top10_ratio, leader_hits, leader_n, second_hits, second_n "
        "FROM theme_forecast_results WHERE date=? AND phase=? ORDER BY kind, dedupe DESC", (tf["date"], out["phase"]))]
    return out


def theme_outlook(conn, date: str, phase: str | None = None) -> dict | None:
    """예측 대상 거래일 date의 테마 전망. phase: evening(전날 19:30 1차, 종가매매 리포트) / morning(그날 07:00 최종).
    phase가 없으면 최종(아침)이 있으면 그것, 없으면 1차(저녁)."""
    from stock_news.timewindow import load_holidays, prev_trading_day
    paths = {"morning": session_dir(date, "morning") / "candidates.json",
             "evening": session_dir(f"{prev_trading_day(dt.date.fromisoformat(date), load_holidays()):%Y-%m-%d}",
                                    "evening") / "candidates.json"}
    for ph in ([phase] if phase else ["morning", "evening"]):
        tf = _outlook_file(paths[ph])
        if tf and tf.get("date") == date:
            return _outlook_out(conn, tf)
    return None


def ep_outlook(conn, p, m) -> dict:
    phase = q1(p, "phase")
    if phase and phase not in PHASES:
        raise ApiError(400, "phase는 evening 또는 morning")
    date = m[0]
    if date == "latest":
        date = conn.execute("SELECT MAX(date) FROM theme_forecasts" + (" WHERE phase=?" if phase else ""),
                            (phase,) if phase else ()).fetchone()[0]
        if not date:
            raise ApiError(404, "테마 전망 없음(평일 19:30 저녁 1차 · 07:00 아침 최종 뒤 생성)")
    out = theme_outlook(conn, check_date(date), phase)
    if not out:
        raise ApiError(404, "그날 테마 전망 없음")
    out["available_phases"] = [r[0] for r in conn.execute(
        "SELECT DISTINCT phase FROM theme_forecasts WHERE date=?", (date,))]
    return out


def ep_themes(conn, p, m) -> dict:
    """테마 묶음 집계(15:35) + 그날 아침 예측·채점. date=latest면 마지막 집계일."""
    date = m[0] if m[0] != "latest" else conn.execute("SELECT MAX(dt) FROM theme_daily").fetchone()[0]
    if not date:
        raise ApiError(404, "테마 집계 없음")
    limit = q_int(p, "limit", 30, 1, 400)
    rows = conn.execute("SELECT * FROM theme_daily WHERE dt=? AND rank IS NOT NULL ORDER BY rank LIMIT ?",
                        (date, limit)).fetchall()
    items = []
    for r in rows:
        ms = conn.execute("SELECT t.*, s.name FROM theme_members_daily t LEFT JOIN stocks s ON s.code=t.code "
                          "WHERE t.dt=? AND t.group_id=? AND t.label IS NOT NULL ORDER BY t.pos LIMIT 8",
                          (date, r["group_id"])).fetchall()
        d = {k: r[k] for k in r.keys() if k not in ("dt",)}
        d["members"] = [{"code": x["code"], "name": x["name"], "label": x["label"], "change_pct": x["change_pct"],
                         "limit_hit": x["limit_hit"], "first_hit": x["first_hit"], "trade_value": x["trade_value"]}
                        for x in ms]
        items.append(d)
    fc = [dict(r) for r in conn.execute("SELECT * FROM theme_forecasts WHERE date=? AND dedupe=1 ORDER BY kind, rank",
                                         (date,))]
    res = [dict(r) for r in conn.execute("SELECT * FROM theme_forecast_results WHERE date=?", (date,))]
    for f in fc:
        for k in ("components", "second_picks", "related"):
            try:
                f[k] = json.loads(f[k]) if f[k] else None
            except ValueError:
                pass
    return {"date": date, "approx": bool(rows and rows[0]["approx"]), "count": len(items), "items": items,
            "forecasts": fc, "results": res}


def ep_scenario(conn, p, m) -> dict:
    """일일 손익 시나리오(평일 16:10) — 추천·진입 비교·기간 표. date=latest면 마지막 생성일."""
    date = m[0]
    if date == "latest":
        files = sorted(WORK_DIR.glob("*/scenario.json"))
        if not files:
            raise ApiError(404, "손익 시나리오 없음(평일 16:10 생성)")
        f = files[-1]
    else:
        f = WORK_DIR / check_date(date) / "scenario.json"
        if not f.exists():
            raise ApiError(404, "그날 손익 시나리오 없음")
    out = json.loads(f.read_text(encoding="utf-8"))
    rows = out.get("trades") or []
    td, code = q1(p, "trade_date"), q1(p, "code")
    if td:
        check_date(td)
        rows = [r for r in rows if r["date"] == td]
    if code:
        rows = [r for r in rows if r["code"] == check_code(code)]
    if q1(p, "trades") == "0":
        out.pop("trades", None)
        return out
    if q1(p, "bars") == "1":                     # 재계산용: 진입 봉부터 15:30까지 1분봉 [hhmm, 시, 고, 저, 종, 거래량]
        from stock_news import minute_bars
        from stock_news.analysis import scenario
        cfg = scenario.load_config()
        md_all = minute_bars.load(conn, sorted({r["date"] for r in rows}))
        for r in rows:
            md = md_all.get((r["date"], r["code"]))
            if not md:
                r["bars"] = None
                continue
            e = scenario.make_entries({**md, "krx_close": r["close"], "prev_close": r.get("prev_close") or md["prev_close"]},
                                      cfg).get(r["entry"]) or {}
            r["bars"] = e.get("bars")
    out["trades"] = rows
    return out


ROUTES: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"^/api/stocks$"), ep_stocks),
    (re.compile(r"^/api/themes/(latest|\d{4}-\d{2}-\d{2})$"), ep_themes),
    (re.compile(r"^/api/theme-outlook/(latest|\d{4}-\d{2}-\d{2})$"), ep_outlook),
    (re.compile(r"^/api/scenario/(latest|\d{4}-\d{2}-\d{2})$"), ep_scenario),
    (re.compile(r"^/api/health$"), ep_health),
    (re.compile(r"^/api/dates$"), ep_dates),
    (re.compile(r"^/api/report/(latest|\d{4}-\d{2}-\d{2})$"), ep_report),
    (re.compile(r"^/api/report/(\d{4}-\d{2}-\d{2})/markdown$"), ep_report_md),
    (re.compile(r"^/api/news$"), ep_news),
    (re.compile(r"^/api/news/(\d+)$"), ep_news_one),
    (re.compile(r"^/api/disclosures$"), ep_disclosures),
    (re.compile(r"^/api/disclosures/(\d{14})$"), ep_disclosure_one),
    (re.compile(r"^/api/stock/([0-9A-Za-z]{6})$"), ep_stock),
]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "stock-news-api/1"
    token: str = ""
    nets: list = []
    log_file = None

    def log_message(self, fmt, *args):  # 접속 기록(토큰은 남기지 않음)
        if self.log_file:
            self.log_file.write(f"[{now_kst():%Y-%m-%d %H:%M:%S}] {self.client_address[0]} {fmt % args}\n")
            self.log_file.flush()

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _allowed_ip(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.nets)

    def _authorized(self) -> bool:
        got = self.headers.get("X-API-Token", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        return bool(self.token) and hmac.compare_digest(got.encode(), self.token.encode())

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed_ip():
            return self._send(403, {"error": "허용되지 않은 네트워크"})
        if not self._authorized():
            return self._send(401, {"error": "토큰 없음 또는 불일치"})
        url = urlparse(self.path)
        params = parse_qs(url.query)
        for pat, fn in ROUTES:
            m = pat.match(url.path.rstrip("/") or "/")
            if m:
                try:
                    with connect() as conn:
                        return self._send(200, fn(conn, params, m.groups()))
                except ApiError as e:
                    return self._send(e.status, {"error": e.message})
                except Exception:  # noqa: BLE001
                    self.log_message("%s", traceback.format_exc().replace("\n", " | "))
                    return self._send(500, {"error": "서버 오류(로그 확인)"})
        return self._send(404, {"error": "없는 경로", "paths": [r.pattern for r, _ in ROUTES]})

    def _method_not_allowed(self) -> None:
        self._send(405, {"error": "조회(GET)만 지원"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None, help="기본 .env API_HOST 또는 0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help=f"기본 .env API_PORT 또는 {DEFAULT_PORT}")
    ap.add_argument("--quiet-if-running", action="store_true", help="포트가 이미 사용 중이면 조용히 종료(cron 감시용)")
    a = ap.parse_args()
    load_env()
    token = os.environ.get("API_TOKEN", "").strip()
    if len(token) < 20:
        print(".env에 API_TOKEN(20자 이상)이 없습니다", file=sys.stderr)
        return 1
    host = a.host or os.environ.get("API_HOST", "0.0.0.0")
    port = a.port or int(os.environ.get("API_PORT", DEFAULT_PORT))
    Handler.token = token
    Handler.nets = [ipaddress.ip_network(n.strip()) for n in
                    os.environ.get("API_ALLOW_NETS", DEFAULT_NETS).split(",") if n.strip()]
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    Handler.log_file = (DATA_DIR / "logs" / "api.log").open("a", encoding="utf-8")
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE and a.quiet_if_running:
            return 0
        raise
    msg = f"API 시작 http://{host}:{port}  허용 네트워크 {', '.join(map(str, Handler.nets))}"
    print(msg, flush=True)
    Handler.log_file.write(f"[{now_kst():%Y-%m-%d %H:%M:%S}] {msg}\n")
    Handler.log_file.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
