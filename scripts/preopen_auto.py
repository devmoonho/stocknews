#!/usr/bin/env python3
"""장 전 추가 (cron 평일 07:45) — 아침 리포트 뒤(07:00~) 새로 나온 공시·정책만 추가 수집·채점, 08:00 전에 알림.

    python3 scripts/preopen_auto.py                    # 아침 수집 끝 시각 ~ 지금
    python3 scripts/preopen_auto.py --skip-send        # 시험용(텔레그램 안 보냄)
    python3 scripts/preopen_auto.py --since "2026-11-16 07:03" --deadline 07:57:30 --force

순서: 수집(정책브리핑·부처 보도자료·DART — 네이버 뉴스는 하루 2회 제한이라 제외) → 매칭(--session preopen)
     → Claude 채점(아침과 같은 채점표, --deadline까지) → 검증(score.py --session preopen → DB preopen_scores)
     → status.json → 텔레그램 '장 전 추가'.
  - 아침 리포트는 다시 만들지 않는다. 결과는 API /api/report/{날짜}의 pre_open_additions로도 나간다.
  - Claude가 마감까지 못 끝내면 객관 지표 간이 결과(공시만)로 대체.
  - DART 접수는 07:30부터. 로그: data/logs/YYYY-MM-DD_preopen.log
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

PROMPT = """오늘 {date} **장 전 추가**(아침 리포트 뒤 {since}~{until}에 새로 나온 공시·정책)의 채점 단계만 수행한다.
수집·매칭은 끝났고 알림은 자동 — 하지 마라. 아침 리포트는 건드리지 않는다.
시간이 없다: **{minutes}분 안에** 끝내야 한다. 다이제스트 A(공시)·E(정부 정책)를 먼저, 나머지는 시간이 남으면.

1. config/scoring_rubric.md 를 읽는다.
2. {work}/candidates_digest.md 를 읽는다.
3. 아침과 같은 채점표 v3대로 {work}/scores.json 을 쓴다. 형식은 src/stock_news/analysis/scoring.py 맨 위 docstring.
   - contract·mna·buyback은 quality 필수. preprice·theme_cluster·leader는 쓰지 않는다.
   - 공시만 있는 종목은 "diffusion": null. 항목마다 category. 기사·공시에 없는 사실은 쓰지 않는다.
   - 정책은 근거에 [정책:부처명]. 정부 정책뉴스(E)는 reviewed_policies에 한 줄씩. 호재가 없으면 items는 빈 배열.
4. `python3 scripts/score.py --date {date} --session preopen` 을 실행하고 오류가 나면 고쳐 통과시킨다.
5. 끝나면 한국어로 한 줄: 호재 몇 종목.
"""


def parse_hms(s: str) -> dt.time:
    parts = [int(x) for x in s.split(":")] + [0, 0]
    return dt.time(parts[0], parts[1], parts[2])


def morning_end(conn, date: str) -> str:
    """오늘 아침 수집(직전 거래일 15:30 ~ 07:0x)이 끝난 시각 = 장 전 추가의 시작. 없으면 07:00."""
    r = conn.execute(
        "SELECT MAX(window_end) FROM collect_runs WHERE source IN ('dart','korea_policy','ministry_press') "
        "AND window_end BETWEEN ? AND ? AND window_start < ?",
        (f"{date} 06:30:00", f"{date} 07:40:00", f"{date} 00:00:00")).fetchone()
    return (r[0] or f"{date} 07:00:00")[:16]


def write_status(work: Path, **kw) -> None:
    work.mkdir(parents=True, exist_ok=True)
    kw["finished_at"] = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    (work / "status.json").write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")


def write_fallback(run: Run, date: str, cands: dict, reason: str) -> int:
    from score import save_preopen

    from stock_news import db
    from stock_news.analysis.fallback import build_items
    items = build_items(cands)
    (session_dir(date, "preopen") / "scored.json").write_text(json.dumps(
        {"date": date, "session": "preopen", "fallback": True, "fallback_reason": reason, "window": cands["window"],
         "items": items, "reviewed_policies": []}, ensure_ascii=False, indent=1), encoding="utf-8")
    save_preopen(db.connect(), date, items, fallback=True)
    run.write(f"  간이 결과로 대체: {reason} — {len(items)}종목")
    return len(items)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="시작 'YYYY-MM-DD HH:MM' (기본: 오늘 아침 수집이 끝난 시각, 없으면 07:00)")
    ap.add_argument("--until", help="끝 'YYYY-MM-DD HH:MM' (기본: 지금) — 지난 날 시험용")
    ap.add_argument("--force", action="store_true", help="휴장일이어도 실행")
    ap.add_argument("--skip-send", action="store_true")
    ap.add_argument("--deadline", default="07:57:30", help="Claude 채점을 기다릴 마지막 시각 HH:MM[:SS]")
    a = ap.parse_args()
    load_env()
    os.environ.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    today = dt.date.fromisoformat(a.since[:10]) if a.since else now_kst().date()
    if not a.force and not is_trading_day(today, load_holidays()):
        print(f"{today} 휴장일 — 실행 안 함")
        return 0
    lock = (DATA_DIR / "preopen_auto.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("이미 실행 중")
        return 0

    from stock_news import db
    from stock_news.preopen import load, telegram_text
    date = f"{today:%Y-%m-%d}"
    conn = db.connect()
    since = a.since or morning_end(conn, date)
    until = a.until or f"{now_kst():%Y-%m-%d %H:%M}"
    work = session_dir(date, "preopen")
    run = Run(f"{date}_preopen", a.skip_send)
    run.write(f"===== 장 전 추가 {since} ~ {until} 시작 =====")
    window = {"start": f"{since}:00", "end": f"{until}:00"}
    try:
        run.step("수집", [PY, "scripts/collect.py", "--since", since, "--until", until,
                         "--sources", "policy,ministry,dart", "--session", "preopen"], ok_codes=(0, 2), timeout=330)
        run.step("매칭", [PY, "scripts/match.py", "--since", since, "--until", until, "--session", "preopen"],
                 ok_codes=(0, 2), timeout=150)
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
        counts = {"disclosures": cands["counts"].get("disclosures_listed", 0), "policies": len(cands.get("gov_policy", [])),
                  "stocks": len(cands.get("stocks", []))}
        status, fallback_reason = "있음", None
        if not cands.get("stocks") and not cands.get("gov_policy"):
            status = "없음"
            (work / "scored.json").write_text(json.dumps({"date": date, "session": "preopen", "window": cands["window"],
                                                          "items": [], "reviewed_policies": []}, ensure_ascii=False),
                                              encoding="utf-8")
            run.write("  새 공시·정책 후보 없음 — 채점 생략")
        else:
            deadline = dt.datetime.combine(today, parse_hms(a.deadline), tzinfo=now_kst().tzinfo)
            left = (deadline - now_kst()).total_seconds()
            scored = work / "scored.json"
            before = scored.stat().st_mtime if scored.exists() else 0
            claude = find_claude()
            if not claude:
                fallback_reason = "claude 실행 파일 없음"
            elif left < 60:
                fallback_reason = f"채점 시작 시각이 마감 {a.deadline}에 너무 가까움"
            else:
                cmd = [claude, "-p", PROMPT.format(date=date, work=f"data/work/{date}_preopen", minutes=int(left // 60),
                                                   since=since[11:], until=until[11:]),
                       "--output-format", "json", "--allowedTools", ",".join(CLAUDE_TOOLS), "--max-turns", "50"]
                if os.environ.get("CLAUDE_MODEL_PREOPEN") or os.environ.get("CLAUDE_MODEL"):
                    cmd += ["--model", os.environ.get("CLAUDE_MODEL_PREOPEN") or os.environ["CLAUDE_MODEL"]]
                run.write(f"▶ 채점(Claude) — 마감 {a.deadline}까지 {left:.0f}초")
                t0 = time.monotonic()
                try:
                    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=left)
                    run.log.write(p.stdout[-8000:] + ("\n[stderr]\n" + p.stderr[-2000:] if p.stderr.strip() else "") + "\n")
                    run.write(f"  종료 코드 {p.returncode} ({time.monotonic() - t0:.0f}초)")
                    if p.returncode != 0:
                        fallback_reason = f"Claude 종료 코드 {p.returncode}"
                except subprocess.TimeoutExpired:
                    fallback_reason = f"Claude 채점이 {a.deadline}까지 끝나지 않음"
                if not fallback_reason and (not scored.exists() or scored.stat().st_mtime <= before):
                    fallback_reason = "scored.json이 만들어지지 않음(채점 미완료)"
                if not fallback_reason:
                    try:
                        run.step("검증", [PY, "scripts/score.py", "--date", date, "--session", "preopen"], timeout=60)
                    except StepFailed as e:
                        fallback_reason = f"검증 실패: {e.tail[-200:]}"
            if fallback_reason:
                write_fallback(run, date, cands, fallback_reason)
        if status == "있음":
            n_items = len(json.loads((work / "scored.json").read_text(encoding="utf-8")).get("items", []))
            status = "있음" if n_items else "없음"           # 후보는 있었지만 채점 결과 종목이 없으면 '없음'
        write_status(work, status=status, window=window, fallback=bool(fallback_reason),
                     fallback_reason=fallback_reason, counts=counts)
        data = load(conn, date)
        text = telegram_text(data, date)
        run.write(f"  결과: {data['status']} · {len(data['items'])}종목")
        if not a.skip_send:
            from stock_news.telegram_notify import TelegramNotifier
            tg = TelegramNotifier()
            if tg.configured:
                run.write("✅ 텔레그램 전송" if tg.send(text) else "❌ 텔레그램 실패")
        else:
            print("--- 텔레그램 ---\n" + text)
        run.write(f"===== 완료 {now_kst():%H:%M:%S} =====")
        return 0
    except (StepFailed, subprocess.TimeoutExpired, Exception) as e:  # noqa: BLE001
        msg = f"{e}: {getattr(e, 'tail', '')[-300:]}" if isinstance(e, StepFailed) else repr(e)
        run.write(f"✖ {msg}")
        write_status(work, status="실패", window=window, fallback=True, fallback_reason=msg[:300], counts=None)
        if not a.skip_send:
            alert(f"❌ 장 전 추가 실패 ({date})\n{msg[:400]}\n로그: {run.log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
