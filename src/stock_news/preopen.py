"""장 전 추가(평일 07:50 본 실행 + 08:00 재확인) — 아침 리포트(07:00) 뒤 07:00~지금 새로 나온 공시·정책만 채점한 결과 읽기·알림 문구.

결과 파일: data/work/날짜_preopen/ (07:50 본 실행), data/work/날짜_preopen/recheck/ (08:00 재확인 — 같은 형식)
  - status.json : {status: 없음|있음|실패, window, fallback, fallback_reason, counts, finished_at}
  - scored.json : score.py --session preopen 결과(아침과 같은 채점 규칙). DB preopen_scores
아침 리포트는 다시 만들지 않는다. API /api/report/{날짜}의 pre_open_additions, 텔레그램 '장 전 추가' 알림에 쓴다.
"""
from __future__ import annotations

import json

from .config import session_dir

STATUS_FILE = "status.json"


def first_posted_at(conn, it: dict) -> str | None:
    """근거 중 가장 먼저 게시·접수된 시각(공시 접수 시각, 기사·보도자료 게시 시각)."""
    times = []
    for src in it.get("sources", []):
        kind, _, val = str(src).partition(":")
        r = None
        if kind == "rcept":
            r = conn.execute("SELECT received_at AS t FROM disclosures WHERE rcept_no=?", (val,)).fetchone()
        elif kind == "article":
            r = conn.execute("SELECT published_at AS t FROM articles WHERE id=?", (val,)).fetchone()
        elif str(src).startswith("http"):
            r = conn.execute("SELECT published_at AS t FROM articles WHERE url=?", (str(src),)).fetchone()
        if r and r["t"] and len(r["t"]) > 10:
            times.append(r["t"][:19])
    return min(times) if times else None


def _part(conn, work) -> dict | None:
    from .report import basis_labels, basis_tags
    st_path = work / STATUS_FILE
    if not st_path.exists():
        return None
    st = json.loads(st_path.read_text(encoding="utf-8"))
    items = []
    scored = work / "scored.json"
    if st.get("status") == "있음" and scored.exists():
        for it in json.loads(scored.read_text(encoding="utf-8")).get("items", []):
            items.append({"code": it["code"], "name": it.get("name"), "sentiment": it.get("sentiment"),
                          "final": it.get("final"), "final_raw": it.get("final_raw"), "material": it.get("material"),
                          "category": it.get("category"), "basis": basis_tags(conn, it),
                          "basis_labels": basis_labels(conn, it), "posted_at": first_posted_at(conn, it),
                          "caution": it.get("caution"), "item_id": it.get("id")})
    return {**st, "items": items}


def load(conn, date: str) -> dict:
    """→ {status, window, fallback, items[...], recheck} — status: 실행 전 / 없음 / 있음 / 실패.
    07:50 본 실행과 08:00 재확인(_preopen/recheck)을 합친다(같은 종목은 본 실행 것)."""
    work = session_dir(date, "preopen")
    main, re_ = _part(conn, work), _part(conn, work / "recheck")
    if main is None and re_ is None:
        return {"status": "실행 전", "window": None, "fallback": False, "items": [], "counts": None, "recheck": None}
    parts = [p for p in (main, re_) if p]
    items, seen = [], set()
    for p in parts:
        for it in p["items"]:
            if it["code"] not in seen:
                seen.add(it["code"])
                items.append({**it, "recheck": p is re_})
    items.sort(key=lambda x: (x["sentiment"] != "호재", -(x["final_raw"] or 0)))
    statuses = [p.get("status") for p in parts]
    status = "있음" if items else ("실패" if "실패" in statuses and len(set(statuses)) == 1 else "없음")
    wins = [p.get("window") or {} for p in parts]
    counts = {}
    for p in parts:
        for k, v in (p.get("counts") or {}).items():
            counts[k] = counts.get(k, 0) + (v or 0)
    base = main or re_
    return {"status": status,
            "window": {"start": min(w.get("start") or "9" for w in wins), "end": max(w.get("end") or "" for w in wins)},
            "fallback": any(p.get("fallback") for p in parts if p.get("status") == "있음") or bool(base.get("fallback") and not items),
            "fallback_reason": "; ".join(p.get("fallback_reason") for p in parts if p.get("fallback_reason")) or None,
            "counts": counts or None, "finished_at": max(p.get("finished_at") or "" for p in parts),
            "items": items,
            "recheck": None if re_ is None else {"status": re_.get("status"), "window": re_.get("window"),
                                                  "items": len(re_["items"]), "finished_at": re_.get("finished_at")}}


def telegram_text(data: dict, date: str) -> str:
    w = data.get("window") or {}
    span = f"{(w.get('start') or '')[11:16]}~{(w.get('end') or '')[11:16]}" if w else ""
    c = data.get("counts") or {}
    head = f"🌅 장 전 추가 {date[5:]} ({span})"
    seen = f"검토: 공시 {c.get('disclosures', 0)} · 정책 {c.get('policies', 0)}"
    if data["status"] == "실패":
        return f"{head}\n⚠️ 장 전 추가 실패 — {data.get('fallback_reason') or ''}"
    good = [i for i in data["items"] if i["sentiment"] == "호재"]
    bad = [i for i in data["items"] if i["sentiment"] == "악재"]
    if not data["items"]:
        return f"{head}\n장 전 추가 없음 · {seen}"
    L = [head + (" — 간이(객관 지표)" if data.get("fallback") else ""), seen, ""]
    for i in good[:10]:
        when = (i["posted_at"] or "")[11:16]
        L.append(f"• {i['name']}({i['code']}) {i['final'] or '-'}점 [{', '.join(i['basis_labels'])}] {when}")
        L.append(f"  {(i['material'] or '')[:80]}")
    if bad:
        L.append("")
        L.append("주의: " + ", ".join(f"{i['name']}({i['code']})" for i in bad[:8]))
    L.append("")
    L.append("※ 아침 리포트에 없는 항목만. 판단은 직접 하세요.")
    return "\n".join(L)[:4000]
