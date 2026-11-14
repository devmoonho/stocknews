"""리포트 종목의 주가 반응 기록 — price_reactions.

D0 = 리포트 날짜(아침에 재료를 보고 장에 들어가는 날).
  gap_pct   = 시가 / 전일 종가 - 1          (재료가 시초가에 얼마나 반영됐나)
  oh_pct    = 고가 / 시가 - 1               (시가 매수 시 장중 최대 상승 여지)
  oc_pct    = KRX 종가 / 시가 - 1           (시가 매수 → 종가; KRX 종가 = 다음 거래일 기준가, 없으면 통합 종가)
  키움 일봉 종가는 KRX+NXT 통합(애프터마켓 20:00까지). gap·상한가는 KRX 기준가(base_price) 기준.
  open_limit = 시가가 상한가(매수 불가) → 주간 리뷰의 시가→종가 목표에서 제외
  close_pct = 종가 / 전일 종가 - 1
  oc_excess = oc_pct - 같은 시장 지수 oc_pct (기본 목표 지표: 시장 전체 움직임을 뺀 것)
  ret_Nd    = D+N 종가 / D0 시가 - 1         (N = 1, 3, 5 거래일)
  price_paths: D0 ~ D+10 매일의 누적 수익률·지수 대비 초과·최고가 — 재료 영향이 며칠 가는지(지속성) 분석용
  intraday_patterns: D0 1분봉으로 상한가 도달·해제 횟수·재도달·종가 유지 (재료 신뢰도 보조 지표)
비교 기준(control): 그날 후보였지만 채점하지 않은 종목 일부.
테마 비교(theme_peer): 테마·정책에서 고른 종목과 같은 테마의 안 고른 상위 종목 — 대장 후보 기준 검증용.
"""
from __future__ import annotations

import datetime as dt
import json

from .config import WORK_DIR
from .master.kiwoom_client import KiwoomClient
from .report import basis_tags, theme_bundles
from .timewindow import now_kst

INDEX = {"KOSPI": "001", "KOSDAQ": "101"}
CONTROL_PER_DAY = 20
THEME_PEERS = 10           # 테마 묶음마다 안 고른 상위 구성종목(연관도 순) 비교 기록
HORIZONS = (1, 3, 5)
PATH_DAYS = 10            # 지속성 분석용 경로: D0 ~ D+10 거래일


def _num(v) -> float | None:
    try:
        return abs(float(str(v).replace(",", "").replace("+", "")))
    except (TypeError, ValueError):
        return None


def _iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _base_price(r: dict) -> float | None:
    """KRX 기준가(= 전일 KRX 종가). 키움 일봉 종가는 KRX+NXT 통합(애프터마켓 20:00까지)이라 전일 종가와 다를 수 있다.
    '전일 대비'(pred_pre)는 KRX 기준가 대비라서 종가 − 전일 대비 = 기준가."""
    cur, chg = _num(r.get("cur_prc")), _signed(r.get("pred_pre"))
    return None if not cur or chg is None else cur - chg


def _partial_day() -> str | None:
    """NXT 애프터마켓 마감(20:00) + 여유 5분 전이면 오늘 날짜 — 그날 통합 일봉은 아직 미완성이라 저장하지 않는다."""
    now = now_kst()
    return f"{now:%Y-%m-%d}" if now.time() < dt.time(20, 5) else None


class PriceStore:
    def __init__(self, conn, client: KiwoomClient) -> None:
        self.conn, self.client = conn, client
        self._fresh: set[str] = set()

    def ensure_stock(self, code: str, need_until: str) -> None:
        last = self.conn.execute("SELECT dt, base_price FROM price_daily WHERE code=? ORDER BY dt DESC LIMIT 1",
                                 (code,)).fetchone()
        # base_price(KRX 기준가)가 없는 옛 캐시는 한 번 다시 받는다
        if code in self._fresh or (last and last[0] >= need_until and last[1] is not None):
            return
        body, _, _ = self.client.call("ka10081", "/api/dostk/chart",
                                      {"stk_cd": code, "base_dt": f"{now_kst():%Y%m%d}", "upd_stkpc_tp": "1"})
        partial = _partial_day()
        rows = [(code, _iso(r["dt"]), _num(r.get("open_pric")), _num(r.get("high_pric")), _num(r.get("low_pric")),
                 _num(r.get("cur_prc")), _num(r.get("trde_qty")), _base_price(r))
                for r in body.get("stk_dt_pole_chart_qry") or []
                if r.get("dt") and _iso(r["dt"]) != partial]        # 장 마감 전 당일 봉은 미완성 → 저장 안 함
        self.conn.executemany("INSERT OR REPLACE INTO price_daily (code, dt, open, high, low, close, volume, base_price) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._fresh.add(code)

    def ensure_index(self) -> None:
        for code in INDEX.values():
            if code in self._fresh:
                continue
            body, _, _ = self.client.call("ka20006", "/api/dostk/chart",
                                          {"inds_cd": code, "base_dt": f"{now_kst():%Y%m%d}"})
            partial = _partial_day()
            rows = [(code, _iso(r["dt"]), _num(r.get("open_pric")), _num(r.get("high_pric")), _num(r.get("low_pric")),
                     _num(r.get("cur_prc"))) for r in body.get("inds_dt_pole_qry") or []
                    if r.get("dt") and _iso(r["dt"]) != partial]
            self.conn.executemany("INSERT OR REPLACE INTO index_daily VALUES (?, ?, ?, ?, ?, ?)", rows)
            self._fresh.add(code)

    def series(self, table: str, code: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} WHERE code=? ORDER BY dt", (code,))]


def tick_size(price: float) -> int:
    """KRX 호가단위(2023년 이후 코스피·코스닥 공통)."""
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < lim:
            return t
    return 1000


def limit_up(prev_close: float) -> float:
    raw = prev_close * 1.3
    t = tick_size(raw)
    return float(int(raw // t) * t)


def _signed(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def intraday_pattern(bars: list[dict], date: str) -> dict | None:
    """키움 ka10080 1분봉(최신순) → 그날 상한가 도달·해제·재도달 패턴.

    전일 종가는 분봉의 '전일 대비'(pred_pre)로 역산한다(같은 응답 안에서 일관된 값).
    해제 = 상한가에 닿은 뒤 어떤 1분봉 종가가 상한가 아래로 내려온 구간의 수.
    """
    ymd = date.replace("-", "")
    day = sorted((b for b in bars if str(b.get("cntr_tm", "")).startswith(ymd)), key=lambda b: b["cntr_tm"])
    if not day:
        return None
    last = day[-1]
    cur, chg = _num(last.get("cur_prc")), _signed(last.get("pred_pre"))
    if not cur or chg is None:
        return None
    prev = cur - chg
    lim = limit_up(prev)
    hi = max(_num(b.get("high_pric")) or 0 for b in day)
    at = [(_num(b.get("cur_prc")) or 0) >= lim for b in day]
    touched = [(_num(b.get("high_pric")) or 0) >= lim for b in day]
    first = next((i for i, x in enumerate(touched) if x), None)
    releases, rehit, state = 0, False, None
    if first is not None:
        state = True
        for i in range(first, len(day)):
            if state and not at[i] and not touched[i]:
                releases += 1
                state = False
            elif not state and (at[i] or touched[i]):
                rehit = True
                state = True
    return {"prev_close": prev, "limit_price": lim, "high": hi, "close": cur, "hit": int(first is not None),
            "first_hit": f"{day[first]['cntr_tm'][8:10]}:{day[first]['cntr_tm'][10:12]}" if first is not None else None,
            "releases": releases, "rehit": int(rehit), "closed_at_limit": int(cur >= lim),
            "minutes_at_limit": sum(at), "bars": len(day)}


def record_intraday(conn, client: KiwoomClient, date: str, codes: list[str], log=print) -> int:
    """채점 종목의 D0 장중 패턴 → intraday_patterns. 1분봉은 최근 약 2거래일치만 받을 수 있어 그날·다음날 실행용."""
    n = 0
    for code in codes:
        try:
            body, _, _ = client.call("ka10080", "/api/dostk/chart", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"})
        except Exception as e:  # noqa: BLE001
            log(f"  {date} {code} 분봉 조회 실패: {e}")
            continue
        pat = intraday_pattern(body.get("stk_min_pole_chart_qry") or [], date)
        if not pat:
            continue
        row = {"date": date, "stock_code": code, **pat, "updated_at": f"{now_kst():%Y-%m-%d %H:%M:%S}"}
        conn.execute(f"INSERT OR REPLACE INTO intraday_patterns ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        n += 1
    conn.commit()
    return n


def _targets(conn, date: str) -> list[dict]:
    """그날 채점 종목 + 비교 기준 종목."""
    out, seen = [], set()
    scored_path = WORK_DIR / date / "scored.json"
    items = json.loads(scored_path.read_text(encoding="utf-8"))["items"] if scored_path.exists() else [
        {"code": r["stock_code"], "sentiment": r["sentiment"], "final": r["final"], "final_raw": r["final_raw"],
         "sources": json.loads(r["sources"] or "[]"), "id": "S-"}
        for r in conn.execute("SELECT * FROM scores WHERE date=?", (date,))]
    for it in items:
        out.append({"code": it["code"], "grp": "scored", "sentiment": it["sentiment"], "final": it.get("final"),
                    "final_raw": it.get("final_raw"), "basis": ",".join(basis_tags(conn, it))})
        seen.add(it["code"])
    # 장 전 추가(07:45) 채점 종목도 그날(D0) 반응을 기록 — 아침 리포트에 이미 있는 종목은 아침 기준
    pre_path = WORK_DIR / f"{date}_preopen" / "scored.json"
    if pre_path.exists():
        for it in json.loads(pre_path.read_text(encoding="utf-8")).get("items", []):
            if it["code"] in seen or it.get("fallback"):
                continue
            out.append({"code": it["code"], "grp": "scored", "sentiment": it["sentiment"], "final": it.get("final"),
                        "final_raw": it.get("final_raw"), "basis": ",".join(basis_tags(conn, it))})
            seen.add(it["code"])
    cpath = WORK_DIR / date / "candidates.json"
    if cpath.exists():
        cands = json.loads(cpath.read_text(encoding="utf-8"))
        # 테마 묶음: 고른 종목에 테마·대장 순위를 붙이고, 같은 테마의 안 고른 상위 종목을 비교 종목(theme_peer)으로
        by_code = {t["code"]: t for t in out}
        for b in theme_bundles({"items": items}, cands):
            peers = 0
            for m in b["ranked"]:
                if m["code"] in by_code:
                    by_code[m["code"]].setdefault("theme", b["theme"])
                    by_code[m["code"]].setdefault("leader_rank", m["rank"])
                elif m["code"] not in seen and peers < THEME_PEERS:
                    t = {"code": m["code"], "grp": "theme_peer", "sentiment": None, "final": None, "final_raw": None,
                         "basis": "테마", "theme": b["theme"], "leader_rank": m["rank"]}
                    out.append(t)
                    by_code[m["code"]] = t
                    seen.add(m["code"])
                    peers += 1
        n = 0
        for s in cands.get("stocks", []):          # 근거가 강한 순으로 정렬돼 있음
            if s["code"] in seen or n >= CONTROL_PER_DAY:
                continue
            out.append({"code": s["code"], "grp": "control", "sentiment": None, "final": None, "final_raw": None,
                        "basis": "공시" if s.get("disclosures") else "뉴스"})
            seen.add(s["code"])
            n += 1
    return out


def track_evening(conn, store: "PriceStore", trading_days: list[str], log=print) -> int:
    """저녁 종가매매 리포트 종목의 다음 거래일 반응(진입가 = 리포트 시각 NXT 가격 대비 시가·고가·종가).
    리포트 API의 '이후 주가'(1·3·5일 수익률)용으로 다음 거래일부터 5거래일까지 일봉도 채운다."""
    n = 0
    recent = trading_days[-8:]
    for r in conn.execute("SELECT DISTINCT date, stock_code FROM evening_scores WHERE date>=?",
                          (recent[0] if recent else "9999",)).fetchall():
        later = [d for d in trading_days if d > r["date"]]
        if later:
            try:
                store.ensure_stock(r["stock_code"], later[min(5, len(later) - 1)])
            except Exception as e:  # noqa: BLE001
                log(f"  저녁 {r['date']} {r['stock_code']} 일봉 조회 실패: {e}")
    rows = conn.execute("SELECT date, stock_code, entry_price FROM evening_scores WHERE next_close IS NULL "
                        "AND entry_price IS NOT NULL").fetchall()
    for r in rows:
        nxt = next((d for d in trading_days if d > r["date"]), None)
        if not nxt:
            continue
        try:
            store.ensure_stock(r["stock_code"], nxt)
        except Exception as e:  # noqa: BLE001
            log(f"  저녁 {r['date']} {r['stock_code']} 일봉 조회 실패: {e}")
            continue
        bar = conn.execute("SELECT open, high, close FROM price_daily WHERE code=? AND dt=?", (r["stock_code"], nxt)).fetchone()
        if not bar or not bar["open"]:
            continue
        e = r["entry_price"]
        conn.execute("UPDATE evening_scores SET next_open=?, next_high=?, next_close=?, next_open_ret=?, next_high_ret=?, "
                     "next_close_ret=?, updated_at=? WHERE date=? AND stock_code=?",
                     (bar["open"], bar["high"], bar["close"], bar["open"] / e - 1, bar["high"] / e - 1, bar["close"] / e - 1,
                      f"{now_kst():%Y-%m-%d %H:%M:%S}", r["date"], r["stock_code"]))
        n += 1
    conn.commit()
    return n


def track(conn, client: KiwoomClient, dates: list[str] | None = None, log=print) -> dict:
    """채점이 있는 날짜들의 반응을 채운다(이미 complete면 건너뜀)."""
    today = f"{now_kst():%Y-%m-%d}"
    after_close = now_kst().time() >= dt.time(20, 5)        # 통합 봉(KRX+NXT) 완성 뒤에만 그날을 기록
    if dates is None:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT s.date FROM scores s WHERE NOT EXISTS (SELECT 1 FROM price_reactions p "
            "WHERE p.date=s.date AND p.complete=1 AND p.grp='scored' GROUP BY p.date "
            "HAVING COUNT(*) >= (SELECT COUNT(*) FROM scores s2 WHERE s2.date=s.date)) ORDER BY s.date")]
    store = PriceStore(conn, client)
    store.ensure_index()
    idx = {code: {r["dt"]: r for r in store.series("index_daily", code)} for code in INDEX.values()}
    trading_days = sorted(idx["001"])
    markets = {r["code"]: r["market"] for r in conn.execute("SELECT code, market FROM stocks")}
    stats = {"dates": 0, "rows": 0, "skipped": []}
    for date in dates:
        if date > today or (date == today and not after_close):
            stats["skipped"].append(f"{date}(장 마감 전)")
            continue
        if date not in idx["001"]:
            stats["skipped"].append(f"{date}(지수 데이터 없음/휴장)")
            continue
        pos = trading_days.index(date)
        need_until = trading_days[min(pos + PATH_DAYS, len(trading_days) - 1)]
        stats["dates"] += 1
        targets = _targets(conn, date)
        # 장중 상한가 패턴: 1분봉은 최근 2거래일 정도만 받을 수 있어 D0·D+1 실행 때만 (이미 있으면 건너뜀)
        recent = trading_days[-2:] if trading_days else []
        if date in recent or date == today:
            have = {r[0] for r in conn.execute("SELECT stock_code FROM intraday_patterns WHERE date=?", (date,))}
            codes = [t["code"] for t in targets if t["grp"] == "scored" and t["sentiment"] == "호재" and t["code"] not in have]
            if codes:
                stats["intraday"] = stats.get("intraday", 0) + record_intraday(conn, client, date, codes, log)
        for t in targets:
            try:
                store.ensure_stock(t["code"], need_until)
            except Exception as e:  # noqa: BLE001 — 한 종목 실패로 전체를 멈추지 않음
                log(f"  {date} {t['code']} 일봉 조회 실패: {e}")
                continue
            ser = store.series("price_daily", t["code"])
            by_dt = {r["dt"]: i for i, r in enumerate(ser)}
            if date not in by_dt or by_dt[date] == 0:
                continue                               # 거래정지·신규상장 등
            i = by_dt[date]
            d0, prev = ser[i], ser[i - 1]
            if not (d0["open"] and prev["close"]):
                continue
            mk = "KOSDAQ" if str(markets.get(t["code"], "")).startswith("KOSDAQ") else "KOSPI"
            ix = idx[INDEX[mk]].get(date)
            ix_prev = idx[INDEX[mk]].get(trading_days[pos - 1]) if pos > 0 else None
            vols = [r["volume"] for r in ser[max(0, i - 20):i] if r["volume"]]
            rets = {}
            for n in HORIZONS:
                j = i + n
                rets[n] = (ser[j]["close"] / d0["open"] - 1) if j < len(ser) and ser[j]["dt"] <= today else None
            # KRX 기준: 전일 KRX 종가 = D0 기준가, D0 KRX 종가 = D+1 기준가(아직 없으면 통합 종가)
            krx_prev = d0.get("base_price") or prev["close"]
            nxt = ser[i + 1] if i + 1 < len(ser) and ser[i + 1]["dt"] <= today else None
            krx_close = (nxt or {}).get("base_price") or d0["close"]
            oc = krx_close / d0["open"] - 1
            close_pct = d0["close"] / prev["close"] - 1
            krx_close_pct = krx_close / krx_prev - 1
            idx_oc = (ix["close"] / ix["open"] - 1) if ix and ix["open"] else None
            idx_close = (ix["close"] / ix_prev["close"] - 1) if ix and ix_prev else None
            row = {
                "date": date, "stock_code": t["code"], "grp": t["grp"], "sentiment": t["sentiment"],
                "final": t["final"], "final_raw": t["final_raw"], "basis": t["basis"], "market": mk,
                "prev_close": prev["close"], "open": d0["open"], "high": d0["high"], "low": d0["low"],
                "close": d0["close"], "volume": d0["volume"],
                "vol_ratio20": (d0["volume"] / (sum(vols) / len(vols))) if vols and d0["volume"] else None,
                "gap_pct": d0["open"] / krx_prev - 1, "oh_pct": d0["high"] / d0["open"] - 1,
                "krx_prev_close": krx_prev,
                "after_hours_pct": prev["close"] / krx_prev - 1 if d0.get("base_price") else None,
                "open_limit": int(d0["open"] >= limit_up(krx_prev)),
                "krx_close_excess": krx_close_pct - idx_close if idx_close is not None else None,
                "theme": t.get("theme"), "leader_rank": t.get("leader_rank"),
                "oc_pct": oc, "close_pct": close_pct, "idx_oc_pct": idx_oc, "idx_close_pct": idx_close,
                "oc_excess": oc - idx_oc if idx_oc is not None else None,
                "close_excess": close_pct - idx_close if idx_close is not None else None,
                "ret_1d": rets[1], "ret_3d": rets[3], "ret_5d": rets[5],
                # D+10까지 경로가 다 채워져야 complete (그 전까지는 매일 갱신)
                "complete": int(i + PATH_DAYS < len(ser) and ser[i + PATH_DAYS]["dt"] <= today),
                "updated_at": f"{now_kst():%Y-%m-%d %H:%M:%S}",
            }
            # 날짜별 누적 경로 (지속성 분석)
            ix_ser = idx[INDEX[mk]]
            max_high = 0.0
            for n in range(0, PATH_DAYS + 1):
                j = i + n
                if j >= len(ser) or ser[j]["dt"] > today:
                    break
                day = ser[j]
                max_high = max(max_high, (day["high"] or 0) / d0["open"] - 1)
                ixd = ix_ser.get(day["dt"])
                cum = day["close"] / d0["open"] - 1
                idx_cum = (ixd["close"] / ix["open"] - 1) if (ixd and ix and ix["open"]) else None
                conn.execute("INSERT OR REPLACE INTO price_paths VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (date, t["code"], n, day["dt"], cum, idx_cum,
                              cum - idx_cum if idx_cum is not None else None, max_high))
            cols = ", ".join(row)
            conn.execute(f"INSERT OR REPLACE INTO price_reactions ({cols}) VALUES ({', '.join('?' * len(row))})",
                         list(row.values()))
            stats["rows"] += 1
        conn.commit()
    stats["evening"] = track_evening(conn, store, trading_days, log)
    return stats


# ------------------------------------------------------------------ 리포트 API용 '이후 주가' (DB만 읽음)
def _pct(v):
    return None if v is None else round(v * 100, 2)


def after_prices(conn, code: str, d0: str | None) -> dict:
    """D0(진입일) 이후 주가: 시초가 갭·시가→종가·지수 대비·1·3·5일 수익률(%)과 시가·고가·저가·종가 원값.
    값이 아직 없으면 null, status = '집계 전'(D0 봉 없음) / '집계 중'(일부만) / '완료'.
    D0 봉은 통합 일봉이 완성되는 20:05 이후에 생긴다(track_prices 20:10)."""
    keys = ("open", "high", "low", "close", "gap_pct", "oc_pct", "excess_pct", "ret_1d_pct", "ret_3d_pct", "ret_5d_pct")
    out = {"d0": d0, "status": "집계 전", "close_basis": None, **{k: None for k in keys}}
    if not d0:
        return out
    ser = [dict(r) for r in conn.execute("SELECT * FROM price_daily WHERE code=? AND dt>=date(?, '-20 days') "
                                         "ORDER BY dt", (code, d0))]
    pos = next((i for i, r in enumerate(ser) if r["dt"] == d0), None)
    if pos is None or not ser[pos]["open"]:
        return out
    d = ser[pos]
    prev = ser[pos - 1] if pos > 0 else None
    nxt = ser[pos + 1] if pos + 1 < len(ser) else None
    krx_prev = d.get("base_price") or (prev or {}).get("close")
    krx_close = (nxt or {}).get("base_price")
    close = krx_close or d["close"]
    mk = conn.execute("SELECT market FROM stocks WHERE code=?", (code,)).fetchone()
    ix = conn.execute("SELECT open, close FROM index_daily WHERE code=? AND dt=?",
                      (INDEX["KOSDAQ" if mk and str(mk[0]).startswith("KOSDAQ") else "KOSPI"], d0)).fetchone()
    oc = close / d["open"] - 1
    rets = {n: (ser[pos + n]["close"] / d["open"] - 1) if pos + n < len(ser) else None for n in (1, 3, 5)}
    out.update({"open": d["open"], "high": d["high"], "low": d["low"], "close": close,
                "close_basis": "KRX 종가" if krx_close else "통합 종가(KRX 종가는 다음 거래일 기준가로 확정)",
                "gap_pct": _pct(d["open"] / krx_prev - 1) if krx_prev else None, "oc_pct": _pct(oc),
                "excess_pct": _pct(oc - (ix["close"] / ix["open"] - 1)) if ix and ix["open"] else None,
                "ret_1d_pct": _pct(rets[1]), "ret_3d_pct": _pct(rets[3]), "ret_5d_pct": _pct(rets[5])})
    out["status"] = "완료" if (rets[5] is not None and krx_close) else "집계 중"
    return out
