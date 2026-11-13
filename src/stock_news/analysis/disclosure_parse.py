"""공시 본문에서 규모·조건 수치 추출 (채점표 '재료 규모' 객관 값 + '재료의 질' 힌트).

본문은 dart_client.xml_to_text 결과(표는 줄 단위)라 '항목명\n값' 형태가 많다.

종류(kind)별 추출
- 공급계약     : 계약금액, 최근 매출액, 매출 대비 %, 계약기간(→ 연환산 매출 대비 %), 계약명, 계약상대·유형, 선급금
- 자사주취득   : 금액, 직접취득/신탁, 신탁 계약기간(→ 일평균 매수 가능액), 소각 언급
- 자사주소각   : 소각 예정 금액
- 자사주처분   : 금액 (악재 쪽 정보)
- 유상증자     : 방식, 자금 목적별 금액(회사 유입액), 발행가·기준주가·할인율, 납입일, 보호예수, 배정대상자·관계·조달방법
- 경영권변경   : 양수도 대금(기존 대주주에게 가는 돈 — 규모에서 제외), 양수인 정보(설립연월·최대주주·재무)
- 타법인주식취득 / 시설투자 : 금액
시가총액 대비 %는 market_cap(최근 종가 × 상장주식수)이 있을 때.
"""
from __future__ import annotations

import datetime as dt
import re

from ..config import CONFIG_DIR

_NUM = r"([\d,]+(?:\.\d+)?)"
_SNUM = r"(-?[\d,]+(?:\.\d+)?)"
_DATE = r"(\d{4})\s*[-.년/]\s*(\d{1,2})\s*[-.월/]\s*(\d{1,2})"


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _find(body: str, label_regex: str, signed: bool = False) -> float | None:
    """'라벨\\n숫자' 형태의 마지막 값.

    정정공시는 앞에 '정정사항' 표(라벨들이 먼저 나열되고 값이 뒤에 몰림)가 있고 뒤에 정정 후 전체 서식이 온다.
    라벨 바로 다음 줄이 숫자인 경우만 인정하고 마지막 것을 쓰면 정정 후 값이 된다.
    값이 '-'(공시유보 등)면 None.
    """
    num = (_SNUM if signed else _NUM)[1:-1]
    found = None
    for m in re.finditer(label_regex + r"\s*[\n|]\s*(-|" + num + r")\s*(?:\n|$)", body):
        found = m.group(1)
    return None if found in (None, "-") else _num(found)


def _find_text(body: str, label_regex: str, maxlen: int = 80) -> str | None:
    m = None
    for m in re.finditer(label_regex + r"\s*\n\s*([^\n]{1," + str(maxlen) + r"})", body):
        pass
    if not m:
        return None
    v = m.group(1).strip()
    return None if v in ("-", "") else v


def _date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(_DATE, s)
    if not m:
        return None
    try:
        return f"{dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))):%Y-%m-%d}"
    except ValueError:
        return None


def _find_date(body: str, label_regex: str) -> str | None:
    m = None
    for m in re.finditer(label_regex + r"\s*[\n|:]\s*" + _DATE, body):
        pass
    return _date(m.group(0)[-20:]) if m else None


def _period(body: str) -> tuple[str | None, str | None]:
    """계약기간 시작일·종료일 (공급계약·신탁계약 공통 서식)."""
    m = re.search(r"계약\s*기간[\s\S]{0,40}?시작일\s*\n\s*([^\n]+)\n\s*종료일\s*\n\s*([^\n]+)", body)
    if not m:
        return None, None
    return _date(m.group(1)), _date(m.group(2))


def _years(start: str | None, end: str | None) -> float | None:
    if not (start and end):
        return None
    d = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    return round(d / 365.25, 2) if d > 0 else None


# ---------------------------------------------------------------- 계약상대 유형
def _counterparty_rules() -> list[tuple[str, list[str]]]:
    """config/counterparty_types.txt: '유형|키워드1,키워드2' (위에 있는 유형 우선)."""
    path = CONFIG_DIR / "counterparty_types.txt"
    rules = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                kind, kws = line.split("|", 1)
                rules.append((kind.strip(), [k.strip() for k in kws.split(",") if k.strip()]))
    return rules


def counterparty_type(name: str | None) -> str | None:
    if not name:
        return None
    n = name.replace(" ", "")
    for kind, kws in _counterparty_rules():
        if any(k.replace(" ", "") in n for k in kws):
            return kind
    return "기타"


# ---------------------------------------------------------------- 종류별
FUND_LABELS = {"시설자금": r"시설자금\s*\(원\)", "영업양수자금": r"영업양수자금\s*\(원\)",
               "운영자금": r"운영자금\s*\(원\)", "채무상환자금": r"채무상환자금\s*\(원\)",
               "타법인증권취득자금": r"타법인\s*증권\s*취득자금\s*\(원\)", "기타자금": r"기타자금\s*\(원\)"}


def _contract(body: str, out: dict) -> None:
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
    out["counterparty_type"] = counterparty_type(out["counterparty"])
    out["contract_name"] = _find_text(body, r"체결\s*계약명")
    out["start"], out["end"] = _period(body)
    out["years"] = _years(out["start"], out["end"])
    r = out["revenue_ratio_pct"]
    # 기간이 1년을 넘으면 매출 기여는 나뉘어 인식된다 → 연환산
    out["annual_revenue_ratio_pct"] = (round(r / out["years"], 2) if r is not None and out["years"] and out["years"] > 1
                                       else r)
    adv = _find_text(body, r"계약금\s*ㆍ?\s*선급금\s*유무", 5)
    out["advance_payment"] = None if adv is None else adv.startswith("유")
    if out["amount"] is None and "공시유보" in body:
        out["note"] = "계약금액 공시유보"


def _buyback(title: str, body: str, out: dict) -> None:
    out["kind"] = "자사주취득"
    out["method"] = "신탁" if "신탁" in title else "직접취득"
    out["amount"] = (_find(body, r"계약\s*금액\s*\(원\)") if out["method"] == "신탁" else None) \
        or _find(body, r"취득\s*예정\s*금액\s*\(원\)") or _find(body, r"계약\s*금액\s*\(원\)")
    if out["method"] == "신탁":
        out["start"], out["end"] = _period(body)
    else:
        out["start"] = _find_date(body, r"취득\s*예상\s*기간\s*\n\s*시작일")
        out["end"] = _find_date(body, r"취득\s*예상\s*기간[\s\S]{0,40}?종료일")
    purpose = _find_text(body, r"(?:계약|취득)\s*목적", 120) or ""
    # 소각: 별도 '소각결정' 공시가 가장 강함(kind 자사주소각). 여기서는 목적·기타사항에 소각이 적혀 있는지만
    out["burn"] = "예정" if ("소각" in purpose or re.search(r"소각할\s*예정|소각\s*예정|소각을\s*통한", body)) else "없음"


def _rights_issue(body: str, out: dict) -> None:
    out["kind"] = "유상증자"
    m = re.search(r"증자\s*방식\s*\n?\s*\|?\s*([^\n|]{2,30})", body)
    out["method"] = m.group(1).strip() if m else None
    funds = {k: _find(body, rx) for k, rx in FUND_LABELS.items()}
    out["funds"] = {k: v for k, v in funds.items() if v}
    out["amount"] = sum(out["funds"].values()) or None        # 회사로 들어오는 돈
    out["issue_price"] = _find(body, r"신주\s*발행가액\s*\n\s*보통주식\s*\(원\)")
    out["base_price"] = _find(body, r"기준주가\s*\n\s*보통주식\s*\(원\)")
    out["discount_pct"] = _find(body, r"기준주가에\s*대한\s*할인율\s*또는\s*할증율\s*\(%\)", signed=True)
    if out["discount_pct"] is None and out["issue_price"] and out["base_price"]:
        out["discount_pct"] = round((out["issue_price"] / out["base_price"] - 1) * 100, 1)
    out["payment_date"] = _find_date(body, r"납입일")
    out["new_shares"] = _find(body, r"신주의\s*종류와\s*수\s*\n\s*보통주식\s*\(주\)")
    out["shares_before"] = _find(body, r"증자전\s*발행주식총수\s*\(주\)\s*\n\s*보통주식\s*\(주\)")
    if out["new_shares"] and out["shares_before"]:
        out["dilution_pct"] = round(out["new_shares"] / out["shares_before"] * 100, 1)
    lock = re.search(r"(\d+)\s*(년|개월)\s*간?\s*(?:전량\s*)?(?:의무보유|보호예수|전매제한)", body) or \
        re.search(r"(?:의무보유|보호예수)[^\n]{0,20}?(\d+)\s*(년|개월)", body)
    out["lockup"] = f"{lock.group(1)}{lock.group(2)}" if lock else None
    # 배정대상자 표: 헤더(대상자·관계·선정경위·거래내역·배정주식수·비고) 다음 줄부터
    m = re.search(r"제3자배정\s*대상자별[^\n]*\n(?:[^\n]*\n){0,8}?비\s*고\s*\n([^\n]+)\n([^\n]+)", body)
    if m:
        out["allottee"] = m.group(1).strip()
        out["allottee_relation"] = m.group(2).strip()
    out["allottee_is_fund"] = bool(out.get("allottee") and "조합" in out["allottee"])
    out["allottee_funding"] = _find_text(body, r"조달\s*방법", 40)
    out["third_party_securities"] = bool(out["funds"].get("타법인증권취득자금"))
    if out["third_party_securities"]:
        m = re.search(r"타법인\s*증권\s*취득자금\s*\n([^\n]+)\n([^\n]+)", body[body.find("구체적 사용목적"):]
                      if "구체적 사용목적" in body else "")
        out["target_undecided"] = bool(m and "미확정" in m.group(0))


def _control_change(body: str, out: dict) -> None:
    out["kind"] = "경영권변경"
    out["transfer_amount"] = _find(body, r"양수도\s*대금\s*\(원\)")    # 기존 대주주에게 가는 돈 → 규모에서 제외
    out["acquirer"] = _find_text(body, r"\n양수인", 60)
    sec = body[body.find("양수인(법인인 경우)"):] if "양수인(법인인 경우)" in body else ""
    if sec:
        out["acquirer"] = _find_text(sec, r"법인명", 60) or out["acquirer"]
        out["acquirer_founded"] = _date(_find_text(sec, r"설립연월", 20))
        out["acquirer_owner"] = _find_text(sec, r"법인의\s*최대주주명", 40)
        out["acquirer_owner_pct"] = _find(sec, r"보유지분율\s*\(%\)")
        fin = {k: _find(sec, rx, signed=True) for k, rx in
               {"assets": r"자산총계", "capital": r"자본금", "equity": r"자본총계",
                "revenue": r"매출액", "net_income": r"당기순손익"}.items()}
        out["acquirer_fin_mil"] = {k: v for k, v in fin.items() if v is not None}   # 단위: 백만원
    out["acquirer_is_fund"] = bool(out.get("acquirer") and "조합" in out["acquirer"])
    m = re.search(r"지분율은\s*([\d.]+)\s*%", body)
    out["acquirer_stake_pct"] = float(m.group(1)) if m else None
    out["closing_date"] = _find_date(body, r"(?:잔\s*금|거래종결일?|대금지급일정)[^\n]{0,20}")


def parse(report_nm: str, body: str | None, market_cap: float | None) -> dict | None:
    if not body:
        return None
    title = report_nm.replace(" ", "")
    out: dict = {"is_correction": title.startswith(("[기재정정]", "[첨부정정]", "[발행조건확정]"))}
    if "공급계약" in title or "단일판매" in title:
        _contract(body, out)
    elif "자기주식소각" in title or ("주식소각결정" in title):
        out["kind"] = "자사주소각"
        out["amount"] = _find(body, r"소각\s*예정\s*금액\s*\(원\)")
        out["burn"] = "결정"
    elif "자기주식취득" in title and "해지" not in title:
        _buyback(title, body, out)
    elif "자기주식처분" in title:
        out["kind"] = "자사주처분"
        out["amount"] = _find(body, r"처분\s*예정\s*금액\s*\(원\)")
    elif "유상증자" in title:
        _rights_issue(body, out)
    elif "경영권" in title or "최대주주변경" in title:
        _control_change(body, out)
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


# ---------------------------------------------------------------- 규모 객관 점수
def _by_revenue(r: float) -> int:
    return 5 if r >= 50 else 4 if r >= 20 else 3 if r >= 10 else 2 if r >= 5 else 1


def _by_mcap(r: float) -> int:
    """자사주·지분 취득·시설투자·유증 유입액 (시가총액 대비)."""
    return 5 if r >= 10 else 4 if r >= 5 else 3 if r >= 2 else 2 if r >= 1 else 1


def _by_mcap_contract(r: float) -> int:
    """공급계약 금액의 시가총액 대비 — 매출(이익 아님)이라 자사주보다 기준을 높게."""
    return 5 if r >= 50 else 4 if r >= 20 else 3 if r >= 10 else 2 if r >= 5 else 1


def is_third_party(parsed: dict | None) -> bool:
    return bool(parsed and parsed.get("kind") == "유상증자" and "제3자" in (parsed.get("method") or ""))


def scale_score(parsed: dict | None) -> int | None:
    """채점표 '재료 규모' 객관 점수. 해당 수치가 없으면 None(Claude 판단)."""
    if not parsed:
        return None
    kind = parsed.get("kind")
    mc = parsed.get("market_cap_ratio_pct")
    if kind == "공급계약":
        cand = []
        r = parsed.get("annual_revenue_ratio_pct")
        if r is not None:
            cand.append(_by_revenue(r))
        if mc is not None:
            cand.append(_by_mcap_contract(mc))
        return max(cand) if cand else None
    if kind == "자사주취득" and mc is not None:
        s = _by_mcap(mc) - (1 if parsed.get("method") == "신탁" else 0)
        return max(1, min(5, s))
    if kind == "자사주소각" and mc is not None:
        return max(1, min(5, _by_mcap(mc) + 1))
    if kind in ("타법인주식취득", "시설투자") and mc is not None:
        return _by_mcap(mc)
    if is_third_party(parsed) and mc is not None:
        return _by_mcap(mc)
    return None


def scale_basis(parsed: dict | None) -> str | None:
    """규모 점수가 어떤 수치에서 나왔는지 한 줄 (다이제스트·근거용)."""
    if not parsed:
        return None
    k, mc = parsed.get("kind"), parsed.get("market_cap_ratio_pct")
    parts = []
    if k == "공급계약":
        if parsed.get("revenue_ratio_pct") is not None:
            r = f"매출 대비 {parsed['revenue_ratio_pct']}%"
            if parsed.get("years") and parsed["years"] > 1:
                r += f"(계약 {parsed['years']}년 → 연환산 {parsed['annual_revenue_ratio_pct']}%)"
            parts.append(r)
        if mc is not None:
            parts.append(f"시총 대비 {mc}%")
        parts.append("두 기준 중 높은 점수")
    elif k == "자사주취득" and mc is not None:
        parts.append(f"시총 대비 {mc}%" + (" · 신탁 −1" if parsed.get("method") == "신탁" else " · 직접취득"))
    elif k == "자사주소각" and mc is not None:
        parts.append(f"시총 대비 {mc}% · 소각 결정 +1")
    elif is_third_party(parsed) and mc is not None:
        parts.append(f"회사 유입액 시총 대비 {mc}% (제3자배정)")
    elif mc is not None:
        parts.append(f"시총 대비 {mc}%")
    return " · ".join(parts) or None
