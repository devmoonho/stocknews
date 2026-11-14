#!/usr/bin/env python3
"""3단계 준비: 수집 결과를 종목·테마에 매칭해 data/work/날짜/candidates.json 생성 (Claude 채점 입력).

    python3 scripts/match.py                       # 직전 거래일 15:30 ~ 지금
    python3 scripts/match.py --since "2026-11-11 15:30" --until "2026-11-12 07:00"
    python3 scripts/match.py ... --out data/work/2026-11-13_v2   # 다른 폴더에 (기존 결과를 덮어쓰지 않고 시험)
    python3 scripts/match.py --no-prices                          # 일봉 조회 생략(선반영 점수 없음)

후보 종목의 일봉(시가총액·선반영 계산용)을 키움에서 받아 DB에 캐시한다(처음엔 1~2분).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.analysis.candidates import build  # noqa: E402
from stock_news.analysis.digest import render as render_digest  # noqa: E402
from stock_news.config import ROOT, WORK_SESSIONS, load_env, session_dir  # noqa: E402
from stock_news.timewindow import load_holidays, prev_trading_day  # noqa: E402
from stock_news.prices import ensure_daily  # noqa: E402
from stock_news.timewindow import Window, default_window, now_kst  # noqa: E402


def next_trading_day(d: dt.date) -> dt.date:
    from stock_news.timewindow import is_trading_day
    hol = load_holidays()
    d += dt.timedelta(days=1)
    while not is_trading_day(d, hol):
        d += dt.timedelta(days=1)
    return d


def attach_evening_quotes(conn, result: dict, date: str) -> int:
    """공시·제목 기사가 있는 후보 종목에 1분봉 시세(오늘 KRX 종가·지금 NXT 가격·시간외 등락)를 붙이고 선반영을 다시 계산."""
    from stock_news import prices
    from stock_news.master.kiwoom_client import KiwoomClient
    try:
        client = KiwoomClient()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  시간외 시세 조회 불가: {e}")
        return 0
    info = {r["code"]: r for r in conn.execute("SELECT code, audit_info, order_warning, raw FROM stocks")}
    n = 0
    for s in result["stocks"]:
        if not (s["disclosures"] or s["objective"]["diffusion"]["title_mentions"]):
            continue
        first = min((d["received_at"] for d in s["disclosures"] if d.get("received_at") and len(d["received_at"]) > 10),
                    default=None)
        try:
            q = prices.fetch_evening_quote(client, s["code"], date, first)
        except Exception:  # noqa: BLE001
            continue
        if not q:
            continue
        raw = json.loads(info[s["code"]]["raw"] or "{}") if s["code"] in info else {}
        q["nxt_enabled"] = raw.get("nxtEnable") == "Y" or q.get("nxt_traded", False)
        s["evening"] = q
        ctx = prices.evening_context(conn, s["code"], date, q)
        s["objective"]["preprice"] = prices.preprice(ctx, info[s["code"]]["audit_info"], info[s["code"]]["order_warning"])
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since"), ap.add_argument("--until")
    ap.add_argument("--out", help="결과 폴더 (기본 data/work/리포트날짜)")
    ap.add_argument("--no-prices", action="store_true", help="일봉 조회 생략")
    ap.add_argument("--no-quotes", action="store_true", help="저녁: 시간외 시세(1분봉) 생략 — 19:10 사전 수집용")
    ap.add_argument("--session", choices=WORK_SESSIONS, default="morning",
                    help="evening = 저녁 종가매매(당일 15:30~지금, 공시·기사 종목의 시간외 시세 포함) / "
                         "preopen = 장 전 추가(--since 아침 수집 끝 시각, 테마 전망 없음)")
    a = ap.parse_args()
    load_env()
    p = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
    end = p(a.until) if a.until else now_kst()
    window = Window(p(a.since), end) if a.since else default_window(end)

    conn = db.connect()
    if not conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]:
        print("⚠️  종목 마스터가 비어 있습니다 — 먼저 scripts/update_master.py 실행")
        return 2
    result = build(conn, window)
    warn = 0
    if not a.no_prices:
        codes = [s["code"] for s in result["stocks"]]
        # 저녁엔 오늘 통합 봉이 20:00까지 미완성 → 어제까지만 받고, 오늘은 1분봉 시세로
        upto = f"{prev_trading_day(window.end.date(), load_holidays()):%Y-%m-%d}" if a.session in ("evening", "preopen") \
            else f"{window.start:%Y-%m-%d}"
        failed = ensure_daily(conn, codes, upto, log=lambda m: None)
        if failed:
            warn = 2
            print(f"⚠️  일봉 조회 실패 {len(failed)}/{len(codes)}종목 — 그 종목은 선반영 점수 없음, 시총은 마스터 가격 사용")
        result = build(conn, window)              # 가격을 반영해 다시 계산(1초 안팎)
    if not a.out and a.session == "morning":      # 후속 이벤트 상태(followups)는 아침 실제 실행에서만 갱신
        result = build(conn, window, write_followups=True)
    result["session"] = a.session
    # 테마 전망: 직전 15:35 테마 집계 + 재료 → 예측 대상 거래일의 주목 테마
    #   아침 = 오늘(최종, 밤사이 재료 전부) · 저녁 = 다음 거래일(1차, 15:30 이후 재료) — 종가매매 리포트에 넣는다
    if a.session != "preopen":               # 장 전 추가는 테마 전망 없음(아침 최종 전망 그대로)
        from stock_news.analysis import theme_forecast
        if a.session == "evening":
            target, phase = next_trading_day(window.end.date()), "evening"
        else:
            target, phase = window.report_date, "morning"
        try:
            result["theme_forecast"] = theme_forecast.forecast(conn, f"{target:%Y-%m-%d}", result,
                                                               save=not a.out, phase=phase)
            tf = result["theme_forecast"]
            print(f"테마 전망({'저녁 1차' if phase == 'evening' else '아침 최종'}, 대상 {target:%Y-%m-%d}): "
                  f"기준 집계 {tf['prev_day'] or '없음'} · 주도 {len(tf['leaders_yday'])} · 예측 {len(tf['forecast'])}")
        except Exception as e:  # noqa: BLE001 — 테마 전망 실패로 리포트를 막지 않는다
            result["theme_forecast"] = None
            warn = 2
            print(f"⚠️  테마 전망 계산 실패: {e!r}")
    if a.session == "evening" and not a.no_prices and not a.no_quotes:
        n = attach_evening_quotes(conn, result, f"{window.end:%Y-%m-%d}")
        print(f"시간외 시세: {n}종목 (공시·제목 기사 종목)")
    out = (ROOT / a.out) if a.out and not Path(a.out).is_absolute() else Path(a.out) if a.out else \
        session_dir(f"{window.report_date:%Y-%m-%d}", a.session)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "candidates.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    digest = out / "candidates_digest.md"
    digest.write_text(render_digest(result), encoding="utf-8")
    c = result["counts"]
    print(f"시간대 {window}")
    print(f"기사 {c['articles']}건 · 공시 {c['disclosures_listed']}건 → 후보 종목 {c['stocks']}개, "
          f"테마 신호 {c['theme_signals']}개, 정책 기사 {c['policy_articles']}건, 제외 종목 언급 {c['excluded_mentions']}개")
    print(f"→ {path} ({path.stat().st_size // 1024} KB)")
    print(f"→ {digest} ({digest.stat().st_size // 1024} KB) ← Claude가 읽을 파일")
    return warn


if __name__ == "__main__":
    sys.exit(main())
