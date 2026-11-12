"""공시 본문에서 규모 수치 추출 (채점표 '재료 규모' 항목의 객관 값).

- 단일판매·공급계약: 계약금액, 최근 매출액, 매출액 대비(%)
- 자기주식 취득/처분, 유상증자, 타법인 주식 취득, 유형자산 취득: 금액 → 시가총액 대비(%)
본문은 dart_client.xml_to_text 결과(표는 줄 단위)라 '항목명\n값' 형태가 많다.
"""
from __future__ import annotations

import re

_NUM = r"([\d,]+(?:\.\d+)?)"


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _find(body: str, label_regex: str) -> float | None:
    """'라벨\\n숫자' 형태의 마지막 값.

    정정공시는 앞에 '정정사항' 표(라벨들이 먼저 나열되고 값이 뒤에 몰림)가 있고 뒤에 정정 후 전체 서식이 온다.
    라벨 바로 다음 줄이 숫자인 경우만 인정하고 마지막 것을 쓰면 정정 후 값이 된다.
    값이 '-'(공시유보 등)면 None.
    """
    found = None
    for m in re.finditer(label_regex + r"\s*[\n|]\s*(-|" + _NUM[1:-1] + r")\s*(?:\n|$)", body):
        found = m.group(1)
    return None if found in (None, "-") else _num(found)


def parse(report_nm: str, body: str | None, market_cap: float | None) -> dict | None:
    if not body:
        return None
    title = report_nm.replace(" ", "")
    out: dict = {"is_correction": title.startswith(("[기재정정]", "[첨부정정]", "[발행조건확정]"))}
    if "공급계약" in title or "단일판매" in title:
        out["kind"] = "공급계약"
        out["amount"] = _find(body, r"(?<!조건부 )(?<!조건부)(?:계약금액\s*총액|계약금액)\s*\(원\)")
        out["recent_revenue"] = _find(body, r"(?<!- )최근\s*매출액\s*\(원\)")
        out["revenue_ratio_pct"] = _find(body, r"매출액\s*대비\s*\(%\)")
        if out["amount"] and out["recent_revenue"]:
            calc = round(out["amount"] / out["recent_revenue"] * 100, 2)
            if out["revenue_ratio_pct"] is None or abs(calc - out["revenue_ratio_pct"]) > 1:
                out["revenue_ratio_pct"] = calc      # 표기값이 없거나 어긋나면 계산값
        if out["revenue_ratio_pct"] is not None and out["revenue_ratio_pct"] > 100000:
            out["revenue_ratio_pct"] = None          # 파싱 오류 방지
        m = None
        for m in re.finditer(r"\n3\.\s*계약\s*상대(?:방)?\s*\n([^\n]{2,60})", body):
            pass
        out["counterparty"] = m.group(1).strip() if m else None
        if out["amount"] is None and "공시유보" in body:
            out["note"] = "계약금액 공시유보"
    elif "자기주식취득" in title and "해지" not in title:
        out["kind"] = "자사주취득"
        out["amount"] = _find(body, r"취득\s*예정\s*금액\s*\(원\)") or _find(body, r"계약\s*금액\s*\(원\)")
    elif "자기주식처분" in title:
        out["kind"] = "자사주처분"
        out["amount"] = _find(body, r"처분\s*예정\s*금액\s*\(원\)")
    elif "유상증자" in title:
        out["kind"] = "유상증자"
        m = re.search(r"증자\s*방식\s*\n?\s*\|?\s*([^\n|]{2,30})", body)
        out["method"] = m.group(1).strip() if m else None
        m = re.search(r"자금조달의\s*목적[\s\S]{0,400}", body)
        out["purpose_excerpt"] = re.sub(r"\s+", " ", m.group(0))[:300] if m else None
    elif "타법인주식" in title and "취득" in title:
        out["kind"] = "타법인주식취득"
        out["amount"] = _find(body, r"취득\s*금액\s*\(원\)")
    elif "유형자산취득" in title or "신규시설투자" in title:
        out["kind"] = "시설투자"
        out["amount"] = _find(body, r"(?:취득|투자)\s*금액\s*\(원\)")
        out["equity_ratio_pct"] = _find(body, r"자기자본\s*대비\s*\(%\)")
    else:
        return None
    amt = out.get("amount")
    if amt and market_cap:
        out["market_cap_ratio_pct"] = round(amt / market_cap * 100, 2)
    return out


def scale_score(parsed: dict | None) -> int | None:
    """채점표 '재료 규모' 객관 점수. 해당 수치가 없으면 None(Claude 판단)."""
    if not parsed:
        return None
    r = parsed.get("revenue_ratio_pct")
    if parsed.get("kind") == "공급계약" and r is not None:
        return 5 if r >= 50 else 4 if r >= 20 else 3 if r >= 10 else 2 if r >= 5 else 1
    r = parsed.get("market_cap_ratio_pct")
    if parsed.get("kind") in ("자사주취득", "타법인주식취득", "시설투자") and r is not None:
        return 5 if r >= 10 else 4 if r >= 5 else 3 if r >= 2 else 2 if r >= 1 else 1
    return None
