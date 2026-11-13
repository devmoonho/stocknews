"""종목 가격 맥락 — 시가총액(최근 종가 기준), 최근 5·20일 상승률, 20일 평균 거래대금, 선반영(preprice) 객관 점수.

일봉은 price_daily(키움 ka10081 캐시). 없으면 ensure_daily()로 받아 둔다(match.py·score.py에서 호출).
asof = 재료 직전 거래일(수집 시간대 시작일, 보통 전일). 그날 종가까지만 본다(재료 이후 가격은 쓰지 않음).
키움 일봉 종가는 KRX+NXT 통합(애프터마켓 20:00까지) → asof '종가'에는 저녁 공시에 대한 시간외 반응이 들어 있다.
KRX 종가는 다음 거래일 행의 base_price(기준가)로 알 수 있다(아침 실행 땐 아직 없음 → 마스터 lastPrice로 대신).
"""
from __future__ import annotations

import json
import statistics

# 선반영 점수 기준 (채점표 'preprice'와 같게 유지)
PREPRICE_RULES = "5: 5일 ≤+5% & 20일 ≤+15% · 4: ≤+10% & ≤+30% · 3: ≤+20% & ≤+50% · 2: 그 이상 또는 투자주의·단기과열 · " \
                 "1: 투자경고·위험 또는 5일 >+30% · 시간외(NXT) +5%↑ 최대 3, +10%↑ 최대 2"


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


def krx_close(conn, code: str, asof: str, raw: str | None = None) -> float | None:
    """asof의 KRX 종가: 다음 거래일 기준가(base_price) → 없으면(아침 실행) 종목 마스터 lastPrice(전일 KRX 종가)."""
    r = conn.execute("SELECT base_price FROM price_daily WHERE code=? AND dt>? ORDER BY dt LIMIT 1", (code, asof)).fetchone()
    if r and r[0]:
        return r[0]
    try:
        return float(int(json.loads(raw or "{}")["lastPrice"])) or None
    except (KeyError, ValueError, TypeError):
        return None


def context(conn, code: str, asof: str, raw: str | None = None) -> dict | None:
    rows = conn.execute("SELECT dt, close, volume FROM price_daily WHERE code=? AND dt<=? ORDER BY dt DESC LIMIT 21",
                        (code, asof)).fetchall()

    rows = [r for r in rows if r[1]]
    if not rows:
        return None
    close = rows[0][1]

    def ret(n: int) -> float | None:
        return round((close / rows[n][1] - 1) * 100, 1) if len(rows) > n and rows[n][1] else None
    values = [r[1] * r[2] for r in rows[:20] if r[2]]
    # 통합 종가와 KRX 종가의 차이 = NXT 애프터마켓 반응(저녁 공시 직후 등). asof 행이 그날 것일 때만
    krx = krx_close(conn, code, asof, raw) if rows[0][0] == asof else None
    return {"asof": rows[0][0], "close": close, "ret5_pct": ret(5), "ret20_pct": ret(20),
            "avg_value20": round(statistics.fmean(values)) if values else None,
            "krx_close": krx, "after_hours_pct": round((close / krx - 1) * 100, 1) if krx else None}


def market_cap(conn, code: str, raw: str | None, asof: str | None = None) -> float | None:
    """상장주식수 × asof KRX 종가(없으면 그 이전 통합 종가, 그것도 없으면 마스터 lastPrice)."""
    try:
        d = json.loads(raw or "{}")
        shares = int(d["listCount"])
    except (KeyError, ValueError, TypeError):
        return None
    price = krx_close(conn, code, asof, raw) if asof else None     # 시간외 반응이 섞이지 않은 KRX 종가
    if price is None and asof:
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
    ah = ctx.get("after_hours_pct")
    if ah is not None and ah >= 10:                # 저녁 공시 뒤 NXT 애프터마켓에서 이미 반응
        s = min(s, 2)
    elif ah is not None and ah >= 5:
        s = min(s, 3)
    tags = [f"5일 {r5:+.1f}%", f"20일 {r20:+.1f}%"]
    if ah is not None and abs(ah) >= 1:
        tags.append(f"시간외 {ah:+.1f}%")
    if audit != "정상":
        tags.append(audit)
    if str(order_warning or "0") == "3":
        tags.append("단기과열")
    return {"score": s, "reason": f"{' · '.join(tags)} ({ctx['asof']} 종가 기준) — 객관 점수"}


# ---------------------------------------------------------------- 저녁(장 마감 후) 시세 — 1분봉
KRX_CLOSE_HHMM = "1530"


def evening_quote(bars: list[dict], date: str, disclosure_at: str | None = None) -> dict | None:
    """키움 ka10080 1분봉(최신순, 당일 NXT 애프터마켓 포함) → 오늘 KRX 종가·현재가·시간외 등락·시간외 거래대금·공시 후 등락.

    disclosure_at: 'YYYY-MM-DD HH:MM:SS' — 그 시각 직전 마지막 봉 가격 대비 지금 등락.
    """
    from .tracking import _num
    ymd = date.replace("-", "")
    day = sorted((b for b in bars if str(b.get("cntr_tm", "")).startswith(ymd)), key=lambda b: b["cntr_tm"])
    if not day:
        return None
    hm = lambda b: b["cntr_tm"][8:12]  # noqa: E731
    regular = [b for b in day if hm(b) <= KRX_CLOSE_HHMM]
    after = [b for b in day if hm(b) > KRX_CLOSE_HHMM]
    if not regular:
        return None
    krx = _num(regular[-1].get("cur_prc"))
    now = _num(day[-1].get("cur_prc"))
    vol = sum(_num(b.get("trde_qty")) or 0 for b in after)
    value = sum((_num(b.get("trde_qty")) or 0) * (_num(b.get("cur_prc")) or 0) for b in after)
    out = {"krx_close": krx, "price": now, "as_of": f"{day[-1]['cntr_tm'][8:10]}:{day[-1]['cntr_tm'][10:12]}",
           "after_hours_pct": round((now / krx - 1) * 100, 1) if krx and now else None,
           "after_volume": vol, "after_value": round(value), "after_bars": len(after),
           # 18:00 이후 체결 = KRX 시간외 단일가(16:00~18:00)가 아닌 NXT 애프터마켓 체결 → 실제로 거래 가능하다는 증거
           "nxt_traded": any(hm(b) >= "1800" for b in after)}
    if disclosure_at:
        t = disclosure_at.replace("-", "").replace(" ", "").replace(":", "")[:12]     # YYYYMMDDHHMM
        before = [b for b in day if b["cntr_tm"][:12] < t]
        ref = _num(before[-1].get("cur_prc")) if before else None
        out["since_disclosure_pct"] = round((now / ref - 1) * 100, 1) if ref and now else None
    return out


def fetch_evening_quote(client, code: str, date: str, disclosure_at: str | None = None) -> dict | None:
    body, _, _ = client.call("ka10080", "/api/dostk/chart", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"})
    return evening_quote(body.get("stk_min_pole_chart_qry") or [], date, disclosure_at)


def evening_context(conn, code: str, date: str, quote: dict | None) -> dict | None:
    """저녁 선반영 계산용 맥락: 어제까지 통합 종가 + 오늘 KRX 종가로 5·20일 상승률, 지금 시간외 등락."""
    if not quote or not quote.get("krx_close"):
        return None
    hist = [r[0] for r in conn.execute("SELECT close FROM price_daily WHERE code=? AND dt<? ORDER BY dt DESC LIMIT 20",
                                       (code, date)) if r[0]]
    seq = [quote["krx_close"], *hist]

    def ret(n: int) -> float | None:
        return round((seq[0] / seq[n] - 1) * 100, 1) if len(seq) > n else None
    return {"asof": f"{date} 15:30", "close": quote["krx_close"], "ret5_pct": ret(5), "ret20_pct": ret(20),
            "krx_close": quote["krx_close"], "after_hours_pct": quote.get("after_hours_pct")}
