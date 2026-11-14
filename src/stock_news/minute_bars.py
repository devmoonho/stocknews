"""손익 시나리오용 1분봉 저장 — minute_bars.

키움 ka10080(1분봉, 최신순)을 연속조회하면 약 50거래일 전까지 받을 수 있다.
  - KRX: 일반 코드(005930) → 09:00~15:30 봉만 저장. 전일 KRX 종가 = 전일 15:30 이하 마지막 봉 종가.
  - NXT: '_NX' 코드(005930_NX) → 08:00~08:50 프리마켓 봉만 저장. 봉이 없으면 NXT 불가/거래 없음.
봉 시각(cntr_tm HHMMSS)은 그 1분의 시작 시각이다. 09:00 봉 시가 = KRX 시가(일봉 시가와 일치 확인).
"""
from __future__ import annotations

import json

from .master.kiwoom_client import KiwoomClient
from .timewindow import now_kst

WINDOWS = {"KRX": ("0900", "1530"), "NXT": ("0800", "0850")}
MAX_PAGES = 80


def _num(v) -> float:
    return abs(float(str(v).replace(",", "").replace("+", "") or 0))


def parse_day(raw: list[dict], date: str, venue: str) -> list[list]:
    """키움 봉 목록 → 그날 venue 시간대의 [hhmm, o, h, l, c, v] (시간순)."""
    ymd = date.replace("-", "")
    lo, hi = WINDOWS[venue]
    out = []
    for b in raw:
        t = str(b.get("cntr_tm", ""))
        if not t.startswith(ymd) or not (lo <= t[8:12] <= hi):
            continue
        out.append([t[8:12], _num(b["open_pric"]), _num(b["high_pric"]), _num(b["low_pric"]),
                    _num(b["cur_prc"]), _num(b.get("trde_qty") or 0)])
    out.sort(key=lambda r: r[0])
    return out


def prev_krx_close(raw: list[dict], date: str) -> float | None:
    """date 이전 가장 최근 날짜의 15:30 이하 마지막 봉 종가."""
    ymd = date.replace("-", "")
    best = None
    for b in raw:
        t = str(b.get("cntr_tm", ""))
        if t[:8] < ymd and t[8:12] <= "1530" and (best is None or t > best[0]):
            best = (t, b)
    return _num(best[1]["cur_prc"]) if best else None


def fetch_raw(client: KiwoomClient, stk_cd: str, oldest: str) -> list[dict]:
    """oldest(YYYY-MM-DD) 이전 날짜 봉이 나올 때까지 연속조회 (전일 종가까지 포함되도록)."""
    ymd = oldest.replace("-", "")
    out: list[dict] = []
    cont, nkey = "N", ""
    for _ in range(MAX_PAGES):
        body, cont, nkey = client.call("ka10080", "/api/dostk/chart",
                                       {"stk_cd": stk_cd, "tic_scope": "1", "upd_stkpc_tp": "1"}, cont, nkey)
        page = body.get("stk_min_pole_chart_qry") or []
        out.extend(page)
        if not page or cont != "Y" or not nkey:
            break
        # 전일 정규장(15:30 이하) 봉까지 받았으면 멈춤 — 전일 KRX 종가가 필요
        if any(str(x.get("cntr_tm", ""))[:8] < ymd and str(x.get("cntr_tm", ""))[8:12] <= "1530" for x in page):
            break
    return out


def ensure(conn, client: KiwoomClient, wants: dict[str, set[str]], log=print, force: bool = False) -> dict:
    """wants = {code: {date, ...}} → 없는 (date, code, venue)만 받아 저장. 반환: 통계."""
    stats = {"codes": 0, "rows": 0, "calls_failed": 0, "too_old": 0}
    have = {(r[0], r[1], r[2]) for r in conn.execute("SELECT date, code, venue FROM minute_bars")}
    stamp = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    for code, dates in sorted(wants.items()):
        for venue, suffix in (("KRX", ""), ("NXT", "_NX")):
            need = sorted(d for d in dates if force or (d, code, venue) not in have)
            if not need:
                continue
            try:
                raw = fetch_raw(client, code + suffix, need[0])
            except Exception as e:  # noqa: BLE001 — 한 종목 실패로 전체를 멈추지 않음
                log(f"  분봉 조회 실패 {code}{suffix}: {e}")
                stats["calls_failed"] += 1
                continue
            oldest = min((str(b.get("cntr_tm", ""))[:8] for b in raw), default="99999999")
            for d in need:
                if oldest > d.replace("-", ""):
                    stats["too_old"] += 1          # 키움이 주는 범위보다 오래된 날짜
                    continue
                bars = parse_day(raw, d, venue)
                prev = prev_krx_close(raw, d) if venue == "KRX" else None
                conn.execute("INSERT OR REPLACE INTO minute_bars VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (d, code, venue, prev, json.dumps(bars, separators=(",", ":")), len(bars), stamp))
                stats["rows"] += 1
        stats["codes"] += 1
        conn.commit()
    return stats


def load(conn, dates: list[str] | None = None) -> dict[tuple[str, str], dict]:
    """→ {(date, code): {"KRX": bars, "NXT": bars, "prev_close": float}}"""
    q = "SELECT * FROM minute_bars"
    args: tuple = ()
    if dates:
        q += f" WHERE date IN ({','.join('?' * len(dates))})"
        args = tuple(dates)
    out: dict[tuple[str, str], dict] = {}
    for r in conn.execute(q, args):
        d = out.setdefault((r["date"], r["code"]), {"KRX": [], "NXT": [], "prev_close": None})
        d[r["venue"]] = json.loads(r["bars"] or "[]")
        if r["venue"] == "KRX":
            d["prev_close"] = r["prev_close"]
    return out
