#!/usr/bin/env python3
"""1회용: 테마 편입 종목 전체의 일봉(키움 ka10081)을 받아 둔다 — 테마 집계의 거래대금 20일 평균·과거 재구성용.

    python3 scripts/backfill_trade_value.py            # 테마 편입 종목 약 2,400개, 약 35분
    python3 scripts/backfill_trade_value.py --limit 50 # 시험

일봉은 KRX+NXT 통합이라 거래대금은 거래량×종가 근사다(리포트에 '근사' 표시).
그 뒤 `python3 scripts/theme_daily.py --rebuild 날짜`로 과거 테마 집계를 근사 재구성할 수 있다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import load_env  # noqa: E402
from stock_news.prices import ensure_daily  # noqa: E402
from stock_news.timewindow import load_holidays, now_kst, prev_trading_day  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--asof", help="이 날짜까지 받기 (기본: 통합 봉이 완성된 마지막 거래일)")
    a = ap.parse_args()
    load_env()
    conn = db.connect()
    now = now_kst()
    asof = a.asof or (f"{now:%Y-%m-%d}" if now.hour >= 21 or (now.hour == 20 and now.minute >= 5)
                      else f"{prev_trading_day(now.date(), load_holidays()):%Y-%m-%d}")
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT t.stock_code FROM theme_stocks t JOIN stocks s ON s.code=t.stock_code "
        "WHERE s.is_listed=1 AND s.is_excluded=0 ORDER BY t.stock_code")]
    if a.limit:
        codes = codes[:a.limit]
    print(f"{len(codes)}종목 일봉 → {asof}까지", flush=True)
    t0, failed = time.monotonic(), []
    for i in range(0, len(codes), 20):          # 20종목마다 저장 — DB를 오래 잠그지 않게
        failed += ensure_daily(conn, codes[i:i + 20], asof, log=lambda m: None)
        if (i // 20) % 5 == 4 or i + 20 >= len(codes):
            print(f"  {min(i + 20, len(codes))}/{len(codes)} ({time.monotonic() - t0:.0f}초, 실패 {len(failed)})", flush=True)
    print(f"끝 — 실패 {len(failed)}종목{': ' + ', '.join(failed[:20]) if failed else ''}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
