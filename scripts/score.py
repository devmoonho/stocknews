#!/usr/bin/env python3
"""3단계 마무리: Claude가 쓴 scores.json 검증 + 최종 점수 계산 → scored.json, SQLite scores 테이블.

    python3 scripts/score.py                 # 오늘 날짜 폴더
    python3 scripts/score.py --date 2026-11-12
    python3 scripts/score.py --date 2026-11-13 --work data/work/2026-11-13_v2 --no-db   # 시험용(DB에 안 씀)
    python3 scripts/score.py --session evening        # 저녁 종가매매 (data/work/날짜_evening → DB evening_scores)
    python3 scripts/score.py --session preopen        # 장 전 추가 (data/work/날짜_preopen → DB preopen_scores)
오류가 있으면 목록을 출력하고 종료 코드 1 (Claude가 scores.json을 고쳐 다시 실행).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news import prices  # noqa: E402
from stock_news.analysis import followup  # noqa: E402
from stock_news.analysis.scoring import RUBRIC_VERSION, WEIGHTS_VERSION, validate  # noqa: E402
from stock_news.config import WORK_SESSIONS, load_env, session_dir  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--work", help="작업 폴더 (기본 data/work/날짜[_evening])")
    ap.add_argument("--session", choices=WORK_SESSIONS, default="morning")
    ap.add_argument("--no-db", action="store_true", help="scored.json만 쓰고 DB scores에는 저장하지 않음(시험용)")
    ap.add_argument("--append", action="store_true", help="preopen 재확인: 그날 장 전 추가 결과를 지우지 않고 종목만 더함")
    a = ap.parse_args()
    load_env()
    work = Path(a.work).resolve() if a.work else session_dir(a.date, a.session)
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
    if a.session == "preopen":           # 장 전: 오늘 일봉은 아직 없음 → 직전 거래일까지 (불필요한 키움 요청 방지)
        from stock_news.timewindow import load_holidays, prev_trading_day
        asof = f"{prev_trading_day(dt.date.fromisoformat(asof), load_holidays()):%Y-%m-%d}"
    codes = sorted({it.get("code") for it in scores.get("items", []) if it.get("sentiment") == "호재" and it.get("code")})
    failed = prices.ensure_daily(conn, codes, asof, log=lambda m: None)
    if failed:
        print(f"⚠️  일봉 조회 실패 {len(failed)}종목 — 선반영 점수 없이 계산(가중치에서 빠짐): {', '.join(failed[:10])}")
    info = {r["code"]: r for r in conn.execute("SELECT code, audit_info, order_warning, raw FROM stocks")}
    pre = {c: prices.preprice(prices.context(conn, c, asof, (info.get(c) or {"raw": None})["raw"]), (info.get(c) or {"audit_info": None})["audit_info"],
                              (info.get(c) or {"order_warning": None})["order_warning"]) for c in codes}
    quotes: dict[str, dict] = {}
    if a.session == "evening":
        quotes = evening_quotes(conn, cands, codes, a.date)
        for c, q in quotes.items():          # 저녁: 오늘 KRX 종가까지 상승률 + 지금 시간외 등락
            ctx = prices.evening_context(conn, c, a.date, q)
            if ctx:
                pre[c] = prices.preprice(ctx, (info.get(c) or {"audit_info": None})["audit_info"],
                                         (info.get(c) or {"order_warning": None})["order_warning"])

    items, errors = validate(scores, cands, pre)
    if errors:
        print(f"❌ 오류 {len(errors)}건 — scores.json 수정 후 다시 실행:")
        for e in errors:
            print(f"  - {e}")
        return 1

    for it in items:
        if it["code"] in quotes:
            it["evening"] = quotes[it["code"]]
    (work / "scored.json").write_text(json.dumps({"date": a.date, "session": a.session, "window": cands["window"],
                                                 "weights_version": WEIGHTS_VERSION, "items": items,
                                                 "reviewed_policies": scores.get("reviewed_policies", [])},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    if a.no_db:
        print("(--no-db: DB 저장 생략)")
    elif a.session == "evening":
        save_evening(conn, a.date, items)
    elif a.session == "preopen":
        save_preopen(conn, a.date, items, append=a.append)
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


def evening_quotes(conn, cands: dict, codes: list[str], date: str) -> dict[str, dict]:
    """채점한 종목의 저녁 시세: match.py가 붙인 것을 쓰고, 테마·정책에서 고른 종목처럼 없는 것만 1분봉을 받는다."""
    got = {s["code"]: s["evening"] for s in cands.get("stocks", []) if s.get("evening")}
    need = [c for c in codes if c not in got]
    if need:
        from stock_news.master.kiwoom_client import KiwoomClient
        try:
            client = KiwoomClient()
            raws = {r["code"]: r["raw"] for r in conn.execute("SELECT code, raw FROM stocks")}
            for c in need:
                q = prices.fetch_evening_quote(client, c, date)
                if q:
                    q["nxt_enabled"] = json.loads(raws.get(c) or "{}").get("nxtEnable") == "Y" or q.get("nxt_traded", False)
                    got[c] = q
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  시간외 시세 조회 실패: {e}")
    return got


def save_evening(conn, date: str, items: list[dict], fallback: bool = False) -> None:
    conn.execute("DELETE FROM evening_scores WHERE date=?", (date,))
    for it in items:
        q = it.get("evening") or {}
        conn.execute(
            "INSERT INTO evening_scores (date, stock_code, stock_name, sentiment, final, final_raw, material, factors, "
            "sources, category, weights_version, rubric_version, fallback, quote_time, krx_close, entry_price, "
            "after_hours_pct, since_disclosure_pct, after_value, nxt_enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (date, it["code"], it["name"], it["sentiment"], it.get("final"), it.get("final_raw"), it.get("material"),
             json.dumps(it.get("factors"), ensure_ascii=False), json.dumps(it.get("sources"), ensure_ascii=False),
             it.get("category"), WEIGHTS_VERSION, RUBRIC_VERSION, int(fallback), q.get("as_of"), q.get("krx_close"),
             q.get("price"), q.get("after_hours_pct"), q.get("since_disclosure_pct"), q.get("after_value"),
             None if "nxt_enabled" not in q else int(q["nxt_enabled"])))
    conn.commit()


def save_preopen(conn, date: str, items: list[dict], fallback: bool = False, append: bool = False) -> None:
    """장 전 추가 채점 → DB preopen_scores (아침 scores와 별도 — 아침 리포트는 다시 만들지 않는다).
    append(08:00 재확인): 본 실행 결과는 두고, 본 실행에 없던 종목만 더한다."""
    if not append:
        conn.execute("DELETE FROM preopen_scores WHERE date=?", (date,))
    conn.executemany(
        "INSERT OR IGNORE INTO preopen_scores (date, stock_code, stock_name, sentiment, final, final_raw, material, factors, sources, "
        "weights_version, category, rubric_version, fallback, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
        [(date, it["code"], it["name"], it["sentiment"], it.get("final"), it.get("final_raw"), it.get("material"),
          json.dumps(it.get("factors"), ensure_ascii=False), json.dumps(it.get("sources"), ensure_ascii=False),
          WEIGHTS_VERSION, it.get("category"), RUBRIC_VERSION, int(fallback)) for it in items])
    conn.commit()


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
