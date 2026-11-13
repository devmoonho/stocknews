#!/usr/bin/env python3
"""리포트를 메일(HTML + 텍스트 대체본)로 보낸다. Gmail SMTP + 앱 비밀번호.

    python3 scripts/send_report.py --dry-run          # 보내지 않고 미리보기 HTML만 저장
    python3 scripts/send_report.py                    # 오늘 리포트 발송 (.env의 MAIL_TO)
    python3 scripts/send_report.py --date 2026-11-12 --to someone@example.com
먼저 score.py → report.py 까지 끝나 있어야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import SESSIONS, load_env, report_path, session_dir  # noqa: E402
from stock_news.mailer import render_html, send, subject  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--session", choices=SESSIONS, default="morning", help="evening = 저녁 종가매매 리포트")
    ap.add_argument("--to", help="받는 주소(쉼표로 여러 명). 없으면 .env의 MAIL_TO")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 미리보기 파일만 저장")
    a = ap.parse_args()
    load_env()

    work = session_dir(a.date, a.session)
    md_path = report_path(a.date, a.session)
    try:
        scored = json.loads((work / "scored.json").read_text(encoding="utf-8"))
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
        text = md_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        print(f"파일 없음: {e.filename} — score.py, report.py 먼저 실행")
        return 1

    html_body = render_html(db.connect(), scored, cands)
    preview = work / "report_email.html"
    preview.write_text(html_body, encoding="utf-8")
    subj = subject(scored)
    print(f"제목: {subj}")
    print(f"미리보기: {preview} ({len(html_body) // 1024} KB)")
    if a.dry_run:
        return 0
    try:
        to = send(subj, html_body, text, [x.strip() for x in a.to.split(",")] if a.to else None)
    except Exception as e:
        print(f"❌ 발송 실패: {e}")
        if "Username and Password not accepted" in str(e) or "535" in str(e):
            print("   Gmail 앱 비밀번호를 확인하세요 (일반 로그인 비밀번호는 안 됨, 2단계 인증 필요)")
        return 1
    print(f"✅ 발송 완료 → {', '.join(to)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
