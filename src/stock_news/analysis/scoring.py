"""scores.json 검증 + 최종 점수 계산 (가중치는 config/scoring_rubric.md와 동일하게 유지).

scores.json 형식 (Claude 작성):
{
  "date": "YYYY-MM-DD",
  "items": [
    {
      "id": "S-005930" | "T-2:005930" | "P-1:005930" | "F-1:005930",   # 다이제스트 식별자
                                     # S=종목 근거, T=테마 신호, P=정부 정책뉴스, F=후속 이벤트(다이제스트 G)
      "code": "005930", "name": "삼성전자",
      "sentiment": "호재" | "악재",
      "material": "재료 한 줄 요약",
      "factors": {
        "scale":       {"score": 1-5, "reason": "..."},
        "exclusivity": {"score": 1-5, "reason": "..."},
        "novelty":     {"score": 1-5, "reason": "..."},
        "policy":      {"score": 1-5, "reason": "..."} | null,
        "diffusion":   {"score": 1-5, "reason": "..."},
        "quality":     {"score": 1-5, "reason": "..."} | null,   # 재료의 질 — contract·mna·buyback은 필수
        # "preprice"·"theme_cluster"·"leader"는 쓰지 않는다: score.py가 객관 점수로 채운다
        #   preprice 직전 5·20일 상승률·시간외·투자주의 / theme_cluster 테마 결집도 / leader 테마 안 연관도 순위(T-·P-만)
      },
      "category": "contract",   # 재료 유형: contract 공급계약·수주 / earnings 실적 / buyback 자사주·주주환원 /
                                # policy 정부 정책 / tech 기술·특허·허가 / mna M&A·지분·경영권 /
                                # investment 시설투자·증설 / partnership 협력·MOU / other 기타  (없으면 자동 분류)
      "sources": ["rcept:20261111800909", "article:123", "url:https://..."],
      "caution": "선택 — 리스크/주의 한 줄"
    }
  ]
}
악재 항목은 factors 없이 material/caution/sources만 있어도 된다.
공시만 있고 기사가 없는 종목(다이제스트 '확산도 제외')은 "diffusion": null.
채점표 v2(2026-11-13): quality(재료의 질)·preprice(선반영) 추가. 규모(scale)는 다이제스트의 객관 점수 그대로.

정부 정책뉴스 검토 기록(선택, 리포트용):
  "reviewed_policies": [{"id": "P-1", "title": "...", "picked": ["005930"], "note": "고른 이유 또는 안 고른 이유 한 줄"}]
"""
from __future__ import annotations

from .categories import KEYS as CATEGORY_KEYS, classify
from .weights import current as _current_weights

# 가중치는 config/weights.json (주간 리뷰가 데이터로 조절). 모듈 로드 시 현재 버전을 읽는다.
WEIGHTS, WEIGHTS_VERSION = _current_weights()
REQUIRED_FACTORS = ("scale", "exclusivity", "novelty", "diffusion")
QUALITY_REQUIRED = ("contract", "mna", "buyback")     # 재료의 질 판단 근거(발주처·인수자·매입 방식)가 공시에 있는 유형
RUBRIC_VERSION = 3


def _theme_factors(iid: str, code: str, by_code: dict, candidates: dict) -> tuple[dict | None, dict | None]:
    """→ (theme_cluster, leader) 객관 점수. T-·P-는 그 신호·정책에서, S-·F-는 종목이 속한 테마 신호 중 최고 결집도."""
    from .leader import leader_score

    def lead(rank, theme):
        if rank is None:
            return {"score": 3, "reason": f"{theme}: 편입 사유와 겹치는 핵심 단어 없음(중립) — 객관 점수"}
        return {"score": leader_score(rank), "reason": f"{theme} 연관도 {rank}위 — 객관 점수"}
    try:
        if iid.startswith("T-"):
            sig = candidates.get("theme_signals", [])[int(iid[2:].split(":")[0]) - 1]
            cl = sig.get("cluster")
            return ({"score": cl["score"], "reason": cl["reason"]} if cl else None,
                    lead(sig.get("leader_rank", {}).get(code), " / ".join(sig["themes"])))
        if iid.startswith("P-"):
            g = candidates.get("gov_policy", [])[int(iid[2:].split(":")[0]) - 1]
            lr = g.get("leader_rank", {}).get(code)
            if lr:
                cl = lr["cluster"]
                return {"score": cl["score"], "reason": f"[{lr['theme']}] " + cl["reason"]}, lead(lr["rank"], lr["theme"])
            cl = g.get("cluster")
            return ({"score": cl["score"], "reason": cl["reason"]} if cl else None), None
    except (ValueError, IndexError, KeyError):
        return None, None
    cl = (by_code.get(code) or {}).get("objective", {}).get("theme_cluster")
    return ({"score": cl["score"], "reason": cl["reason"]} if cl else None), None


def final_score(factors: dict) -> tuple[float, int]:
    total = wsum = 0.0
    for k, w in WEIGHTS.items():
        f = factors.get(k)
        if f is None:
            continue
        total += f["score"] * w
        wsum += w
    raw = total / wsum
    return round(raw, 2), max(1, min(5, int(raw + 0.5)))     # 반올림(.5 올림)


def _factor_ok(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("score"), int) and 1 <= v["score"] <= 5 \
        and bool(str(v.get("reason") or "").strip())


def validate(scores: dict, candidates: dict, preprice: dict[str, dict | None] | None = None
             ) -> tuple[list[dict], list[str]]:
    """→ (점수 계산된 항목들, 오류 목록). 객관 점수(확산도·규모)와 다르면 오류.

    preprice: {종목코드: {"score", "reason"} | None} — score.py가 가격 데이터로 계산해 넘긴다. 있으면 factors에 채운다.
    """
    preprice = preprice or {}
    errors: list[str] = []
    by_code = {s["code"]: s for s in candidates.get("stocks", [])}
    signals = candidates.get("theme_signals", [])
    from .candidates import diffusion_score

    out = []
    seen = set()
    for i, it in enumerate(scores.get("items", [])):
        tag = f"#{i} {it.get('name')}({it.get('code')})"
        for key in ("id", "code", "name", "sentiment", "material"):
            if not it.get(key):
                errors.append(f"{tag}: '{key}' 없음")
        if it.get("code") in seen:
            errors.append(f"{tag}: 같은 종목이 두 번 나옴 — 재료를 한 항목으로 합칠 것")
        seen.add(it.get("code"))
        if it.get("sentiment") not in ("호재", "악재"):
            errors.append(f"{tag}: sentiment는 '호재' 또는 '악재'")
        if not it.get("sources"):
            errors.append(f"{tag}: sources(근거 출처) 없음")
        cat = it.get("category")
        if cat is not None and cat not in CATEGORY_KEYS:
            errors.append(f"{tag}: category '{cat}'는 {', '.join(CATEGORY_KEYS)} 중 하나")
        if not cat:          # 없으면 자동 분류 (공시명 → 재료 문구)
            names = [str(s).split(":", 1)[1] for s in it.get("sources", []) if str(s).startswith("rcept:")]
            rn = [d["report_nm"] for st in candidates.get("stocks", []) if st["code"] == it.get("code")
                  for d in st["disclosures"] if d["rcept_no"] in names]
            it = {**it, "category": classify(it.get("material", ""), rn, str(it.get("id", ""))), "category_auto": True}
        if it.get("sentiment") == "악재":
            out.append({**it, "final": None, "final_raw": None})
            continue
        f = it.get("factors") or {}
        ok = True
        for k in REQUIRED_FACTORS:
            v = f.get(k)
            if k == "diffusion" and v is None:
                continue                 # 공시만 있는 종목은 null — 아래 객관 점수 대조에서 확인
            if not isinstance(v, dict) or not isinstance(v.get("score"), int) or not 1 <= v["score"] <= 5:
                errors.append(f"{tag}: factors.{k}.score 는 1~5 정수")
                ok = False
            elif not str(v.get("reason") or "").strip():
                errors.append(f"{tag}: factors.{k}.reason(한 줄 근거) 없음")
        p = f.get("policy")
        if p is not None and (not isinstance(p, dict) or not isinstance(p.get("score"), int) or not 1 <= p["score"] <= 5):
            errors.append(f"{tag}: factors.policy 는 null 또는 1~5 점수")
            ok = False
        q = f.get("quality")
        if q is None and it.get("category") in QUALITY_REQUIRED:
            errors.append(f"{tag}: category {it.get('category')}는 factors.quality(재료의 질) 점수·근거가 필요 "
                          "— 발주처/인수자·자금/매입 방식(채점표 6번)")
            ok = False
        elif q is not None and not _factor_ok(q):
            errors.append(f"{tag}: factors.quality 는 null 또는 1~5 점수 + 한 줄 근거")
            ok = False
        if not ok:
            continue

        # 객관 점수 대조
        obj = None
        iid = str(it["id"])
        if iid.startswith("S-") and it["code"] in by_code:
            obj = by_code[it["code"]]["objective"]
        elif iid.startswith("T-"):
            try:
                n = int(iid[2:].split(":")[0])
                sig = signals[n - 1]
                obj = {"diffusion": {"score": diffusion_score(sig["offices"], sig["articles"])}, "scale_score": None}
                if it["code"] in by_code:      # 종목 자체 근거(공시 등)가 있으면 그쪽 규모 점수
                    obj["scale_score"] = by_code[it["code"]]["objective"]["scale_score"]
            except (ValueError, IndexError):
                errors.append(f"{tag}: id '{iid}'에 해당하는 테마 신호 없음")
        elif iid.startswith("P-"):
            try:
                n = int(iid[2:].split(":")[0])
                gov = candidates.get("gov_policy", [])[n - 1]
                obj = {"diffusion": {"score": gov["diffusion"]["score"]}, "scale_score": None}
                if it["code"] in by_code:
                    obj["scale_score"] = by_code[it["code"]]["objective"]["scale_score"]
                if f.get("policy") is None:
                    errors.append(f"{tag}: 정부 정책뉴스(P-)에서 고른 종목은 factors.policy 점수가 필요")
            except (ValueError, IndexError):
                errors.append(f"{tag}: id '{iid}'에 해당하는 정부 정책뉴스 없음")
        elif iid.startswith("F-"):
            try:
                n = int(iid[2:].split(":")[0])
                fu = candidates.get("followups", [])[n - 1]
                if fu["stock_code"] != it["code"]:
                    errors.append(f"{tag}: F-{n}는 {fu['stock_name']}({fu['stock_code']})의 후속 이벤트")
                obj = by_code[it["code"]]["objective"] if it["code"] in by_code else \
                    {"diffusion": {"score": None}, "scale_score": None}
            except (ValueError, IndexError):
                errors.append(f"{tag}: id '{iid}'에 해당하는 후속 이벤트 없음")
        else:
            errors.append(f"{tag}: id '{iid}'가 다이제스트에 없음 (S-코드, T-번호:코드, P-번호:코드, F-번호:코드)")
        if obj:
            want = obj["diffusion"]["score"]
            got = (f.get("diffusion") or {}).get("score")
            if want is None and got is not None:
                errors.append(f"{tag}: 공시만 있는 종목 — diffusion은 null(확산도 제외)이어야 함")
            elif want is not None and got != want:
                errors.append(f"{tag}: diffusion {got} ≠ 객관 점수 {want}")
            if obj.get("scale_score") is not None and f["scale"]["score"] != obj["scale_score"]:
                errors.append(f"{tag}: scale {f['scale']['score']} ≠ 공시 수치 점수 {obj['scale_score']}")
        tc, ld = _theme_factors(iid, it["code"], by_code, candidates)
        # 객관 점수로 덮어씀(데이터 없으면 null → 가중치에서 빠짐)
        f = {**f, "preprice": preprice.get(it["code"]), "theme_cluster": tc, "leader": ld}
        raw, final = final_score(f)
        out.append({**it, "factors": f, "final": final, "final_raw": raw, "rubric_version": RUBRIC_VERSION})

    out.sort(key=lambda x: (x["final"] is None, -(x["final_raw"] or 0)))
    return out, errors
