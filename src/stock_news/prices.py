"""종목 가격 맥락 — 시가총액(최근 종가 기준), 최근 5·20일 상승률, 20일 평균 거래대금, 선반영(preprice) 객관 점수.

일봉은 price_daily(키움 ka10081 캐시). 없으면 ensure_daily()로 받아 둔다(match.py·score.py에서 호출).
asof = 재료 직전 거래일(수집 시간대 시작일, 보통 전일). 그날 종가까지만 본다(재료 이후 가격은 쓰지 않음).
"""
from __future__ import annotations

import json
import statistics

# 선반영 점수 기준 (채점표 'preprice'와 같게 유지)
PREPRICE_RULES = "5: 5일 ≤+5% & 20일 ≤+15% · 4: ≤+10% & ≤+30% · 3: ≤+20% & ≤+50% · 2: 그 이상 또는 투자주의·단기과열 · " \
                 "1: 투자경고·위험 또는 5일 >+30%"


def ensure_daily(conn, codes: list[str], asof: str, log=print) -> list[str]:
    """codes의 일봉을 asof까지 채운다. 실패한 종목코드 목록을 돌려준다(네트워크·키 문제면 전부 실패)."""
    from .master.kiwoom_client import KiwoomClient
    from .tracking import PriceStore
    need = [c for c in dict.fromkeys(codes)
            if (conn.execute("SELECT MAX(dt) FROM price_daily WHERE code=?", (c,)).fetchone()[0] or "") < asof]
    if not need:
        return []
    failed = []
    try:
        store = PriceStore(conn, KiwoomClient())
    except Exception as e:  # noqa: BLE001
        log(f"  일봉 조회 불가(키움): {e}")
        return need
    for c in need:
        try:
            store.ensure_stock(c, asof)
        except Exception as e:  # noqa: BLE001 — 한 종목 실패로 멈추지 않음
            failed.append(c)
            log(f"  {c} 일봉 조회 실패: {e}")
    conn.commit()
    return failed


def context(conn, code: str, asof: str) -> dict | None:
    rows = conn.execute("SELECT dt, close, volume FROM price_daily WHERE code=? AND dt<=? ORDER BY dt DESC LIMIT 21",
                        (code, asof)).fetchall()
    rows = [r for r in rows if r[1]]
    if not rows:
        return None
    close = rows[0][1]

    def ret(n: int) -> float | None:
        return round((close / rows[n][1] - 1) * 100, 1) if len(rows) > n and rows[n][1] else None
    values = [r[1] * r[2] for r in rows[:20] if r[2]]
    return {"asof": rows[0][0], "close": close, "ret5_pct": ret(5), "ret20_pct": ret(20),
            "avg_value20": round(statistics.fmean(values)) if values else None}


def market_cap(conn, code: str, raw: str | None, asof: str | None = None) -> float | None:
    """상장주식수 × 최근 종가(asof 이전). 일봉이 없으면 종목 마스터의 lastPrice."""
    try:
        d = json.loads(raw or "{}")
        shares = int(d["listCount"])
    except (KeyError, ValueError, TypeError):
        return None
    price = None
    if asof:
        r = conn.execute("SELECT close FROM price_daily WHERE code=? AND dt<=? ORDER BY dt DESC LIMIT 1",
                         (code, asof)).fetchone()
        price = r[0] if r and r[0] else None
    if price is None:
        try:
            price = int(d["lastPrice"])
        except (KeyError, ValueError, TypeError):
            return None
    return shares * price


def preprice(ctx: dict | None, audit_info: str | None, order_warning: str | None) -> dict | None:
    """선반영 객관 점수 (높을수록 재료 전에 덜 오름). 가격 데이터가 없으면 None."""
    if not ctx or ctx.get("ret5_pct") is None:
        return None
    r5, r20 = ctx["ret5_pct"], ctx["ret20_pct"] if ctx.get("ret20_pct") is not None else ctx["ret5_pct"]
    audit = audit_info or "정상"
    if audit in ("투자경고", "투자위험") or r5 > 30:
        s = 1
    elif audit == "투자주의" or str(order_warning or "0") == "3" or r5 > 20 or r20 > 50:
        s = 2
    elif r5 > 10 or r20 > 30:
        s = 3
    elif r5 > 5 or r20 > 15:
        s = 4
    else:
        s = 5
    tags = [f"5일 {r5:+.1f}%", f"20일 {r20:+.1f}%"]
    if audit != "정상":
        tags.append(audit)
    if str(order_warning or "0") == "3":
        tags.append("단기과열")
    return {"score": s, "reason": f"{' · '.join(tags)} ({ctx['asof']} 종가 기준) — 객관 점수"}
