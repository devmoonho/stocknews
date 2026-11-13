#!/usr/bin/env python3
"""장 마감 테마 집계 (cron 평일 15:35, 실패 시 15:50 --retry) — 테마 묶음별 강도·서열·단계 + 그날 아침 예측 채점.

    python3 scripts/theme_daily.py                  # 15:35: 전 종목 KRX 스냅샷(ka10032) → 집계 → 예측 채점
    python3 scripts/theme_daily.py --retry          # 15:50: 오늘 성공 기록이 있으면 바로 끝, 실패하면 텔레그램 알림
    python3 scripts/theme_daily.py --rebuild 2026-11-13            # 통합 일봉(price_daily)으로 근사 재구성(approx=1)
    python3 scripts/theme_daily.py --rebuild-from 2026-10-16       # 그날부터 마지막 완성 거래일까지 차례로 재구성
    python3 scripts/theme_daily.py --groups         # 테마 묶음만 다시 계산하고 큰 묶음 출력

16:00 마감 규칙: 스냅샷 마지막 페이지를 받은 시각이 15:59:30을 넘으면 시간외 가격이 섞일 수 있어 저장하지 않고 실패.
1분봉(상한가 시각)은 15:30 이전 봉만 쓰므로 16:00 뒤에 받아도 된다. 주문 기능 없음.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from stock_news import db  # noqa: E402
from stock_news.analysis import theme_forecast, theme_groups, themes_daily  # noqa: E402
from stock_news.config import DATA_DIR, load_env  # noqa: E402
from stock_news.timewindow import is_trading_day, load_holidays, now_kst, prev_trading_day  # noqa: E402

CUTOFF = dt.time(15, 59, 30)
INDEX = {"KOSPI": "001", "KOSDAQ": "101"}


def _f(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 시세 받기
def fetch_snapshot(client, date: str, force_time: bool = False) -> dict[str, dict]:
    """ka10032(거래대금 상위) 전 페이지 = 전 종목 KRX 시세. 16:00 전에 다 받아야 유효."""
    rows = client.call_all("ka10032", "/api/dostk/rkinfo", {"mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": "1"},
                           "trde_prica_upper", max_pages=80)
    done = now_kst()
    if not force_time and (done.date().isoformat() != date or done.time() > CUTOFF):
        raise RuntimeError(f"스냅샷 완료 {done:%H:%M:%S} — 15:59:30 이후라 시간외 가격이 섞였을 수 있음(저장 안 함)")
    snap = {}
    for r in rows:
        code = str(r.get("stk_cd", "")).split("_")[0]
        close, chg = _f(r.get("cur_prc")), _f(r.get("flu_rt"))
        if not code or close is None or chg is None:
            continue
        snap[code] = {"close": abs(close), "change_pct": chg, "trade_value": (_f(r.get("trde_prica")) or 0) * 1e6,
                      "volume": _f(r.get("now_trde_qty"))}
    if len(snap) < 2000:
        raise RuntimeError(f"스냅샷 종목 수가 너무 적음({len(snap)}) — 응답 형식 변경 의심")
    return snap


def snapshot_from_daily(conn, date: str) -> dict[str, dict]:
    """--rebuild: 통합 일봉으로 근사(종가는 NXT 포함, 등락은 KRX 기준가 대비, 거래대금은 거래량×종가)."""
    snap = {}
    prev = {r["code"]: r["close"] for r in conn.execute(
        "SELECT p.code, p.close FROM price_daily p JOIN (SELECT code, MAX(dt) AS d FROM price_daily WHERE dt<? GROUP BY code) m "
        "ON p.code=m.code AND p.dt=m.d", (date,))}
    for r in conn.execute("SELECT code, close, volume, base_price FROM price_daily WHERE dt=?", (date,)):
        base = r["base_price"] or prev.get(r["code"])
        if not r["close"] or not base:
            continue
        snap[r["code"]] = {"close": r["close"], "change_pct": round((r["close"] / base - 1) * 100, 2),
                           "trade_value": (r["volume"] or 0) * r["close"], "volume": r["volume"]}
    return snap


def index_pct_live(client, date: str) -> dict[str, float]:
    out = {}
    for mk, code in INDEX.items():
        body, _, _ = client.call("ka20006", "/api/dostk/chart", {"inds_cd": code, "base_dt": date.replace("-", "")})
        rows = sorted((r for r in body.get("inds_dt_pole_qry") or [] if r.get("dt")), key=lambda r: r["dt"], reverse=True)
        if len(rows) >= 2 and rows[0]["dt"] == date.replace("-", ""):
            a, b = _f(rows[0]["cur_prc"]), _f(rows[1]["cur_prc"])
            if a and b:
                out[mk] = round((abs(a) / abs(b) - 1) * 100, 3)
    return out


def index_pct_db(conn, date: str) -> dict[str, float]:
    out = {}
    for mk, code in INDEX.items():
        rows = conn.execute("SELECT dt, close FROM index_daily WHERE code=? AND dt<=? ORDER BY dt DESC LIMIT 2",
                            (code, date)).fetchall()
        if len(rows) == 2 and rows[0]["dt"] == date and rows[1]["close"]:
            out[mk] = round((rows[0]["close"] / rows[1]["close"] - 1) * 100, 3)
    return out


def fetch_intraday(client, codes: list[str], date: str, log) -> tuple[dict[str, dict], dict[str, list]]:
    from stock_news.tracking import intraday_pattern
    pats, bars = {}, {}
    ymd = date.replace("-", "")
    for c in codes:
        try:
            body, _, _ = client.call("ka10080", "/api/dostk/chart", {"stk_cd": c, "tic_scope": "1", "upd_stkpc_tp": "1"})
        except Exception as e:  # noqa: BLE001
            log(f"  {c} 1분봉 실패: {e}")
            continue
        day = [b for b in body.get("stk_min_pole_chart_qry") or []
               if str(b.get("cntr_tm", "")).startswith(ymd) and b["cntr_tm"][8:12] <= "1530"]   # 정규장만
        p = intraday_pattern(day, date)
        if p:
            pats[c], bars[c] = p, day
    return pats, bars


# ---------------------------------------------------------------- 집계
def save_snapshot(conn, date: str, snap: dict, approx: int) -> None:
    conn.execute("DELETE FROM market_daily WHERE dt=?", (date,))
    conn.executemany("INSERT INTO market_daily VALUES (?,?,?,?,?,?,?)",
                     [(date, c, s["close"], s["change_pct"], s["trade_value"], s["volume"], approx) for c, s in snap.items()])
    conn.commit()


def history(conn, date: str, codes: set[str], days: int = 20) -> tuple[list[str], dict[str, dict[str, dict]]]:
    dts = [r[0] for r in conn.execute("SELECT DISTINCT dt FROM market_daily WHERE dt<? ORDER BY dt DESC LIMIT ?",
                                      (date, days))]
    hist: dict[str, dict[str, dict]] = {}
    if dts:
        q = ",".join("?" * len(dts))
        for r in conn.execute(f"SELECT dt, code, change_pct, trade_value FROM market_daily WHERE dt IN ({q})", dts):
            if r["code"] in codes:
                hist.setdefault(r["code"], {})[r["dt"]] = {"change_pct": r["change_pct"], "trade_value": r["trade_value"]}
    return dts, hist


def group_tv_avg(groups, dts, hist, min_days: int = 5) -> dict[str, float]:
    out = {}
    for g in groups:
        sums = []
        for d in dts:
            vals = [hist[c][d]["trade_value"] for c in g["members"] if c in hist and d in hist[c] and hist[c][d]["trade_value"]]
            if vals:
                sums.append(sum(vals))
        if len(sums) >= min_days:
            out[g["group_id"]] = statistics.fmean(sums)
    return out


def run_day(conn, date: str, snap: dict, index_pct: dict, client, approx: int, log,
            intraday: bool = True) -> tuple[list[dict], list[dict]]:
    groups = theme_groups.load_groups(conn)
    fcfg = theme_groups.load_config()
    lcfg = themes_daily.load_lifecycle()
    markets = {c: m for c, m in conn.execute("SELECT code, market FROM stocks")}
    allcodes = {c for g in groups for c in g["members"]}
    dts, hist = history(conn, date, allcodes)
    streaks = themes_daily.stock_streaks({c: [snap.get(c, {}).get("change_pct")] +
                                          [hist.get(c, {}).get(d, {}).get("change_pct") for d in dts] for c in allcodes})
    tv_avg = group_tv_avg(groups, dts, hist)
    pday = conn.execute("SELECT MAX(dt) FROM theme_daily WHERE dt<?", (date,)).fetchone()[0]
    prev_rows = {r["group_id"]: dict(r) for r in conn.execute("SELECT * FROM theme_daily WHERE dt=?", (pday,))} if pday else {}
    mn = fcfg["groups"]["min_members"]

    t_rows, m_rows = themes_daily.compute(date, groups, snap, index_pct, markets, {}, prev_rows, tv_avg, streaks, lcfg, mn)
    pats, bars = {}, {}
    if intraday and client is not None:
        top = sorted((r for r in t_rows if r["rank"]), key=lambda r: r["rank"])[:fcfg["intraday_top_groups"]]
        top_ids = {r["group_id"] for r in top}
        want = []
        for gid in top_ids:
            ms = sorted((m for m in m_rows if m["group_id"] == gid), key=lambda m: -(m["change_pct"] or 0))
            want += [m["code"] for m in ms[:fcfg["intraday_per_group"]]]
        # 상한가 근처(+25%↑) 테마 편입 종목은 순위와 상관없이 시각을 받는다(대장 서열의 1순위 기준)
        want += [c for c in allcodes if (snap.get(c, {}).get("change_pct") or 0) >= 25]
        want = list(dict.fromkeys(want))
        t0 = time.monotonic()
        pats, bars = fetch_intraday(client, want, date, log)
        log(f"  1분봉 {len(pats)}/{len(want)}종목 ({time.monotonic() - t0:.0f}초)")
        t_rows, m_rows = themes_daily.compute(date, groups, snap, index_pct, markets, pats, prev_rows, tv_avg, streaks,
                                              lcfg, mn)
    # 2등주 추종: 대장이 상한가 도달·유지한 묶음에서, 도달 시각 뒤 2등주 추가 상승
    by_g: dict[str, list[dict]] = {}
    for m in m_rows:
        by_g.setdefault(m["group_id"], []).append(m)
    for gid, ms in by_g.items():
        ld = next((m for m in ms if m["label"] == "대장"), None)
        if not (ld and ld.get("limit_hit") and ld.get("closed_at_limit") and ld.get("first_hit")):
            continue
        for m in ms:
            if m["label"] == "2등주" and m["code"] in bars:
                m["after_leader_pct"] = themes_daily.after_leader(ld["first_hit"], bars[m["code"]], date)
    for r in t_rows:
        r["approx"] = approx
    conn.execute("DELETE FROM theme_daily WHERE dt=?", (date,))
    conn.execute("DELETE FROM theme_members_daily WHERE dt=?", (date,))
    for r in t_rows:
        conn.execute(f"INSERT INTO theme_daily ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    for m in m_rows:
        conn.execute(f"INSERT INTO theme_members_daily ({', '.join(m)}) VALUES ({', '.join('?' * len(m))})", list(m.values()))
    if pday:   # 전일 서열 종목의 D+1
        conn.executemany("UPDATE theme_members_daily SET next_change_pct=?, next_excess=? WHERE dt=? AND code=?",
                         [(m["change_pct"], m["excess"], pday, m["code"]) for m in
                          {m["code"]: m for m in m_rows}.values()])
    conn.commit()
    return t_rows, m_rows


def summary(conn, date: str, t_rows: list[dict], log) -> None:
    names = {c: n for c, n in conn.execute("SELECT code, name FROM stocks")}
    ranked = sorted((r for r in t_rows if r["rank"]), key=lambda r: r["rank"])
    log(f"묶음 {len(t_rows)}개(순위 대상 {len(ranked)}, 소형 {sum(r['small'] for r in t_rows)}) — 상위 10:")
    for r in ranked[:10]:
        log(f"  {r['rank']:>2}. {r['group_name'][:28]:<28} 중앙값 {r['median_excess']:+.1f}%p · 평균 {r['avg_excess']:+.1f} · "
            f"상승 {r['rise_ratio'] * 100:.0f}% · +5% {r['up5']} · 상한가 {r['limit_ups']} · {r['stage'] or '-'}"
            f"({r['streak']}일) · 대장 {names.get(r['leader_code'], '-')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=f"{now_kst():%Y-%m-%d}")
    ap.add_argument("--retry", action="store_true", help="15:50 재시도: 오늘 성공 기록이 있으면 끝, 또 실패하면 알림")
    ap.add_argument("--rebuild", help="이 날짜를 통합 일봉으로 근사 재구성")
    ap.add_argument("--rebuild-from", help="이 날짜부터 마지막 완성 거래일까지 근사 재구성")
    ap.add_argument("--groups", action="store_true", help="테마 묶음만 다시 계산")
    ap.add_argument("--force", action="store_true", help="휴장일·16:00 마감 규칙 무시(시험용)")
    ap.add_argument("--no-intraday", action="store_true")
    a = ap.parse_args()
    load_env()
    conn = db.connect()
    log = lambda m: print(m, flush=True)  # noqa: E731

    if a.groups:
        theme_groups.rebuild(conn, log)
        return 0
    hol = load_holidays()
    if a.rebuild or a.rebuild_from:
        return rebuild(conn, a, hol, log)

    today = dt.date.fromisoformat(a.date)
    if not a.force and not is_trading_day(today, hol):
        log(f"{a.date} 휴장일 — 실행 안 함")
        return 0
    lock = (DATA_DIR / "theme_daily.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("이미 실행 중")
        return 0
    if a.retry and conn.execute("SELECT 1 FROM theme_runs WHERE dt=? AND ok=1", (a.date,)).fetchone():
        log(f"{a.date} 테마 집계 이미 성공 — 재시도 안 함")
        return 0
    started = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    log(f"===== 테마 집계 {a.date} 시작 {started[11:]} {'(재시도)' if a.retry else ''} =====")
    try:
        from stock_news.master.kiwoom_client import KiwoomClient
        client = KiwoomClient()
        t0 = time.monotonic()
        snap = fetch_snapshot(client, a.date, force_time=a.force)
        save_snapshot(conn, a.date, snap, 0)
        log(f"스냅샷 {len(snap)}종목 ({time.monotonic() - t0:.0f}초, {now_kst():%H:%M:%S} 완료)")
        idx = index_pct_live(client, a.date)
        log(f"지수 {idx}")
        t_rows, _ = run_day(conn, a.date, snap, idx, client, 0, log, intraday=not a.no_intraday)
        summary(conn, a.date, t_rows, log)
        res = theme_forecast.verify(conn, a.date)
        for r in res:
            log(f"예측 채점 {'저녁1차' if r['phase'] == 'evening' else '아침최종'} {r['kind']}"
                f"{'' if r['dedupe'] else '(중복 회피 없음)'}: 상위10 적중 {r['top10_hits']}/{r['n']} · "
                f"평균 초과 {r['avg_excess']} · 대장 {r['leader_hits']}/{r['leader_n']}")
        conn.execute("INSERT INTO theme_runs VALUES (?,?,?,1,?)",
                     (a.date, started, f"{now_kst():%Y-%m-%d %H:%M:%S}", f"묶음 {len(t_rows)}"))
        conn.commit()
        log(f"===== 끝 {now_kst():%H:%M:%S} =====")
        return 0
    except Exception as e:  # noqa: BLE001
        conn.execute("INSERT INTO theme_runs VALUES (?,?,?,0,?)", (a.date, started, f"{now_kst():%Y-%m-%d %H:%M:%S}", repr(e)[:300]))
        conn.commit()
        log(f"✖ 테마 집계 실패: {e!r}")
        if a.retry or now_kst().time() >= dt.time(15, 50):     # 재시도 몫이 없으면(15:50 이후 실패) 바로 알림
            from morning_auto import alert
            alert(f"❌ 테마 집계 실패 ({a.date}, 15:50 재시도까지)\n{e!r}"[:600] + "\n그날 테마 지표는 비어 있습니다.")
        return 1


def rebuild(conn, a, hol, log) -> int:
    now = now_kst()
    last = now.date() if now.time() >= dt.time(20, 5) else prev_trading_day(now.date(), hol)
    if a.rebuild:
        days = [dt.date.fromisoformat(a.rebuild)]
    else:
        d, days = dt.date.fromisoformat(a.rebuild_from), []
        while d <= last:
            if is_trading_day(d, hol):
                days.append(d)
            d += dt.timedelta(days=1)
    client = None
    if not a.no_intraday:
        from stock_news.master.kiwoom_client import KiwoomClient
        client = KiwoomClient()
    for d in days:
        ds = f"{d:%Y-%m-%d}"
        existing = conn.execute("SELECT MIN(approx) FROM market_daily WHERE dt=?", (ds,)).fetchone()[0]
        if existing == 0:
            log(f"{ds}: 실제 스냅샷이 있어 그대로 사용")
            snap = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM market_daily WHERE dt=?", (ds,))}
            approx = 0
        else:
            snap = snapshot_from_daily(conn, ds)
            if len(snap) < 500:
                log(f"{ds}: 일봉 {len(snap)}종목뿐 — 건너뜀 (backfill_trade_value.py 먼저)")
                continue
            save_snapshot(conn, ds, snap, 1)
            approx = 1
        idx = index_pct_db(conn, ds)
        recent = d == now.date() or (d == last and now.time() < dt.time(8, 0))   # 1분봉(약 900개)은 그날 정규장이 온전한 날만
        t_rows, _ = run_day(conn, ds, snap, idx, client if recent else None, approx, log, intraday=recent)
        log(f"{ds}: 재구성 {'(근사)' if approx else ''} 묶음 {len(t_rows)}개, 지수 {idx}")
        if a.rebuild or d == days[-1]:
            summary(conn, ds, t_rows, log)
        theme_forecast.verify(conn, ds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
