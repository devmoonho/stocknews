"""전 종목 리스트 갱신 (키움 ka10099 종목정보 리스트) + 후보 제외 플래그.

제외 기준:
  - 상장폐지: 오늘 목록에서 사라진 종목(is_listed=0), 정리매매 중(orderWarning=2)
  - 거래정지: state에 '거래정지'
  - 관리종목: state에 '관리종목'
"""
from __future__ import annotations

import json

from .. import db
from ..health import Warnings, check_fields, check_volume
from ..timewindow import now_kst
from .kiwoom_client import KiwoomClient

SOURCE = "kiwoom_stocks"
MARKETS = {"0": "KOSPI", "10": "KOSDAQ"}        # ka10099 mrkt_tp
REQUIRED = ("code", "name", "state", "orderWarning")
MIN_EXPECTED = {"KOSPI": 700, "KOSDAQ": 1500}   # 이보다 적으면 형식/조회 이상 의심


NON_STOCK_MARKETS = ("ETF", "ETN")             # marketName이 이걸로 시작하면 개별 종목이 아님


def exclusion_reason(row: dict) -> str | None:
    """후보 제외 이유. 거래정지·관리종목은 state와 auditInfo 양쪽에 따로 표시되는 경우가 있어 둘 다 본다
    (2026-11 실응답: auditInfo '거래정지' 30건 vs state 10건, '관리종목' 28건 vs 48건)."""
    reasons = []
    flags = f"{row.get('state') or ''}|{row.get('auditInfo') or ''}"
    if str(row.get("marketName") or "").startswith(NON_STOCK_MARKETS):
        reasons.append(str(row.get("marketName")))
    if "거래정지" in flags:
        reasons.append("거래정지")
    if "관리종목" in flags:
        reasons.append("관리종목")
    if str(row.get("orderWarning") or "").strip() == "2":
        reasons.append("정리매매(상장폐지 예정)")
    return ", ".join(reasons) or None


def update(conn, client: KiwoomClient) -> dict:
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 전 종목 리스트 갱신")
    today = f"{started:%Y-%m-%d}"
    seen: set[str] = set()
    total = 0
    for mrkt_tp, market in MARKETS.items():
        try:
            rows = client.call_all("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": mrkt_tp}, "list")
        except Exception as e:
            warn.add(f"{SOURCE}: {market} 조회 실패 — {e}")
            continue
        if not check_fields(rows, REQUIRED, f"{SOURCE}/{market}", warn):
            continue
        if len(rows) < MIN_EXPECTED[market]:
            warn.add(f"{SOURCE}: {market} {len(rows)}종목 — 예상({MIN_EXPECTED[market]}+)보다 적음")
        for r in rows:
            code = str(r["code"]).strip()
            reason = exclusion_reason(r)
            conn.execute(
                """INSERT INTO stocks (code, name, market, sector, audit_info, state, order_warning,
                                       is_listed, is_excluded, exclude_reason, first_seen, last_seen, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, market=excluded.market, sector=excluded.sector,
                     audit_info=excluded.audit_info, state=excluded.state, order_warning=excluded.order_warning,
                     is_listed=1, is_excluded=excluded.is_excluded, exclude_reason=excluded.exclude_reason,
                     last_seen=excluded.last_seen, raw=excluded.raw""",
                (code, str(r["name"]).strip(), _market_label(market, r), r.get("upName"), r.get("auditInfo"), r.get("state"),
                 r.get("orderWarning"), int(reason is not None), reason, today, today,
                 json.dumps(r, ensure_ascii=False)))
            seen.add(code)
        total += len(rows)
        print(f"  {market} {len(rows)}종목")

    delisted = 0
    if total >= sum(MIN_EXPECTED.values()):     # 목록이 정상일 때만 사라진 종목을 상장폐지로 처리
        cur = conn.execute(
            "UPDATE stocks SET is_listed=0, is_excluded=1, exclude_reason='상장폐지(목록에서 사라짐)' "
            "WHERE is_listed=1 AND last_seen < ?", (today,))
        delisted = cur.rowcount
    excluded = conn.execute("SELECT COUNT(*) FROM stocks WHERE is_listed=1 AND is_excluded=1").fetchone()[0]
    print(f"  상장 {len(seen)}종목, 후보 제외 {excluded}종목, 오늘 사라진 종목 {delisted}개")

    check_volume(conn, SOURCE, total, warn)
    db.log_run(conn, SOURCE, f"{started:%Y-%m-%d %H:%M:%S}", f"{now_kst():%Y-%m-%d %H:%M:%S}",
               _DayWindow(started), total, len(seen), warn)
    conn.commit()
    return {"source": SOURCE, "fetched": total, "excluded": excluded, "delisted": delisted, "warnings": list(warn)}


def _market_label(market: str, row: dict) -> str:
    """KOSPI/KOSDAQ, 단 ETF·ETN·리츠 등은 marketName을 붙여 구분 (예: 'KOSPI:ETF')."""
    name = str(row.get("marketName") or "")
    return market if name in ("", "거래소", "코스닥") else f"{market}:{name}"


class _DayWindow:
    """collect_runs 기록용 (마스터 갱신은 시간대 개념이 없어 실행 시각만 남긴다)."""
    def __init__(self, t):
        self.start = self.end = t
