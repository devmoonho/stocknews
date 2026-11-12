"""재료 유형 — 주간 리포트의 '유형별 영향 지속성' 집계 단위.

채점 때 Claude가 scores.json 항목에 "category"를 적는다(아래 키 중 하나). 없으면 재료 문구·공시명으로 자동 분류.
"""
from __future__ import annotations

# 키: (표시 이름, 자동 분류 키워드 — 앞에 있는 유형이 우선)
CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "contract":   ("공급계약·수주", ("공급계약", "단일판매", "수주", "납품계약", "공사 계약", "공사계약")),
    "earnings":   ("실적", ("실적", "영업익", "영업이익", "순이익", "매출", "흑자전환", "3Q", "4Q", "1Q", "2Q", "분기")),
    "buyback":    ("자사주·주주환원", ("자사주", "자기주식", "소각", "배당", "주주환원", "밸류업")),
    "policy":     ("정부 정책", ("정부", "정책", "예산", "부처", "국책", "규제", "육성", "로드맵")),
    "tech":       ("기술·특허·허가", ("특허", "임상", "허가", "승인", "기술이전", "기술수출", "개발 성공", "인증")),
    "investment": ("시설투자·증설", ("시설투자", "증설", "공장", "생산법인", "생산라인", "유형자산")),
    "mna":        ("M&A·지분·경영권", ("인수", "합병", "지분", "경영권", "최대주주", "출자", "타법인", "소송")),
    "partnership": ("협력·MOU", ("MOU", "업무협약", "협력", "제휴", "맞손", "파트너십")),
    "other":      ("기타", ()),
}
KEYS = tuple(CATEGORIES)


def label(key: str | None) -> str:
    return CATEGORIES.get(key or "other", CATEGORIES["other"])[0]


def classify(material: str, report_names: list[str] | None = None, item_id: str = "") -> str:
    """자동 분류: 정부 정책뉴스에서 고른 종목(P-)은 policy, 그 외는 재료 문구 → 공시명 순으로 키워드 매칭."""
    if item_id.startswith("P-"):
        return "policy"
    for text in [material or "", *(report_names or [])]:   # 재료 문구(구체적) → 공시명(형식적) 순
        t = text.replace(" ", "")
        for key, (_, kws) in CATEGORIES.items():
            if any(k.replace(" ", "") in t for k in kws):
                return key
    return "other"
