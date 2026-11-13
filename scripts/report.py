#!/usr/bin/env python3
"""4단계: 검증된 채점 결과(scored.json) → reports/YYYY-MM-DD.md

    python3 scripts/report.py                  # 오늘 날짜
    python3 scripts/report.py --date 2026-11-12
먼저 scripts/score.py 가 오류 없이 끝나 scored.json 이 있어야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import SESSIONS, report_path, session_dir  # noqa: E402
from stock_news.report import render  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--session", choices=SESSIONS, default="morning", help="evening = 저녁 종가매매 리포트")
    a = ap.parse_args()
    work = session_dir(a.date, a.session)
    try:
        scored = json.loads((work / "scored.json").read_text(encoding="utf-8"))
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        print(f"파일 없음: {e.filename} — score.py까지 먼저 실행")
        return 1
    path = report_path(a.date, a.session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(db.connect(), scored, cands), encoding="utf-8")
    print(f"리포트: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
