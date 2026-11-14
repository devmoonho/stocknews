#!/usr/bin/env python3
"""일일 손익 시나리오 리포트 — 장 마감 후(cron 평일 16:10) 장전 리포트 종목 전체를 1분봉으로 시뮬레이션.

    python3 scripts/scenario_daily.py                    # 오늘: 분봉 받기 → 시뮬레이션 → 리포트 → 메일·텔레그램
    python3 scripts/scenario_daily.py --dry-run          # 저장·발송 없이 미리보기(data/work/날짜/scenario_preview.md·_email.html)
    python3 scripts/scenario_daily.py --no-send          # 저장은 하고 발송만 생략
    python3 scripts/scenario_daily.py --date 2026-11-13 --force
    python3 scripts/scenario_daily.py --no-fetch         # 키움 요청 없이 이미 받은 분봉으로만
대상: 아침 리포트 채점 종목(호재·악재) + 비교 기준 후보(tracking._targets). 기준: config/scenario.json.
결과: reports/scenario/날짜.md, data/work/날짜/scenario.json, DB minute_bars·scenario_recs. 주문은 하지 않는다.
※ 실거래를 제어하는 봇(AutoInvest QuoVestor)의 토큰은 쓰지 않는다(기존 TELEGRAM_* 사용).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_news import db, minute_bars  # noqa: E402
from stock_news.analysis import scenario  # noqa: E402
from stock_news.config import REPORTS_DIR, WORK_DIR, load_env  # noqa: E402
from stock_news.timewindow import is_trading_day, load_holidays, now_kst  # noqa: E402
from stock_news.tracking import _targets  # noqa: E402

FETCH_DAYS = 70          # 달력일 기준 — 키움 1분봉 연속조회로 받을 수 있는 범위(약 50거래일) 안만 요청


def trading_days(start: str, end: str, hol) -> list[str]:
    d, e, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d <= e:
        if is_trading_day(d, hol):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def krx_closes(conn, d: str, next_day: str | None) -> dict[str, float]:
    """그날 KRX 종가: ① 15:35 실측 스냅샷(market_daily, approx=0) ② 다음 거래일 일봉 기준가(base_price = 전일 KRX 종가).
    근사 재구성(approx=1) 스냅샷은 쓰지 않는다. 둘 다 없으면 분봉 마지막 봉(15:30 봉이 빠진 종목 있음)."""
    out = {}
    if next_day:
        out.update({r[0]: r[1] for r in conn.execute(
            "SELECT code, base_price FROM price_daily WHERE dt=? AND base_price>0", (next_day,))})
    out.update({r[0]: r[1] for r in conn.execute(
        "SELECT code, close FROM market_daily WHERE dt=? AND close>0 AND COALESCE(approx,0)=0", (d,))})
    return out


def build_trades(conn, dates: list[str], bars: dict, cfg: dict, days: list[str], log) -> list[dict]:
    names = {r[0]: r[1] for r in conn.execute("SELECT code, name FROM stocks")}
    trades, missing = [], 0
    for d in dates:
        prev_day = max((x for x in days if x < d), default=None)
        next_day = min((x for x in days if x > d), default=None)
        today = krx_closes(conn, d, next_day)
        prev = krx_closes(conn, prev_day, d) if prev_day else {}
        for t in _targets(conn, d):
            md = bars.get((d, t["code"]))
            if not md or not md["KRX"]:
                missing += 1
                continue
            md = {**md, "krx_close": today.get(t["code"]), "prev_close": prev.get(t["code"]) or md["prev_close"]}
            trades.append({"date": d, "code": t["code"], "name": names.get(t["code"], t["code"]), "grp": t["grp"],
                           "sentiment": t["sentiment"], "final": t["final"], "basis": t["basis"],
                           "prev_close": md["prev_close"],
                           "entries": scenario.make_entries(md, cfg)})
    if missing:
        log(f"분봉 없는 종목 {missing}건 제외(거래정지·조회 범위 밖 등)")
    return trades


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--dry-run", action="store_true", help="저장·발송 없이 미리보기")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--no-fetch", action="store_true", help="키움 요청 없이 저장된 분봉만")
    ap.add_argument("--force", action="store_true", help="휴장일이어도 실행")
    a = ap.parse_args()
    load_env()
    log = lambda m: print(m, flush=True)  # noqa: E731
    hol = load_holidays()
    day = dt.date.fromisoformat(a.date)
    if not a.force and not is_trading_day(day, hol):
        log(f"{a.date} 휴장일 — 실행 안 함")
        return 0
    conn = db.connect()
    log(f"===== 손익 시나리오 {a.date} 시작 {now_kst():%H:%M:%S} =====")
    try:
        cfg = scenario.load_config()
        dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM scores WHERE date<=? ORDER BY date", (a.date,))]
        if not dates:
            log("채점 기록 없음 — 종료")
            return 0
        if not a.no_fetch:
            from stock_news.master.kiwoom_client import KiwoomClient
            cutoff = (day - dt.timedelta(days=FETCH_DAYS)).isoformat()
            wants: dict[str, set[str]] = {}
            for d in dates:
                if d >= cutoff:
                    for t in _targets(conn, d):
                        wants.setdefault(t["code"], set()).add(d)
            t0 = time.monotonic()
            st = minute_bars.ensure(conn, KiwoomClient(), wants, log)
            log(f"분봉: 종목 {st['codes']} · 저장 {st['rows']}행 · 실패 {st['calls_failed']} · 범위 밖 {st['too_old']} "
                f"({time.monotonic() - t0:.0f}초)")
        bars = minute_bars.load(conn, dates)
        days = trading_days(dates[0], a.date, hol)
        allday = trading_days((dt.date.fromisoformat(dates[0]) - dt.timedelta(days=10)).isoformat(),
                              (day + dt.timedelta(days=10)).isoformat(), hol)
        trades = build_trades(conn, dates, bars, cfg, allday, log)
        t0 = time.monotonic()
        res = scenario.analyze(trades, days, cfg, scenario.load_prev_recs(conn, a.date))
        log(f"시뮬레이션 {len(trades)}건 × 조합 {res['n_combos']:,}개 ({time.monotonic() - t0:.1f}초)")
        md = scenario.render(res, a.date)
        work = WORK_DIR / a.date
        work.mkdir(parents=True, exist_ok=True)
        title = f"[손익 시나리오] {a.date} 표본 {res['n_trades']}건"
        tg_text = scenario.telegram_text(res, a.date)
        if a.dry_run:
            from stock_news.mailer import md_to_email_html
            (work / "scenario_preview.md").write_text(md, encoding="utf-8")
            (work / "scenario_email.html").write_text(md_to_email_html(md, title), encoding="utf-8")
            log(f"미리보기: {work / 'scenario_preview.md'} · {work / 'scenario_email.html'}")
            log("--- 텔레그램 ---\n" + tg_text + f"\n--- {len(tg_text)}자")
            return 0
        out_dir = REPORTS_DIR / "scenario"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{a.date}.md"
        path.write_text(md, encoding="utf-8")
        out = {"date": a.date, **scenario.to_json(res), "trades": scenario.trade_rows(trades, res, cfg)}
        (work / "scenario.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        scenario.save_recs(conn, a.date, res, f"{now_kst():%Y-%m-%d %H:%M:%S}")
        log(f"리포트: {path}")
        if a.no_send:
            return 0
        if os.environ.get("MAIL_SMTP_PASSWORD"):
            from stock_news.mailer import md_to_email_html, send
            try:
                send(title, md_to_email_html(md, title), md)
                log("✅ 메일 발송")
            except Exception as e:  # noqa: BLE001
                log(f"❌ 메일 실패: {e}")
        else:
            log("MAIL_SMTP_PASSWORD 없음 — 메일 건너뜀")
        from stock_news.telegram_notify import TelegramNotifier
        tg = TelegramNotifier()
        if tg.configured:
            log("✅ 텔레그램 전송" if tg.send(tg_text) else "❌ 텔레그램 실패")
        else:
            log("텔레그램 설정 없음 — 건너뜀")
        log(f"===== 끝 {now_kst():%H:%M:%S} =====")
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"✖ 손익 시나리오 실패: {e!r}")
        if not a.dry_run:
            from morning_auto import alert
            alert(f"❌ 손익 시나리오 실패 ({a.date})\n{e!r}"[:600])
        return 1


if __name__ == "__main__":
    sys.exit(main())
