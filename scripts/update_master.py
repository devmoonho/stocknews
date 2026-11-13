#!/usr/bin/env python3
"""2단계: 종목 마스터 갱신 — 전 종목 + 제외 플래그(키움), 테마-종목 매핑(키움 + 네이버).

    python3 scripts/update_master.py              # 전부
    python3 scripts/update_master.py --only naver_themes
    python3 scripts/update_master.py --skip-if-fresh   # 아직 새로 받을 때가 아니면 건너뜀 (아침 자동 실행)

갱신 주기(--skip-if-fresh일 때)
  - 키움 종목·테마: 매일(오늘 받았으면 건너뜀) — 거래정지·관리종목 등 제외 플래그가 매일 바뀔 수 있음
  - 네이버 테마: 주 1회(이번 주 월요일 이후 받았으면 건너뜀 → 보통 월요일 아침, 월요일이 휴장이면 그 주 첫 거래일)
    테마 구성은 하루 사이에 거의 바뀌지 않고, 네이버 요청(하루 약 270회)을 줄이기 위해 (2026-11-13 사용자 결정)
    바로 받으려면: --only naver_themes
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import load_env  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


WEEKLY = {"naver_themes"}          # 주 1회 갱신하는 출처 (나머지는 매일)


def last_update(conn, source: str) -> str | None:
    row = conn.execute("SELECT MAX(started_at) FROM collect_runs WHERE source=? AND fetched > 0", (source,)).fetchone()
    return row[0]


def is_fresh(conn, source: str, today: dt.date | None = None) -> bool:
    """매일 출처는 오늘 받았으면, 주 1회 출처는 이번 주 월요일 이후 받았으면 '새로 받을 필요 없음'."""
    today = today or now_kst().date()
    last = last_update(conn, source)
    if not last:
        return False
    since = today - dt.timedelta(days=today.weekday()) if source in WEEKLY else today
    return last[:10] >= f"{since:%Y-%m-%d}"


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
        if a.skip_if_fresh and is_fresh(conn, mod.SOURCE):
            when = "이번 주" if mod.SOURCE in WEEKLY else "오늘"
            print(f"[{mod.SOURCE}] {when} 이미 갱신됨({last_update(conn, mod.SOURCE)[:16]}) — 건너뜀")
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
