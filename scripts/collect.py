#!/usr/bin/env python3
"""1단계: 뉴스(네이버 시황·전망) + 정책뉴스(korea.kr) + DART 공시 수집 → SQLite → data/work/날짜/ 덤프.

    python3 scripts/collect.py                      # 직전 거래일 15:30 ~ 지금
    python3 scripts/collect.py --since "2026-11-11 15:30" --until "2026-11-12 07:00"
    python3 scripts/collect.py --only news --no-body
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import load_env  # noqa: E402
from stock_news.export import export_window  # noqa: E402
from stock_news.timewindow import Window, default_window, now_kst  # noqa: E402


def _parse(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="시작 'YYYY-MM-DD HH:MM' (기본: 직전 거래일 15:30)")
    ap.add_argument("--until", help="끝 'YYYY-MM-DD HH:MM' (기본: 지금)")
    ap.add_argument("--only", choices=["news", "policy", "dart"], help="한 소스만")
    ap.add_argument("--no-body", action="store_true", help="본문 수집 생략(빠른 확인용)")
    a = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    end = _parse(a.until) if a.until else now_kst()
    window = Window(_parse(a.since), end) if a.since else default_window(end)
    print(f"수집 시간대: {window}\n")

    conn = db.connect()
    results = []
    if a.only in (None, "news"):
        from stock_news.collectors import naver_focus
        results.append(naver_focus.collect(conn, window, fetch_bodies=not a.no_body))
    if a.only in (None, "policy"):
        from stock_news.collectors import korea_policy
        results.append(korea_policy.collect(conn, window, fetch_bodies=not a.no_body))
    if a.only in (None, "dart"):
        from stock_news.collectors import dart
        results.append(dart.collect(conn, window, fetch_bodies=not a.no_body))

    out = export_window(conn, window, results)
    warnings = [w for r in results for w in r["warnings"]]
    print(f"\n분석용 덤프: {out}")
    if warnings:
        print(f"\n⚠️  경고 {len(warnings)}건:")
        for w in warnings:
            print(f"  - {w}")
        return 2
    print("경고 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
