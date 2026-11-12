#!/usr/bin/env python3
"""리포트 요약을 텔레그램으로 보낸다 (ref/telegram 묶음의 telegram_notify.py 사용).

    python3 scripts/send_telegram.py --check            # 봇 토큰 확인(메시지 안 보냄)
    python3 scripts/send_telegram.py --chat-ids         # 봇에 말 건 채팅 ID 찾기
    python3 scripts/send_telegram.py --dry-run          # 보낼 문구만 출력
    python3 scripts/send_telegram.py [--date 2026-11-12]
.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, (선택) TELEGRAM_PREFIX
※ 실거래를 제어하는 봇(AutoInvest QuoVestor)의 토큰은 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.config import WORK_DIR, load_env  # noqa: E402
from stock_news.report import collect_status  # noqa: E402
from stock_news.summary import render  # noqa: E402
from stock_news.telegram_notify import TelegramNotifier  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--chat-ids", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    load_env()
    tg = TelegramNotifier()

    if a.check or a.chat_ids:
        if not tg.token:
            print("TELEGRAM_BOT_TOKEN이 .env에 없습니다")
            return 1
        if a.check:
            me = tg.check()
            print(f"봇 확인: @{me.get('username')} · 채팅 ID {'있음' if tg.chat_id else '없음'}")
        else:
            for c in tg.recent_chat_ids() or [{"안내": "봇에게 아무 말이나 먼저 보낸 뒤 다시 실행"}]:
                print(c)
        return 0

    work = WORK_DIR / a.date
    try:
        scored = json.loads((work / "scored.json").read_text(encoding="utf-8"))
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        print(f"파일 없음: {e.filename} — score.py까지 먼저 실행")
        return 1
    conn = db.connect()
    _, warnings = collect_status(conn, scored["window"])
    text = render(conn, scored, cands, warnings=warnings)
    if a.dry_run:
        print(text)
        print(f"--- {len(text)}자")
        return 0
    if not tg.configured:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env에 없어 건너뜀")
        return 0
    ok = tg.send(text, parse_mode="HTML")
    if not ok:      # HTML 서식 오류 등 → 태그를 빼고 평문으로 한 번 더
        plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        ok = tg.send(plain)
    print("✅ 텔레그램 전송 완료" if ok else "❌ 텔레그램 전송 실패(로그 확인)")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
