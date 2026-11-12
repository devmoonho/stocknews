#!/usr/bin/env python3
"""2단계: 종목 마스터 갱신 — 전 종목 + 제외 플래그(키움), 테마-종목 매핑(키움 + 네이버).

    python3 scripts/update_master.py              # 전부
    python3 scripts/update_master.py --only naver_themes
    python3 scripts/update_master.py --skip-if-fresh   # 오늘 이미 갱신했으면 건너뜀
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import load_env  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def updated_today(conn, source: str) -> bool:
    row = conn.execute("SELECT MAX(started_at) FROM collect_runs WHERE source=? AND fetched > 0", (source,)).fetchone()
    return bool(row[0]) and row[0][:10] == f"{now_kst():%Y-%m-%d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["stocks", "themes", "naver_themes"])
    ap.add_argument("--skip-if-fresh", action="store_true")
    a = ap.parse_args()
    load_env()

    from stock_news.master import naver_themes, stocks, themes
    from stock_news.master.kiwoom_client import KiwoomClient

    conn = db.connect()
    client = None
    results = []
    for name, mod, needs_kiwoom in (("stocks", stocks, True), ("themes", themes, True),
                                    ("naver_themes", naver_themes, False)):
        if a.only and a.only != name:
            continue
        if a.skip_if_fresh and updated_today(conn, mod.SOURCE):
            print(f"[{mod.SOURCE}] 오늘 이미 갱신됨 — 건너뜀")
            continue
        if needs_kiwoom and client is None:
            client = KiwoomClient()
        results.append(mod.update(conn, client))

    warnings = [w for r in results for w in r["warnings"]]
    if warnings:
        print(f"\n⚠️  경고 {len(warnings)}건:")
        for w in warnings:
            print(f"  - {w}")
        return 2
    print("\n경고 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
