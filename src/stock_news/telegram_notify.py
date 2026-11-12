"""텔레그램 봇 메시지 발송 — 단독 모듈(표준 라이브러리만, Python 3.9+, 리눅스/윈도우 공통).

환경변수 또는 .env:  TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...   (선택) TELEGRAM_PREFIX="[내앱] "

출처(정리·통합): RAS live/telegram_notify.py · StockLab lab/notify.py(설정 없으면 조용히 건너뜀, 실패는 로그만 —
알림 실패가 본 로직을 막으면 안 됨) · StockAnalysis scripts/send_telegram_report.py(표준입력 → 전송 CLI).
추가: 4096자 넘는 글은 잘라 버리지 않고 여러 통으로 나눠 보냄, 429(초당 제한)면 retry_after만큼 기다렸다 재시도.

사용 예(CLI):
    python telegram_notify.py "매수 체결: 삼성전자 10주"
    some_report_cmd | python telegram_notify.py --title "일일 리포트"
    python telegram_notify.py --check                 # 봇 토큰 확인(getMe) — 메시지 안 보냄
    python telegram_notify.py --chat-ids              # 봇에 최근 말 건 채팅 ID 찾기(getUpdates)

사용 예(코드):
    from telegram_notify import TelegramNotifier
    tg = TelegramNotifier()                  # 환경변수/.env
    tg.send("🔔 [매도가 도달] 삼성전자(005930)\\n현재가: 72,000원")
    await tg.send_async("...")               # asyncio 코드에서
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_CHARS = 4096


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for p in (Path(__file__).with_name(".env"), Path.cwd() / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")})   # 환경변수가 우선
    return env


def split_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """limit 이하 조각으로 — 줄 경계에서 자르고, 한 줄이 너무 길면 그 줄만 강제로 자른다."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return [p.rstrip("\n") for p in parts if p.strip()]


class TelegramNotifier:
    """토큰·채팅 ID가 비어 있으면 send는 아무것도 안 하고 False(호출부가 매번 확인할 필요 없음).
    전송 실패도 예외를 삼키고 로그만 남긴 뒤 False — 매매·감시 루프를 막지 않는다."""

    def __init__(self, token: str | None = None, chat_id: str | None = None, *, prefix: str | None = None,
                 timeout: float = 10.0, retries: int = 2) -> None:
        env = _load_env()
        self.token = (token if token is not None else env.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = str(chat_id if chat_id is not None else env.get("TELEGRAM_CHAT_ID", "")).strip()
        self.prefix = prefix if prefix is not None else env.get("TELEGRAM_PREFIX", "")
        self.timeout = timeout
        self.retries = retries

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, payload: dict | None = None) -> dict:
        """Bot API 호출 → 응답 JSON. 429면 retry_after만큼 쉬고 재시도. 실패하면 예외."""
        data = json.dumps(payload or {}).encode("utf-8")
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(API.format(token=self.token, method=method), data=data,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                try:
                    info = json.loads(body)
                except ValueError:
                    info = {"description": body[:200]}
                wait = (info.get("parameters") or {}).get("retry_after")
                if exc.code == 429 and wait and attempt < self.retries:
                    time.sleep(float(wait) + 0.5)
                    continue
                raise RuntimeError(f"telegram {method} HTTP {exc.code}: {info.get('description')}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"telegram {method} 연결 실패: {exc}") from None
        raise RuntimeError(f"telegram {method} 재시도 초과")

    def send(self, text: str, *, parse_mode: str | None = None, silent: bool = False,
             disable_preview: bool = True) -> bool:
        """메시지 보내기(길면 나눠서). 전부 보냈으면 True. parse_mode: None(평문, 권장) | "HTML" | "MarkdownV2"."""
        if not self.configured or not text or not text.strip():
            return False
        ok = True
        for part in split_message(self.prefix + text):
            payload = {"chat_id": self.chat_id, "text": part, "disable_notification": silent,
                       "disable_web_page_preview": disable_preview}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                res = self._call("sendMessage", payload)
                if not res.get("ok"):
                    log.warning("telegram_send_failed description=%s", res.get("description"))
                    ok = False
            except Exception as exc:  # noqa: BLE001 — 알림 실패가 본 로직을 막으면 안 됨
                log.warning("telegram_send_failed error=%s", exc)
                ok = False
        return ok

    async def send_async(self, text: str, **kw) -> bool:
        """asyncio용 — 이벤트 루프를 막지 않도록 스레드에서 보낸다."""
        return await asyncio.to_thread(self.send, text, **kw)

    def check(self) -> dict:
        """봇 토큰 확인(getMe) — 메시지는 보내지 않는다."""
        return self._call("getMe").get("result", {})

    def recent_chat_ids(self) -> list[dict]:
        """봇에게 최근 말을 건 채팅 목록(getUpdates) — 채팅 ID 찾기용. 봇에게 아무 말이나 먼저 보내 둘 것.
        (웹훅을 쓰는 봇이면 동작하지 않는다.)"""
        seen, out = set(), []
        for u in self._call("getUpdates").get("result", []):
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None and chat["id"] not in seen:
                seen.add(chat["id"])
                out.append({"chat_id": chat["id"], "type": chat.get("type"),
                            "name": chat.get("title") or chat.get("username") or chat.get("first_name")})
        return out


# 메시지 문구 예시(RAS) — 필요에 맞게 고쳐 쓰기
def build_price_alert(code: str, name: str, label: str, price: float, ref: float | None = None,
                      ref_label: str = "기준가") -> str:
    lines = [f"🔔 [{label}] {name}({code})", f"현재가: {price:,.0f}원"]
    if ref:
        lines.append(f"{ref_label}: {ref:,.0f}원 ({(price / ref - 1) * 100:+.2f}%)")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="텔레그램 메시지 발송")
    ap.add_argument("text", nargs="?", help="보낼 글(없으면 표준입력)")
    ap.add_argument("--title", help="맨 앞에 붙일 제목")
    ap.add_argument("--silent", action="store_true", help="알림음 없이")
    ap.add_argument("--html", action="store_true", help="HTML 서식(<b>, <code> 등)")
    ap.add_argument("--check", action="store_true", help="봇 토큰 확인만")
    ap.add_argument("--chat-ids", action="store_true", help="최근 채팅 ID 목록")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    tg = TelegramNotifier()
    if not tg.token:
        print("TELEGRAM_BOT_TOKEN이 없습니다", file=sys.stderr)
        return 1
    if a.check:
        me = tg.check()
        print(f"봇 확인: @{me.get('username')} ({me.get('first_name')}) · 채팅 ID {'있음' if tg.chat_id else '없음'}")
        return 0
    if a.chat_ids:
        for c in tg.recent_chat_ids():
            print(c)
        return 0
    text = a.text if a.text is not None else sys.stdin.read()
    if not text.strip():
        print("보낼 내용이 없습니다", file=sys.stderr)
        return 1
    if a.title:
        text = f"{a.title}\n\n{text}"
    if not tg.chat_id:
        print("TELEGRAM_CHAT_ID가 없습니다 (--chat-ids로 찾기)", file=sys.stderr)
        return 1
    ok = tg.send(text, silent=a.silent, parse_mode="HTML" if a.html else None)
    print("전송 완료" if ok else "전송 실패(로그 확인)")
    return 0 if ok else 2


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    sys.exit(_main())
