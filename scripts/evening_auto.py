#!/usr/bin/env python3
"""저녁 종가매매 리포트 자동 실행 (cron 평일 19:10 사전 수집, 19:30 본 실행) — 19:45 발송 목표.

    python3 scripts/evening_auto.py --prefetch        # 19:10: 15:30 이후 공시·기사 수집 + 후보 일봉 미리 받기
    python3 scripts/evening_auto.py                   # 19:30: 추가 수집 → 매칭(시간외 시세) → Claude 채점 → 리포트 → 메일 → 텔레그램
    python3 scripts/evening_auto.py --skip-send       # 시험용(발송 안 함)
    python3 scripts/evening_auto.py --deadline 19:42:30 --force

마감 규칙
  - Claude 채점은 --deadline(기본 19:42:30)까지만 기다린다. 넘기거나 실패하면 객관 지표 간이 리포트(analysis/fallback.py)로 대체.
  - 무엇이든 19:45 전에 보낸다. 로그: data/logs/YYYY-MM-DD_evening.log
주문 기능 없음 — 최종 판단은 사용자.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from morning_auto import CLAUDE_TOOLS, PY, Run, StepFailed, alert, find_claude  # noqa: E402

from stock_news.config import DATA_DIR, load_env, session_dir  # noqa: E402
from stock_news.timewindow import is_trading_day, load_holidays, now_kst  # noqa: E402

PROMPT = """오늘 {date} **저녁 종가매매 리포트**의 채점 단계만 수행한다. (수집·매칭은 끝났고 리포트·발송은 자동 — 하지 마라.)
시간이 없다: **{minutes}분 안에** 끝내야 한다. 다이제스트 A(공시)·B(제목 기사)·E(정부 정책)·D(테마) 순으로 중요한 것부터 채점하고,
C(본문에만 나온 종목)는 건너뛴다.

1. config/scoring_rubric.md 를 읽는다.
2. {work}/candidates_digest.md 를 읽는다. 각 종목의 '시간외' 시세(선반영)는 score.py가 점수로 채운다.
3. 채점표 v3대로 {work}/scores.json 을 쓴다. 형식은 src/stock_news/analysis/scoring.py 맨 위 docstring.
   - contract·mna·buyback은 quality 필수. preprice·theme_cluster·leader는 쓰지 않는다.
   - 공시만 있는 종목은 "diffusion": null. 항목마다 category. 기사·공시에 없는 사실은 쓰지 않는다.
   - 정부 정책뉴스(E)는 reviewed_policies에 한 줄씩.
4. `python3 scripts/score.py --date {date} --session evening` 을 실행하고 오류가 나면 고쳐 통과시킨다.
5. 끝나면 한국어로 한 줄: 호재 몇 종목, 1위 종목.
"""


def parse_hms(s: str) -> dt.time:
    parts = [int(x) for x in s.split(":")] + [0, 0]
    return dt.time(parts[0], parts[1], parts[2])


def write_fallback(run: Run, date: str, reason: str) -> None:
    """Claude 채점 대신 객관 지표로 scored.json을 만들고 evening_scores에 저장."""
    from score import save_evening

    from stock_news import db
    from stock_news.analysis.fallback import build_items
    work = session_dir(date, "evening")
    cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    items = build_items(cands)
    (work / "scored.json").write_text(json.dumps(
        {"date": date, "session": "evening", "fallback": True, "fallback_reason": reason, "window": cands["window"],
         "items": items, "reviewed_policies": []}, ensure_ascii=False, indent=1), encoding="utf-8")
    save_evening(db.connect(), date, items, fallback=True)
    run.write(f"  간이 리포트로 대체: {reason} — 후보 {len(items)}종목")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefetch", action="store_true", help="19:10 사전 수집만")
    ap.add_argument("--force", action="store_true", help="휴장일이어도 실행")
    ap.add_argument("--skip-send", action="store_true")
    ap.add_argument("--deadline", default="19:42:30", help="Claude 채점을 기다릴 마지막 시각 HH:MM[:SS]")
    a = ap.parse_args()
    load_env()
    os.environ.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

    today = now_kst().date()
    if not a.force and not is_trading_day(today, load_holidays()):
        print(f"{today} 휴장일 — 실행 안 함")
        return 0
    lock = (DATA_DIR / "evening_auto.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("이미 실행 중")
        return 0

    date = f"{today:%Y-%m-%d}"
    run = Run(f"{date}_evening", a.skip_send)
    run.write(f"===== 저녁 {'사전 수집' if a.prefetch else '본 실행'} 시작 {now_kst():%H:%M:%S} =====")
    try:
        run.step("수집", [PY, "scripts/collect.py"], ok_codes=(0, 2), timeout=600)
        if a.prefetch:
            run.step("후보 일봉 미리 받기", [PY, "scripts/match.py", "--session", "evening", "--no-quotes"],
                     ok_codes=(0, 2), timeout=600)
            run.write(f"===== 사전 수집 끝 {now_kst():%H:%M:%S} =====")
            return 0
        run.step("매칭(시간외 시세)", [PY, "scripts/match.py", "--session", "evening"], ok_codes=(0, 2), timeout=300)

        # ---- Claude 채점 (마감 시각까지)
        deadline = dt.datetime.combine(today, parse_hms(a.deadline), tzinfo=now_kst().tzinfo)
        left = (deadline - now_kst()).total_seconds()
        work = session_dir(date, "evening")
        scored = work / "scored.json"
        before = scored.stat().st_mtime if scored.exists() else 0
        claude = find_claude()
        fallback_reason = None
        if not claude:
            fallback_reason = "claude 실행 파일 없음"
        elif left < 60:
            fallback_reason = f"채점 시작 시각이 마감 {a.deadline}에 너무 가까움"
        else:
            cmd = [claude, "-p", PROMPT.format(date=date, work=f"data/work/{date}_evening", minutes=int(left // 60)),
                   "--output-format", "json", "--allowedTools", ",".join(CLAUDE_TOOLS), "--max-turns", "60"]
            if os.environ.get("CLAUDE_MODEL_EVENING") or os.environ.get("CLAUDE_MODEL"):
                cmd += ["--model", os.environ.get("CLAUDE_MODEL_EVENING") or os.environ["CLAUDE_MODEL"]]
            run.write(f"▶ 채점(Claude) — 마감 {a.deadline}까지 {left:.0f}초")
            t0 = time.monotonic()
            try:
                p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=left)
                run.log.write(p.stdout[-10000:] + ("\n[stderr]\n" + p.stderr[-3000:] if p.stderr.strip() else "") + "\n")
                run.write(f"  종료 코드 {p.returncode} ({time.monotonic() - t0:.0f}초)")
                if p.returncode != 0:
                    fallback_reason = f"Claude 종료 코드 {p.returncode}"
            except subprocess.TimeoutExpired:
                fallback_reason = f"Claude 채점이 {a.deadline}까지 끝나지 않음"
                run.write(f"  ✖ 시간 초과 ({time.monotonic() - t0:.0f}초)")
            if not fallback_reason and (not scored.exists() or scored.stat().st_mtime <= before):
                fallback_reason = "scored.json이 만들어지지 않음(채점 미완료)"
            if not fallback_reason:
                # Claude가 통과시켰어도 직접 한 번 더 검증·저장
                try:
                    run.step("검증", [PY, "scripts/score.py", "--date", date, "--session", "evening"], timeout=90)
                except StepFailed as e:
                    fallback_reason = f"검증 실패: {e.tail[-200:]}"
        if fallback_reason:
            write_fallback(run, date, fallback_reason)
            run.notes.append("간이 리포트(" + fallback_reason + ")")

        run.step("리포트", [PY, "scripts/report.py", "--date", date, "--session", "evening"], timeout=60)
        if not a.skip_send:
            if os.environ.get("MAIL_SMTP_PASSWORD"):
                try:
                    run.step("메일", [PY, "scripts/send_report.py", "--date", date, "--session", "evening"], timeout=60)
                except StepFailed as e:     # 메일이 실패해도 텔레그램은 보낸다
                    run.notes.append(f"메일 실패: {e.tail[-100:]}")
            run.step("텔레그램", [PY, "scripts/send_telegram.py", "--date", date, "--session", "evening"], timeout=60)
        run.write(f"===== 완료 {now_kst():%H:%M:%S} {'· ' + ', '.join(run.notes) if run.notes else ''} =====")
        return 0
    except StepFailed as e:
        run.write(f"✖ {e}: {e.tail}")
        alert(f"❌ 저녁 종가매매 리포트 실패 ({date})\n단계: {e.step}\n{e.tail[-400:]}\n로그: {run.log_path}")
        return 1
    except subprocess.TimeoutExpired as e:
        run.write(f"✖ 시간 초과: {e.cmd[:2]}")
        alert(f"❌ 저녁 종가매매 리포트 시간 초과 ({date})\n로그: {run.log_path}")
        return 1
    except Exception as e:  # noqa: BLE001
        run.write(f"✖ 예상 못한 오류: {e!r}")
        alert(f"❌ 저녁 종가매매 리포트 오류 ({date})\n{e!r}\n로그: {run.log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
