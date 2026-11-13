"""저녁 간이 리포트 — Claude 채점이 시간 안에 끝나지 않았을 때 객관 지표만으로 후보를 만든다.

대상: 그날 공시 중 확정형 재료이거나 매수 신호이고 정정·악재 신호가 아닌 것.
정렬: 규모 객관 점수 → 확정형 → 시간외 등락이 작은 순(덜 반영). 점수(final)는 규모 객관 점수(없으면 null).
"""
from __future__ import annotations

from .categories import classify


def build_items(candidates: dict) -> list[dict]:
    out = []
    for s in candidates.get("stocks", []):
        good = [d for d in s["disclosures"] if (d["confirmed"] or d["signal"] == "buy") and d["signal"] != "sell"
                and not (d.get("figures") or {}).get("is_correction")]
        if not good:
            continue
        o = s["objective"]
        d0 = max(good, key=lambda d: (d.get("scale_score") or 0, d["confirmed"]))
        basis = o.get("scale_basis") or ""
        q = s.get("evening") or {}
        out.append({
            "id": f"S-{s['code']}", "code": s["code"], "name": s["name"], "sentiment": "호재",
            "material": f"{d0['report_nm'][:60]}" + (f" — {basis}" if basis else ""),
            "category": classify(d0["report_nm"], [d["report_nm"] for d in good]),
            "factors": {"scale": {"score": o["scale_score"], "reason": basis + " — 객관 점수"} if o.get("scale_score") else None,
                        "preprice": o.get("preprice")},
            "sources": [f"rcept:{d['rcept_no']}" for d in good],
            "final": o.get("scale_score"), "final_raw": o.get("scale_score"),
            "evening": q or None, "fallback": True,
            "_sort": (-(o.get("scale_score") or 0), -max(d["confirmed"] for d in good), q.get("after_hours_pct") or 0),
        })
    out.sort(key=lambda x: x.pop("_sort"))
    return out[:MAX_ITEMS]


MAX_ITEMS = 15
