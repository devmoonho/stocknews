#!/usr/bin/env python3
"""1단계: 뉴스(네이버 시황·전망) + 정책뉴스(korea.kr) + 부처 보도자료(11개 부처) + DART 공시 수집 → SQLite → data/work/날짜/ 덤프.

    python3 scripts/collect.py                      # 직전 거래일 15:30 ~ 지금
    python3 scripts/collect.py --since "2026-11-11 15:30" --until "2026-11-12 07:00"
    python3 scripts/collect.py --only news --no-body
    python3 scripts/collect.py --only ministry      # 부처 보도자료만
    python3 scripts/collect.py --snapshot           # 15:30 부처 목록 스냅샷(cron) — 날짜만 있는 부처의 게시 시각 판정용
    python3 scripts/collect.py --probe 3            # 부처 파서 점검: 최근 3일 목록·상세를 파싱만(DB에 안 씀)
    python3 scripts/collect.py --since "2026-11-16 07:03" --sources policy,ministry,dart --session preopen
                                                    # 장 전 추가(07:45): 네이버 뉴스 없이, 덤프는 data/work/날짜_preopen
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
    ap.add_argument("--only", choices=["news", "policy", "ministry", "dart"], help="한 소스만")
    ap.add_argument("--sources", help="여러 소스: news,policy,ministry,dart (--only 대신)")
    ap.add_argument("--session", default="morning", help="덤프 폴더: preopen이면 data/work/날짜_preopen(아침 덤프를 덮어쓰지 않음)")
    ap.add_argument("--no-body", action="store_true", help="본문 수집 생략(빠른 확인용)")
    ap.add_argument("--ministries", help="부처 보도자료 일부만: motir,msit,… (기본 전체)")
    ap.add_argument("--snapshot", action="store_true", help="부처 보도자료 15:30 목록 스냅샷만")
    ap.add_argument("--probe", type=int, metavar="DAYS", help="부처 파서 점검(최근 DAYS일, DB에 안 씀)")
    a = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    only_m = a.ministries.split(",") if a.ministries else None
    if a.probe:
        from stock_news.collectors import ministry_press
        res = ministry_press.probe(a.probe, only_m)
        print("\n| 부처 | 목록 | 날짜별 | 상세 성공 | 본문 글자 | 첨부 | 게시 시각 | 엠바고 | 실패 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in res:
            print(f"| {r['name']} | {r['listed']} | {r['by_date'] or '-'} | {r['detail_ok']}/{r['detail_tried']} | "
                  f"{r['body_chars'] or '-'} | {r['attachments']} | {r['posted_at']} | {', '.join(r['embargo']) or '-'} | "
                  f"{r['error'] or r['detail_error'] or '-'} |")
        return 2 if any(r["error"] or r["detail_error"] for r in res) else 0
    if a.snapshot:
        from stock_news.collectors import ministry_press
        print(f"부처 보도자료 스냅샷 {now_kst():%Y-%m-%d %H:%M:%S}")
        res = ministry_press.snapshot(db.connect(), only_m)
        return 0 if res["ok"] else 2
    end = _parse(a.until) if a.until else now_kst()
    window = Window(_parse(a.since), end) if a.since else default_window(end)
    print(f"수집 시간대: {window}\n")

    conn = db.connect()
    results = []
    if a.session == "preopen":
        from stock_news import health
        health.QUIET_VOLUME = True
    want = set(a.sources.split(",")) if a.sources else ({a.only} if a.only else {"news", "policy", "ministry", "dart"})
    if "news" in want:
        from stock_news.collectors import naver_focus
        results.append(naver_focus.collect(conn, window, fetch_bodies=not a.no_body))
    if "policy" in want:
        from stock_news.collectors import korea_policy
        results.append(korea_policy.collect(conn, window, fetch_bodies=not a.no_body))
    if "ministry" in want:          # 정책브리핑 다음에 — 같은 보도자료를 묶으려면 정책브리핑 기사가 먼저 있어야 함
        from stock_news.collectors import ministry_press
        results.append(ministry_press.collect(conn, window, fetch_bodies=not a.no_body, only=only_m))
    if "dart" in want:
        from stock_news.collectors import dart
        results.append(dart.collect(conn, window, fetch_bodies=not a.no_body))

    from stock_news.config import session_dir
    out = export_window(conn, window, results,
                        session_dir(f"{window.report_date:%Y-%m-%d}", a.session) if a.session != "morning" else None)
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
