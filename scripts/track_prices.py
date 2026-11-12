#!/usr/bin/env python3
"""장 마감 후: 리포트 종목의 주가 반응 기록 (price_reactions). cron 평일 16:10.

    python3 scripts/track_prices.py                 # 아직 다 안 채워진 날짜 전부(최근 5거래일 수익률 포함)
    python3 scripts/track_prices.py --date 2026-11-12
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import load_env  # noqa: E402
from stock_news.master.kiwoom_client import KiwoomClient  # noqa: E402
from stock_news.tracking import track  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", action="append", help="특정 날짜만 (여러 번 지정 가능)")
    a = ap.parse_args()
    load_env()
    conn = db.connect()
    st = track(conn, KiwoomClient(), a.date)
    print(f"반응 기록: {st['dates']}일 · {st['rows']}건" + (f" · 건너뜀 {', '.join(st['skipped'])}" if st["skipped"] else ""))
    for r in conn.execute(
            "SELECT p.date, s.stock_name, p.final, p.gap_pct, p.oc_pct, p.oc_excess, p.ret_5d FROM price_reactions p "
            "JOIN scores s ON s.date=p.date AND s.stock_code=p.stock_code WHERE p.grp='scored' "
            "ORDER BY p.date DESC, p.final_raw DESC LIMIT 12"):
        f = lambda v: "-" if v is None else f"{v * 100:+.1f}%"  # noqa: E731
        print(f"  {r[0]} {r[1]:<10} {r[2] if r[2] is not None else '악재'}점 갭 {f(r[3])} 시가→종가 {f(r[4])} "
              f"초과 {f(r[5])} 5일 {f(r[6])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
