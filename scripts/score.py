#!/usr/bin/env python3
"""3단계 마무리: Claude가 쓴 scores.json 검증 + 최종 점수 계산 → scored.json, SQLite scores 테이블.

    python3 scripts/score.py                 # 오늘 날짜 폴더
    python3 scripts/score.py --date 2026-11-12
    python3 scripts/score.py --date 2026-11-13 --work data/work/2026-11-13_v2 --no-db   # 시험용(DB에 안 씀)
오류가 있으면 목록을 출력하고 종료 코드 1 (Claude가 scores.json을 고쳐 다시 실행).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news import prices  # noqa: E402
from stock_news.analysis import followup  # noqa: E402
from stock_news.analysis.scoring import RUBRIC_VERSION, WEIGHTS_VERSION, validate  # noqa: E402
from stock_news.config import WORK_DIR, load_env  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--work", help="작업 폴더 (기본 data/work/날짜)")
    ap.add_argument("--no-db", action="store_true", help="scored.json만 쓰고 DB scores에는 저장하지 않음(시험용)")
    a = ap.parse_args()
    load_env()
    work = Path(a.work).resolve() if a.work else WORK_DIR / a.date
    try:
        scores = json.loads((work / "scores.json").read_text(encoding="utf-8"))
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        print(f"파일 없음: {e.filename}")
        return 1
    except json.JSONDecodeError as e:
        print(f"scores.json JSON 오류: {e}")
        return 1

    # 선반영(preprice) 객관 점수: 호재 종목의 직전 거래일까지 일봉으로 계산 (테마·정책에서 고른 종목은 여기서 일봉을 받음)
    conn = db.connect()
    asof = cands["window"]["start"][:10]
    codes = sorted({it.get("code") for it in scores.get("items", []) if it.get("sentiment") == "호재" and it.get("code")})
    failed = prices.ensure_daily(conn, codes, asof, log=lambda m: None)
    if failed:
        print(f"⚠️  일봉 조회 실패 {len(failed)}종목 — 선반영 점수 없이 계산(가중치에서 빠짐): {', '.join(failed[:10])}")
    info = {r["code"]: r for r in conn.execute("SELECT code, audit_info, order_warning FROM stocks")}
    pre = {c: prices.preprice(prices.context(conn, c, asof), (info.get(c) or {"audit_info": None})["audit_info"],
                              (info.get(c) or {"order_warning": None})["order_warning"]) for c in codes}

    items, errors = validate(scores, cands, pre)
    if errors:
        print(f"❌ 오류 {len(errors)}건 — scores.json 수정 후 다시 실행:")
        for e in errors:
            print(f"  - {e}")
        return 1

    (work / "scored.json").write_text(json.dumps({"date": a.date, "window": cands["window"], "weights_version": WEIGHTS_VERSION, "items": items,
                                                 "reviewed_policies": scores.get("reviewed_policies", [])},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    if a.no_db:
        print("(--no-db: DB 저장 생략)")
    else:
        _save(conn, a.date, items)
        n = followup.register(conn, a.date, items, cands)
        if n:
            print(f"후속 일정 {n}건 등록(유증 납입·거래종결) — 매일 아침 다이제스트 G에서 확인")
    good = [i for i in items if i["sentiment"] == "호재"]
    print(f"✅ 호재 {len(good)}종목 · 악재 {len(items) - len(good)}종목 → {work / 'scored.json'}")
    for it in good[:10]:
        print(f"  {it['final']}점({it['final_raw']}) {it['name']} — {it['material']}")
    return 0


def _save(conn, date: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM scores WHERE date=?", (date,))
    conn.executemany(
        "INSERT INTO scores (date, stock_code, stock_name, sentiment, final, final_raw, material, factors, sources, "
        "weights_version, category, rubric_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(date, it["code"], it["name"], it["sentiment"], it["final"], it["final_raw"], it["material"],
          json.dumps(it.get("factors"), ensure_ascii=False), json.dumps(it.get("sources"), ensure_ascii=False),
          WEIGHTS_VERSION, it.get("category"), RUBRIC_VERSION)
         for it in items])
    conn.commit()


if __name__ == "__main__":
    sys.exit(main())
