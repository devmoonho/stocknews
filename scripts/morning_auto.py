#!/usr/bin/env python3
"""매일 아침 자동 실행 (cron용) — 사람 없이 1~8단계를 끝까지.

    python3 scripts/morning_auto.py              # 평소 (휴장일이면 아무것도 안 함)
    python3 scripts/morning_auto.py --force      # 휴장일이어도 실행 (시험용)
    python3 scripts/morning_auto.py --skip-send  # 메일·텔레그램은 보내지 않음 (시험용)

순서
  1~3  python: 종목 마스터(--skip-if-fresh) → 수집 → 매칭
  4~5  claude -p (화면 없는 Claude Code): 채점표대로 scores.json 작성 → score.py 통과할 때까지 수정
  5'   python: score.py 다시 실행해 결과 확인 (Claude가 통과했다고 해도 직접 확인)
  6~8  python: 리포트 → 메일 → 텔레그램
실패하면 어느 단계에서 멈췄는지 텔레그램으로 알린다. 로그: data/logs/YYYY-MM-DD.log

Claude 실행에 필요한 것 (.env)
  CLAUDE_CODE_OAUTH_TOKEN=...   ← 미니PC에서 `claude setup-token`으로 발급
  (선택) CLAUDE_BIN=/home/user/.local/bin/claude, CLAUDE_MODEL=...
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news.config import DATA_DIR, WORK_DIR, load_env  # noqa: E402
from stock_news.telegram_notify import TelegramNotifier  # noqa: E402
from stock_news.timewindow import is_trading_day, load_holidays, now_kst  # noqa: E402

LOG_DIR = DATA_DIR / "logs"
PY = sys.executable or "/usr/bin/python3"

# 화면 없는 Claude에게 허용하는 도구 — 읽기, 그날 작업 폴더에 쓰기, 검증 스크립트와 DB 조회만.
# 주문·네트워크·다른 파일 수정은 허용하지 않는다.
CLAUDE_TOOLS = [
    "Read", "Glob", "Grep",
    "Write(data/work/**)", "Edit(data/work/**)",
    "Bash(python3 scripts/score.py:*)",
    "Bash(python3 -c:*)", "Bash(sqlite3:*)",
]

SCORING_PROMPT = """오늘 {date} 아침 리포트의 **채점 단계만** 수행한다. (수집·매칭은 끝났고, 리포트·발송은 이후 자동으로 된다 — 하지 마라.)

1. config/scoring_rubric.md 를 읽는다.
2. data/work/{date}/candidates_digest.md 를 처음부터 끝까지 읽는다. 필요한 근거는 data/work/{date}/candidates.json 또는
   data/stock_news.db(sqlite3 또는 python3 -c)에서 확인한다.
3. CLAUDE.md의 "채점 규칙"과 채점표대로 data/work/{date}/scores.json 을 쓴다.
   - 형식은 src/stock_news/analysis/scoring.py 맨 위 docstring.
   - 정부 정책뉴스(다이제스트 E)는 모두 검토해 reviewed_policies 에 남긴다.
   - 공시만 있는 종목(확산도 제외)은 "diffusion": null.
   - 항목마다 재료 유형 "category"(contract/earnings/buyback/policy/tech/mna/investment/partnership/other)를 적는다.
   - 기사·공시에 없는 사실을 만들지 않는다. 항목마다 근거 한 줄.
4. `python3 scripts/score.py --date {date}` 를 실행하고, 오류가 나오면 scores.json 을 고쳐 통과할 때까지 반복한다.
5. 끝나면 한국어로 한 줄: 호재 몇 종목, 악재 몇 종목, 1위 종목.
"""


class Run:
    def __init__(self, date: str, skip_send: bool) -> None:
        self.date = date
        self.skip_send = skip_send
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{date}.log"
        self.log = self.log_path.open("a", encoding="utf-8")
        self.notes: list[str] = []

    def write(self, msg: str) -> None:
        line = f"[{now_kst():%H:%M:%S}] {msg}"
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def step(self, name: str, cmd: list[str], ok_codes=(0,), timeout: int = 1800, env=None) -> subprocess.CompletedProcess:
        self.write(f"▶ {name}: {' '.join(cmd[:4])}{' …' if len(cmd) > 4 else ''}")
        t0 = time.monotonic()
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
        self.log.write(p.stdout[-20000:] + ("\n[stderr]\n" + p.stderr[-5000:] if p.stderr.strip() else "") + "\n")
        self.log.flush()
        self.write(f"  종료 코드 {p.returncode} ({time.monotonic() - t0:.0f}초)")
        if p.returncode not in ok_codes:
            raise StepFailed(name, p.returncode, (p.stdout + p.stderr)[-600:])
        return p


class StepFailed(Exception):
    def __init__(self, step: str, code, tail: str):
        super().__init__(f"{step} 실패 (코드 {code})")
        self.step, self.code, self.tail = step, code, tail


def find_claude() -> str | None:
    cand = [os.environ.get("CLAUDE_BIN", ""), shutil.which("claude") or "",
            str(Path.home() / ".local/bin/claude"), str(Path.home() / ".claude/local/claude")]
    for c in cand:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


def alert(text: str) -> None:
    try:
        TelegramNotifier().send(text)
    except Exception:  # noqa: BLE001 — 알림 실패로 더 망가뜨리지 않는다
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="휴장일이어도 실행")
    ap.add_argument("--skip-send", action="store_true", help="메일·텔레그램 발송 생략")
    ap.add_argument("--skip-claude", action="store_true", help="채점 단계 생략(1~3단계 시험용)")
    a = ap.parse_args()
    load_env()
    os.environ.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

    today = now_kst().date()
    if not a.force and not is_trading_day(today, load_holidays()):
        print(f"{today} 휴장일 — 실행 안 함")
        return 0

    # 동시에 두 번 돌지 않게
    lock = (DATA_DIR / "morning_auto.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("이미 실행 중")
        return 0

    date = f"{today:%Y-%m-%d}"
    run = Run(date, a.skip_send)
    run.write(f"===== 자동 실행 시작 {now_kst():%Y-%m-%d %H:%M} =====")
    try:
        run.step("1 종목 마스터", [PY, "scripts/update_master.py", "--skip-if-fresh"], ok_codes=(0, 2), timeout=900)
        p = run.step("2 수집", [PY, "scripts/collect.py"], ok_codes=(0, 2), timeout=900)
        if p.returncode == 2:
            run.notes.append("수집 경고 있음(리포트 상단 참고)")
        run.step("3 매칭", [PY, "scripts/match.py"], timeout=300)

        if a.skip_claude:
            run.write("채점 단계 생략(--skip-claude)")
            return 0

        claude = find_claude()
        if not claude:
            raise StepFailed("4 채점", "-", "claude 실행 파일 없음 — 미니PC에 Claude Code 설치 필요")
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not (Path.home() / ".claude/.credentials.json").exists():
            raise StepFailed("4 채점", "-", "Claude 로그인 정보 없음 — `claude setup-token` 후 .env에 CLAUDE_CODE_OAUTH_TOKEN")
        scored = WORK_DIR / date / "scored.json"
        before = scored.stat().st_mtime if scored.exists() else 0
        cmd = [claude, "-p", SCORING_PROMPT.format(date=date), "--output-format", "json",
               "--allowedTools", ",".join(CLAUDE_TOOLS), "--max-turns", "80"]
        if os.environ.get("CLAUDE_MODEL"):
            cmd += ["--model", os.environ["CLAUDE_MODEL"]]
        p = run.step("4 채점(Claude)", cmd, timeout=2400)
        try:
            res = json.loads(p.stdout)
            run.write(f"  Claude: {str(res.get('result', ''))[:300]}")
            if res.get("is_error"):
                raise StepFailed("4 채점", "error", str(res.get("result", ""))[:600])
        except json.JSONDecodeError:
            pass
        if not scored.exists() or scored.stat().st_mtime <= before:
            raise StepFailed("4 채점", "-", "scored.json이 새로 만들어지지 않음(채점 미완료)")

        run.step("5 검증", [PY, "scripts/score.py", "--date", date], timeout=120)
        run.step("6 리포트", [PY, "scripts/report.py", "--date", date], timeout=120)
        if not a.skip_send:
            if os.environ.get("MAIL_SMTP_PASSWORD"):
                run.step("7 메일", [PY, "scripts/send_report.py", "--date", date], timeout=120)
            run.step("8 텔레그램", [PY, "scripts/send_telegram.py", "--date", date], ok_codes=(0,), timeout=120)
        run.write(f"===== 완료 {now_kst():%H:%M} {'· ' + ', '.join(run.notes) if run.notes else ''} =====")
        return 0
    except StepFailed as e:
        run.write(f"✖ {e}: {e.tail}")
        alert(f"❌ 장전 리포트 자동 실행 실패 ({date})\n단계: {e.step}\n{e.tail[-400:]}\n\n로그: {run.log_path}\n"
              f"수동 실행: Claude Code에서 /morning-report")
        return 1
    except subprocess.TimeoutExpired as e:
        run.write(f"✖ 시간 초과: {e.cmd[:2]}")
        alert(f"❌ 장전 리포트 자동 실행 시간 초과 ({date})\n로그: {run.log_path}")
        return 1
    except Exception as e:  # noqa: BLE001
        run.write(f"✖ 예상 못한 오류: {e!r}")
        alert(f"❌ 장전 리포트 자동 실행 오류 ({date})\n{e!r}\n로그: {run.log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
