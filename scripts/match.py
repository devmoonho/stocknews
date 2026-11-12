#!/usr/bin/env python3
"""3단계 준비: 수집 결과를 종목·테마에 매칭해 data/work/날짜/candidates.json 생성 (Claude 채점 입력).

    python3 scripts/match.py                       # 직전 거래일 15:30 ~ 지금
    python3 scripts/match.py --since "2026-11-11 15:30" --until "2026-11-12 07:00"
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
from stock_news.config import WORK_DIR  # noqa: E402
from stock_news.timewindow import Window, default_window, now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since"), ap.add_argument("--until")
    a = ap.parse_args()
    p = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
    end = p(a.until) if a.until else now_kst()
    window = Window(p(a.since), end) if a.since else default_window(end)

    conn = db.connect()
    if not conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]:
        print("⚠️  종목 마스터가 비어 있습니다 — 먼저 scripts/update_master.py 실행")
        return 2
    result = build(conn, window)
    out = WORK_DIR / f"{window.report_date:%Y-%m-%d}"
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
