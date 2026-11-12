#!/usr/bin/env python3
"""3단계 마무리: Claude가 쓴 scores.json 검증 + 최종 점수 계산 → scored.json, SQLite scores 테이블.

    python3 scripts/score.py                 # 오늘 날짜 폴더
    python3 scripts/score.py --date 2026-11-12
오류가 있으면 목록을 출력하고 종료 코드 1 (Claude가 scores.json을 고쳐 다시 실행).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.analysis.scoring import WEIGHTS_VERSION, validate  # noqa: E402
from stock_news.config import WORK_DIR  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    a = ap.parse_args()
    work = WORK_DIR / a.date
    try:
        scores = json.loads((work / "scores.json").read_text(encoding="utf-8"))
        cands = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        print(f"파일 없음: {e.filename}")
        return 1
    except json.JSONDecodeError as e:
        print(f"scores.json JSON 오류: {e}")
        return 1

    items, errors = validate(scores, cands)
    if errors:
        print(f"❌ 오류 {len(errors)}건 — scores.json 수정 후 다시 실행:")
        for e in errors:
            print(f"  - {e}")
        return 1

    (work / "scored.json").write_text(json.dumps({"date": a.date, "window": cands["window"], "weights_version": WEIGHTS_VERSION, "items": items,
                                                 "reviewed_policies": scores.get("reviewed_policies", [])},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    conn = db.connect()
    conn.execute("DELETE FROM scores WHERE date=?", (a.date,))
    conn.executemany(
        "INSERT INTO scores (date, stock_code, stock_name, sentiment, final, final_raw, material, factors, sources, "
        "weights_version, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(a.date, it["code"], it["name"], it["sentiment"], it["final"], it["final_raw"], it["material"],
          json.dumps(it.get("factors"), ensure_ascii=False), json.dumps(it.get("sources"), ensure_ascii=False),
          WEIGHTS_VERSION, it.get("category"))
         for it in items])
    conn.commit()
    good = [i for i in items if i["sentiment"] == "호재"]
    print(f"✅ 호재 {len(good)}종목 · 악재 {len(items) - len(good)}종목 → {work / 'scored.json'}")
    for it in good[:10]:
        print(f"  {it['final']}점({it['final_raw']}) {it['name']} — {it['material']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
