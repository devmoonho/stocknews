"""일일 손익 시나리오 — 장전 리포트 종목을 1분봉으로 시뮬레이션해 손절·익절 조합을 비교한다.

네트워크를 쓰지 않는다(분봉은 minute_bars에서 미리 받아 둔다) → 단위 테스트 가능. 주문은 하지 않는다.

진입 (모두 한 시점 전량 진입, 진입가 = 진입 봉 시가 × (1 + 진입 슬리피지))
  nxt_early  NXT 08:00 봉 시가     → 경로: NXT 08:00~08:50 + KRX 09:00~15:30
  nxt_late   NXT 08:40 봉 시가     → 경로: NXT 08:40~08:50 + KRX 09:00~15:30
  krx_open   KRX 첫 봉 시가(보통 09:00, 시가 VI면 09:02 등) → 경로: KRX 09:00~15:30
  NXT 진입만 유동성 판정: 진입 봉 거래대금(대표가 (고+저+종)/3 × 거래량) < nxt_min_amount → '유동성 부족'(추천 근거 제외)
  진입 시가가 상한가 → 매수 불가로 제외. 진입 봉이 없으면 '해당 없음'.
경로 판정 (진입 봉부터, 봉의 고가·저가 포함)
  한 봉에서 손절가·익절가가 둘 다 닿으면 손절 먼저(보수적, '모호 봉'으로 셈). 봉 시가가 이미 넘어 있으면 시가 체결.
  손절 체결 = 그 종목 매매 끝(재진입 없음). 끝까지 안 걸리면 15:30 KRX 종가 청산.
익절 방식
  full      전량 익절
  pctX      절반 익절 → 손절선 본전 → 남은 절반은 고점 대비 X% 트레일
  tickN     절반 익절 → 손절선 본전 → 남은 절반은 고점 대비 N호가 트레일 (KRX 호가 단위, 가격대 넘나들면 한 호가씩)
  트레일은 익절 봉 다음 봉부터 본다. 봉 안 순서는 '이전 고점 기준 하락 먼저 확인 → 고점 갱신'(보수적).
비용: 왕복 cost_pct는 모든 거래에서, stop_slippage_pct는 손절·본전·트레일(하락 방향) 체결분에서 추가로 뺀다.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections import defaultdict

from ..config import ROOT
from ..tracking import limit_up, tick_size

CONFIG_PATH = ROOT / "config" / "scenario.json"
ENTRY_LABEL = {"nxt_early": "NXT 초반(08:00 시가)", "nxt_late": "NXT 후반(08:40 시가)", "krx_open": "KRX 시가(09:00)"}
ENTRY_SHORT = {"nxt_early": "NXT 08:00", "nxt_late": "NXT 08:40", "krx_open": "KRX 시가"}
REC_SEGMENTS = ["호재 전체", "2점", "3점", "4점↑", "공시", "뉴스", "정책"]
REF_SEGMENTS = ["악재", "비교 기준"]


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 진입 준비
def amount(bar: list) -> float:
    """1분봉 거래대금 = 대표가((고+저+종)/3) × 거래량."""
    _, _, h, l, c, v = bar
    return (h + l + c) / 3 * v


def make_entries(md: dict, cfg: dict) -> dict[str, dict]:
    """md = {"KRX": bars, "NXT": bars, "prev_close": float} → {entry_mode: {status, price, raw_open, bars, amount}}.
    status: ok / illiquid(NXT 유동성 부족) / limit(시가 상한가) / na(봉 없음)."""
    krx, nxt, prev = md.get("KRX") or [], md.get("NXT") or [], md.get("prev_close")
    close = md.get("krx_close") or (krx[-1][4] if krx else None)     # 15:30 KRX 종가(스냅샷 우선 — 15:30 봉이 빠진 종목 있음)
    lim = limit_up(prev) if prev else None
    out = {}
    for mode in cfg["entry_modes"]:
        if not krx:
            out[mode] = {"status": "na"}
            continue
        if mode == "krx_open":
            i = 0                                # 첫 KRX 봉 = 시가(시가 단일가가 VI로 늦어지면 09:02 등)
            path, slip, liq = (krx[i:] if i is not None else None), cfg["krx_entry_slippage_pct"], False
        else:
            t = cfg["nxt_early_time"] if mode == "nxt_early" else cfg["nxt_late_time"]
            i = next((k for k, b in enumerate(nxt) if b[0] == t), None)
            path, slip, liq = (nxt[i:] + krx if i is not None else None), cfg["nxt_entry_slippage_pct"], True
        if not path:
            out[mode] = {"status": "na"}
            continue
        o = path[0][1]
        e = {"status": "ok", "raw_open": o, "price": o * (1 + slip / 100), "bars": path, "amount": amount(path[0]),
             "close": close}
        if lim and o >= lim:
            e["status"] = "limit"
        elif liq and e["amount"] < cfg["nxt_min_amount"]:
            e["status"] = "illiquid"
        out[mode] = e
    return out


# ------------------------------------------------------------------ 한 진입 경로의 시뮬레이터
def trail_level(peak: float, mode: str) -> float:
    if mode.startswith("pct"):
        return peak * (1 - float(mode[3:]) / 100)
    n = int(mode[4:])
    t = tick_size(peak)
    p = math.floor(peak / t + 1e-9) * t
    for _ in range(n):
        p -= tick_size(p - 0.5)
    return p


class Path:
    """진입 하나(진입가 + 진입 봉부터 15:30까지 봉) — 손절·익절 첫 도달 위치를 이분 탐색으로 찾는다."""

    def __init__(self, entry: float, bars: list[list], cfg: dict, close: float | None = None) -> None:
        self.e, self.bars = entry, bars
        self.close = close or bars[-1][4]              # 미체결 청산가 = 15:30 KRX 종가
        self.cost = cfg["cost_pct"] / 100
        self.slip = cfg["stop_slippage_pct"] / 100
        pmin, pmax, lo, hi = [], [], math.inf, -math.inf
        for b in bars:
            lo, hi = min(lo, b[3]), max(hi, b[2])
            pmin.append(-lo)          # 비감소로 만들어 bisect
            pmax.append(hi)
        self.pmin, self.pmax = pmin, pmax
        self._trail: dict = {}

    def first_low(self, s: float) -> int | None:
        i = bisect_left(self.pmin, -s)
        return i if i < len(self.bars) else None

    def first_high(self, t: float) -> int | None:
        i = bisect_left(self.pmax, t)
        return i if i < len(self.bars) else None

    def _event(self, stop: float, take: float):
        """→ (kind 'stop'|'take'|'close', index, fill, ambiguous)"""
        S, T = self.e * (1 + stop / 100), self.e * (1 + take / 100)
        s, t = self.first_low(S), self.first_high(T)
        if s is None and t is None:
            return "close", len(self.bars) - 1, self.close, False
        if t is None or (s is not None and s < t):
            return "stop", s, min(self.bars[s][1], S), False
        if s is None or t < s:
            return "take", t, max(self.bars[t][1], T), False
        o = self.bars[s][1]                      # 같은 봉: 시가로 이미 넘었으면 그쪽, 아니면 손절 먼저(모호)
        if o >= T:
            return "take", t, o, False
        if o <= S:
            return "stop", s, o, False
        return "stop", s, S, True

    def _rest(self, t: int, fill: float, mode: str):
        """익절 봉 다음부터 남은 절반: → (청산가, 하락 방향 체결 여부)."""
        key = (t, fill, mode)
        if key not in self._trail:
            peak, res = fill, None
            for b in self.bars[t + 1:]:
                lvl = max(self.e, trail_level(peak, mode))
                if b[1] <= lvl:
                    res = (b[1], True)
                    break
                if b[3] <= lvl:
                    res = (lvl, True)
                    break
                peak = max(peak, b[2])
            self._trail[key] = res or (self.close, False)
        return self._trail[key]

    def run(self, stop: float, take: float, mode: str) -> tuple[float, bool, str]:
        """→ (수익률(비용·슬리피지 차감), 모호 봉 여부, 청산 사유)"""
        kind, i, fill, amb = self._event(stop, take)
        r = fill / self.e - 1 - self.cost
        if kind == "stop":
            return r - self.slip, amb, "손절"
        if kind == "close" or mode == "full":
            return r, amb, "종가" if kind == "close" else "익절"
        px, down = self._rest(i, fill, mode)
        r2 = px / self.e - 1 - self.cost
        return 0.5 * r + 0.5 * (r2 - (self.slip if down else 0)), amb, "익절+트레일"


# ------------------------------------------------------------------ 그리드
def modes(cfg: dict) -> list[str]:
    return ["full"] + [f"pct{x:g}" for x in cfg["trail_pct"]] + [f"tick{n}" for n in cfg["trail_ticks"]]


def mode_label(m: str) -> str:
    if m == "full":
        return "전량 익절"
    if m.startswith("pct"):
        return f"절반 익절+트레일 {m[3:]}%"
    return f"절반 익절+트레일 {m[4:]}호가"


def combo_label(c: tuple) -> str:
    stop, take, m = c
    return f"손절 {stop:g}% · 익절 +{take:g}% · {mode_label(m)}"


def stats(rets: list[float], ambs: list[bool] | None = None) -> dict:
    if not rets:
        return {"n": 0, "mean": None, "win": None, "max_loss": None, "pf": None, "amb": 0}
    w = [r for r in rets if r > 0]
    lo = [r for r in rets if r <= 0]
    pf = (sum(w) / len(w)) / abs(sum(lo) / len(lo)) if w and lo and sum(lo) else None
    return {"n": len(rets), "mean": sum(rets) / len(rets), "win": len(w) / len(rets), "max_loss": min(rets),
            "pf": pf, "amb": sum(ambs) if ambs else 0}


class Grid:
    """trades의 진입별 Path를 만들어 두고, (stop, take, mode) 조합 결과를 캐시한다."""

    def __init__(self, trades: list[dict], cfg: dict) -> None:
        self.cfg, self.trades = cfg, trades
        self.paths: dict[tuple[int, str], Path] = {}
        for k, t in enumerate(trades):
            for em, e in t["entries"].items():
                if e["status"] in ("ok", "illiquid"):
                    self.paths[(k, em)] = Path(e["price"], e["bars"], cfg, e.get("close"))
        self.cache: dict = {}

    def result(self, k: int, em: str, combo: tuple):
        key = (k, em, combo)
        if key not in self.cache:
            self.cache[key] = self.paths[(k, em)].run(*combo)
        return self.cache[key]

    def agg(self, idx: list[int], em: str, combo: tuple) -> dict:
        """idx 거래들 중 진입 em이 'ok'인 것만(유동성 부족·상한가·해당 없음 제외)."""
        rs, am = [], []
        for k in idx:
            if self.trades[k]["entries"][em]["status"] == "ok":
                r, a, _ = self.result(k, em, combo)
                rs.append(r)
                am.append(a)
        return stats(rs, am)


def smoothed(table: dict[tuple, dict], stops: list, takes: list, combo: tuple) -> float | None:
    """같은 진입·방식에서 손절 ±1칸 × 익절 ±1칸 이웃 평균 기대수익(표본 있는 칸만)."""
    stop, take, m = combo
    si, ti = stops.index(stop), takes.index(take)
    vals = []
    for a in (si - 1, si, si + 1):
        for b in (ti - 1, ti, ti + 1):
            if 0 <= a < len(stops) and 0 <= b < len(takes):
                s = table.get((stops[a], takes[b], m))
                if s and s["n"]:
                    vals.append(s["mean"])
    return sum(vals) / len(vals) if vals else None


def stable_zone(table: dict[tuple, dict], stops: list, takes: list, rec: tuple, tol: float) -> dict:
    """추천 칸과 이어진(상하좌우) 칸 중 기대수익 ≥ (그 방식 최고 - tol%p)인 칸들의 손절·익절 범위."""
    m = rec[2]
    vals = {(s, t): table[(s, t, m)]["mean"] for s in stops for t in takes
            if table.get((s, t, m)) and table[(s, t, m)]["n"]}
    if not vals:
        return {}
    thr = max(vals.values()) - tol / 100
    seen, todo = {(rec[0], rec[1])}, [(rec[0], rec[1])]
    while todo:
        s, t = todo.pop()
        si, ti = stops.index(s), takes.index(t)
        for a, b in ((si - 1, ti), (si + 1, ti), (si, ti - 1), (si, ti + 1)):
            if 0 <= a < len(stops) and 0 <= b < len(takes):
                c = (stops[a], takes[b])
                if c not in seen and vals.get(c) is not None and vals[c] >= thr:
                    seen.add(c)
                    todo.append(c)
    ss, ts = [c[0] for c in seen], [c[1] for c in seen]
    return {"stop": [max(ss), min(ss)], "take": [min(ts), max(ts)], "cells": len(seen)}


def zone_label(z: dict) -> str:
    if not z:
        return "-"
    s = f"{z['stop'][0]:g}%" if z["stop"][0] == z["stop"][1] else f"{z['stop'][0]:g}~{z['stop'][1]:g}%"
    t = f"+{z['take'][0]:g}%" if z["take"][0] == z["take"][1] else f"+{z['take'][0]:g}~{z['take'][1]:g}%"
    return f"손절 {s} · 익절 {t} ({z['cells']}칸)"


def confidence(n: int, cfg: dict) -> str:
    return "높음" if n >= cfg["conf"]["high"] else "보통" if n >= cfg["conf"]["mid"] else "낮음"


# ------------------------------------------------------------------ 분류·기간
def segments_of(t: dict) -> list[str]:
    if t["grp"] in ("control", "theme_peer"):
        return ["비교 기준"]
    if t.get("sentiment") == "악재":
        return ["악재"]
    out = ["호재 전체"]
    f = t.get("final")
    if f is not None:
        out.append("4점↑" if f >= 4 else f"{f}점" if f in (2, 3) else None)
    tags = [x.strip() for x in str(t.get("basis") or "").split(",")]
    out += [x for x in ("공시", "뉴스", "정책") if x in tags]
    return [x for x in out if x]


def periods(trading_days: list[str], cfg: dict) -> dict[str, set[str]]:
    """trading_days: 실행일까지의 거래일(오름차순). → 기간별 날짜 집합 + 선정 구간(최근 1주 제외)."""
    week = set(trading_days[-cfg["periods"]["week"]:])
    month = set(trading_days[-cfg["periods"]["month"]:])
    alld = set(trading_days)
    return {"week": week, "month": month, "all": alld, "month_sel": month - week, "all_sel": alld - week}


# ------------------------------------------------------------------ 전체 분석
def analyze(trades: list[dict], trading_days: list[str], cfg: dict | None = None,
            prev_recs: dict | None = None) -> dict:
    """trades: [{date, code, name, grp, sentiment, final, basis, entries}] → 추천·표·경고 dict."""
    cfg = cfg or load_config()
    stops, takes, ms = list(cfg["stop"]), list(cfg["take"]), modes(cfg)
    grid = Grid(trades, cfg)
    per = periods(trading_days, cfg)
    seg_idx: dict[str, list[int]] = defaultdict(list)
    for k, t in enumerate(trades):
        for s in segments_of(t):
            seg_idx[s].append(k)

    def subset(seg: str, p: str) -> list[int]:
        return [k for k in seg_idx.get(seg, []) if trades[k]["date"] in per[p]]

    def table_for(stops: list, takes: list):
        def table(idx: list[int], em: str) -> dict[tuple, dict]:
            return {(s, t, m): grid.agg(idx, em, (s, t, m)) for s in stops for t in takes for m in ms}
        return table

    lim = cfg["expand_limit"]
    notes: list[str] = []
    recs, seg_grid = {}, {}
    for seg in REC_SEGMENTS:                         # 범위 자동 확장은 분류마다 따로(작은 분류가 전체 그리드를 넓히지 않게)
        stops, takes = list(cfg["stop"]), list(cfg["take"])
        for _ in range(40):
            r = _recommend(seg, subset, table_for(stops, takes), grid.agg, stops, takes, cfg)
            if not r.get("combo"):
                break
            s, t, _m = r["combo"]
            grow = []
            if s == min(stops) and s > lim["stop"]:
                stops.append(s - 1)
                grow.append(f"손절 {s - 1:g}%")
            elif s == max(stops) and s < lim["stop_inner"]:
                stops.append(s + 1)
                grow.append(f"손절 {s + 1:g}%")
            if t == max(takes) and t < lim["take"]:
                takes.append(t + 1)
                grow.append(f"익절 +{t + 1:g}%")
            elif t == min(takes) and t > lim["take_inner"]:
                takes.append(t - 1)
                grow.append(f"익절 +{t - 1:g}%")
            stops.sort(reverse=True)
            takes.sort()
            if not grow:
                break
        if (stops, takes) != (list(cfg["stop"]), list(cfg["take"])):
            notes.append(f"{seg}: 최적값이 범위 끝에 걸려 손절 {max(stops):g}~{min(stops):g}% · "
                         f"익절 +{min(takes):g}~+{max(takes):g}%로 확장")
        recs[seg], seg_grid[seg] = r, {"stop": stops, "take": takes}

    # 기간 × 분류 표, 진입 비교, 참고
    out_segs = {}
    for seg in REC_SEGMENTS + REF_SEGMENTS:
        r = recs.get(seg) or {}
        combo = r.get("combo") or (recs["호재 전체"].get("combo") if seg in REF_SEGMENTS else None)
        em = r.get("entry") or (recs["호재 전체"].get("entry") if seg in REF_SEGMENTS else None)
        rows = {}
        if combo and em:
            for p in ("week", "month", "all"):
                rows[p] = grid.agg(subset(seg, p), em, combo)
        counts = {e: defaultdict(int) for e in cfg["entry_modes"]}
        for k in subset(seg, "all"):
            for e in cfg["entry_modes"]:
                counts[e][trades[k]["entries"][e]["status"]] += 1
        out_segs[seg] = {**r, "combo": combo, "entry": em, "periods": rows, "n_all": len(subset(seg, "all")),
                         "status_counts": {e: dict(v) for e, v in counts.items()}}
    changes = _changes(out_segs, prev_recs or {})
    n_total = len(trades)
    return {"segments": out_segs, "notes": notes, "changes": changes, "grid": {"modes": ms, "by_segment": seg_grid},
            "n_combos": len(cfg["stop"]) * len(cfg["take"]) * len(ms) * len(cfg["entry_modes"]),
            "n_trades": n_total, "n_good": len(seg_idx.get("호재 전체", [])),
            "sel_days": sorted(per["all_sel"]), "week_days": sorted(per["week"]), "cfg": cfg}


def _recommend(seg, subset, table, agg, stops, takes, cfg) -> dict:
    """선정 구간(최근 1주 제외)에서 진입·조합 추천. 선정 구간 표본이 없으면 최근 1주로 '참고'만."""
    sel_m, sel_a, wk = subset(seg, "month_sel"), subset(seg, "all_sel"), subset(seg, "week")
    reference_only = not sel_a
    basis_a = wk if reference_only else sel_a
    best, entries, single = None, {}, None
    for em in cfg["entry_modes"]:
        ta = table(basis_a, em)
        use_m = (not reference_only) and len(sel_m) >= cfg["min_n"]
        tm = table(sel_m, em) if use_m else None
        top = None
        for c, s in ta.items():
            if not s["n"]:
                continue
            sa = smoothed(ta, stops, takes, c)
            sc = (sa + smoothed(tm, stops, takes, c)) / 2 if use_m and tm[c]["n"] else sa
            key = (sc, s["n"])
            if top is None or key > top[0]:
                top = (key, c, sc)
            if single is None or s["mean"] > single[0]:
                single = (s["mean"], em, c, s)
        if not top:
            continue
        c = top[1]
        entries[em] = {"combo": c, "score": top[2], "basis": ta[c], "month": tm[c] if use_m else None,
                       "zone": stable_zone(ta, stops, takes, c, cfg["stable_tol"]), "use_month": use_m}
        if best is None or (top[2], ta[c]["n"]) > (best[1]["score"], best[1]["basis"]["n"]):
            best = (em, entries[em])
    if not best:
        return {"reference_only": reference_only, "entries": entries}
    em, b = best
    combo = b["combo"]
    basis_n = b["month"]["n"] if b["use_month"] else b["basis"]["n"]
    sel_mean = ((b["month"]["mean"] + b["basis"]["mean"]) / 2) if b["use_month"] else b["basis"]["mean"]
    out = {"entry": em, "combo": combo, "zone": b["zone"], "basis": b["basis"], "month": b["month"],
           "use_month": b["use_month"], "reference_only": reference_only, "entries": entries,
           "confidence": confidence(basis_n, cfg), "basis_n": basis_n, "sel_mean": sel_mean,
           "single": {"entry": single[1], "combo": single[2], "stats": single[3]} if single else None}
    if not reference_only:
        v = agg(wk, em, combo)
        out["oos"] = v
        out["overfit"] = bool(v["n"] and sel_mean - v["mean"] > cfg["oos_drop_warn"] / 100)
    return out


def _changes(segs: dict, prev: dict) -> list[str]:
    out = []
    for seg in REC_SEGMENTS:
        r, p = segs.get(seg) or {}, prev.get(seg)
        if not p or not r.get("combo") or r.get("reference_only"):
            continue
        cur = (r["entry"], *r["combo"])
        old = (p["entry"], p["stop"], p["take"], p["mode"])
        diffs = []
        if old[0] != cur[0]:
            diffs.append(f"진입 {ENTRY_SHORT.get(old[0], old[0])}→{ENTRY_SHORT[cur[0]]}")
        if old[1] != cur[1]:
            diffs.append(f"손절 {old[1]:g}→{cur[1]:g}%")
        if old[2] != cur[2]:
            diffs.append(f"익절 +{old[2]:g}→+{cur[2]:g}%")
        if old[3] != cur[3]:
            diffs.append(f"방식 {mode_label(old[3])}→{mode_label(cur[3])}")
        if (p.get("zone") or {}) != (r.get("zone") or {}) and p.get("zone") is not None:
            diffs.append(f"안정 구간 {zone_label(p['zone'])}→{zone_label(r['zone'])}")
        if diffs:
            out.append(f"{seg}: " + ", ".join(diffs))
    return out


# ------------------------------------------------------------------ 출력
def pct(v, d: int = 2, sign: bool = True) -> str:
    return "-" if v is None else (f"{v * 100:+.{d}f}%" if sign else f"{v * 100:.{d}f}%")


def _s(s: dict | None, cfg: dict) -> str:
    """기대 · 승률 · 건수 (+표본 부족)"""
    if not s or not s["n"]:
        return "표본 없음"
    warn = " ⚠️표본 부족" if s["n"] < cfg["min_n"] else ""
    return f"기대 {pct(s['mean'])} · 승률 {pct(s['win'], 0, False)} · {s['n']}건{warn}"


def render(res: dict, date: str) -> str:
    cfg = res["cfg"]
    segs = res["segments"]
    L = [f"# 손익 시나리오 {date}", ""]
    L.append(f"> ⚠️ 조합 {res['n_combos']:,}개(진입 {len(cfg['entry_modes'])} × {res['n_combos'] // len(cfg['entry_modes'])}) "
             f"대비 표본 {res['n_trades']}건(호재 {res['n_good']}건). 조합이 많을수록 최적값이 우연히 좋아 보일 수 있습니다 — "
             f"추천은 최근 1주를 뺀 구간에서 고르고 최근 1주로 따로 검증합니다. 주문은 하지 않으며 판단은 직접 하세요.")
    L.append("")
    sel = res["sel_days"]
    L.append(f"- 선정 구간: {sel[0]} ~ {sel[-1]} ({len(sel)}거래일)" if sel else
             "- 선정 구간: **없음** (기록이 최근 1주 안에만 있음 → 추천 대신 최근 1주 결과를 참고로만 표시)")
    L.append(f"- 검증 구간(최근 1주): {res['week_days'][0]} ~ {res['week_days'][-1]}")
    L.append("")
    # 1. 추천
    L += ["## 오늘의 추천 시나리오", ""]
    for seg in REC_SEGMENTS:
        r = segs[seg]
        if not r.get("combo"):
            L.append(f"- **{seg}**: 표본 없음")
            continue
        if r.get("reference_only"):
            L.append(f"- **{seg}** — 선정 구간 표본 없음, 최근 1주 결과만 참고: {ENTRY_SHORT[r['entry']]} · "
                     f"{combo_label(r['combo'])} → {_s(r['basis'], cfg)}")
            continue
        basis = (f"1개월 {_s(r['month'], cfg)} / 누적 {_s(r['basis'], cfg)}" if r["use_month"]
                 else f"누적 {_s(r['basis'], cfg)}")
        of = " ⚠️**과적합 의심**" if r.get("overfit") else ""
        L.append(f"- **{seg}**: **{ENTRY_LABEL[r['entry']]}** · **{combo_label(r['combo'])}**")
        L.append(f"  - 근거(선정 구간, 최근 1주 제외): {basis}")
        L.append(f"  - 검증(최근 1주): {_s(r.get('oos'), cfg)}{of}")
        L.append(f"  - 신뢰도 **{r['confidence']}**({r['basis_n']}건) · 안정 구간 {zone_label(r['zone'])}")
    L.append("")
    # 2. 변화
    L += ["## 전일 대비 변화", ""]
    L += [f"- {c}" for c in res["changes"]] or ["- 변화 없음(또는 전일 추천 없음)"]
    L.append("")
    # 3. 진입 시점 비교
    L += ["## 진입 시점 비교", "",
          "분류마다 진입 시점별로 안정 점수가 가장 높은 조합. 유동성 부족은 NXT 진입에만 해당(진입 봉 거래대금 "
          f"{cfg['nxt_min_amount'] / 1e4:,.0f}만 원 미만, 추천 근거에서 제외).", "",
          "| 분류 | 진입 | 조합 | 기대수익 | 승률 | 최대 손실 | 건수 | 유동성 부족 |", "|---|---|---|---|---|---|---|---|"]
    for seg in REC_SEGMENTS:
        r = segs[seg]
        for em in cfg["entry_modes"]:
            e = (r.get("entries") or {}).get(em)
            ill = r["status_counts"][em].get("illiquid", 0) if em != "krx_open" else "-"
            if not e:
                L.append(f"| {seg} | {ENTRY_SHORT[em]} | - | - | - | - | 0 | {ill} |")
                continue
            s = e["basis"]
            warn = " ⚠️" if s["n"] < cfg["min_n"] else ""
            L.append(f"| {seg} | {ENTRY_SHORT[em]} | {combo_label(e['combo'])} | {pct(s['mean'])} | "
                     f"{pct(s['win'], 0, False)} | {pct(s['max_loss'])} | {s['n']}{warn} | {ill} |")
    L.append("")
    # 4. 기간 × 분류
    L += ["## 기간 × 분류 (추천 조합 기준)", "",
          "| 분류 | 기간 | 기대수익 | 승률 | 최대 손실 | 건수 |", "|---|---|---|---|---|---|"]
    for seg in REC_SEGMENTS + REF_SEGMENTS:
        r = segs[seg]
        for p, pl in (("week", "1주(검증)"), ("month", "1개월"), ("all", "누적")):
            s = (r.get("periods") or {}).get(p)
            if not s or not s["n"]:
                L.append(f"| {seg} | {pl} | - | - | - | 0 |")
                continue
            warn = " ⚠️표본 부족" if s["n"] < cfg["min_n"] else ""
            L.append(f"| {seg} | {pl} | {pct(s['mean'])} | {pct(s['win'], 0, False)} | {pct(s['max_loss'])} | {s['n']}{warn} |")
    L.append("")
    # 5. 단일 최적값 vs 추천
    L += ["## 단일 최적값 vs 추천(안정)", ""]
    for seg in REC_SEGMENTS:
        r = segs[seg]
        if not r.get("single"):
            continue
        sg = r["single"]
        same = sg["entry"] == r["entry"] and sg["combo"] == r["combo"]
        L.append(f"- {seg}: 단일 최적 {ENTRY_SHORT[sg['entry']]} · {combo_label(sg['combo'])} {pct(sg['stats']['mean'])}"
                 f"({sg['stats']['n']}건)" + (" — 추천과 같음" if same else " — 추천과 다름(이웃 칸 평균이 낮아 안정 조합을 추천)"))
    L += [f"- 범위 확장: {n}" for n in res["notes"]] or ["- 범위 확장 없음"]
    L.append("")
    # 6. 참고
    L += ["## 참고", ""]
    for seg in REF_SEGMENTS:
        s = (segs[seg].get("periods") or {}).get("all")
        L.append(f"- {seg}(호재 전체 추천 조합 적용, 누적): {_s(s, cfg)}")
    tot = defaultdict(lambda: defaultdict(int))
    for seg in ("호재 전체", "악재", "비교 기준"):
        for em, c in segs[seg]["status_counts"].items():
            for k, v in c.items():
                tot[em][k] += v
    for em in cfg["entry_modes"]:
        c = tot[em]
        extra = f" · 유동성 부족 {c.get('illiquid', 0)}" if em != "krx_open" else ""
        L.append(f"- {ENTRY_SHORT[em]}: 시뮬레이션 {c.get('ok', 0)} · 상한가 제외 {c.get('limit', 0)}{extra} · 해당 없음 {c.get('na', 0)}")
    hr = segs["호재 전체"]
    if hr.get("periods", {}).get("all"):
        L.append(f"- 모호 봉(한 봉에서 손절·익절 동시 도달 → 손절 처리, 호재 전체 추천 조합 누적): {hr['periods']['all']['amb']}건")
    L.append("")
    L += ["<sub>가정: 비용 왕복 {c}% + 손절·트레일 체결 슬리피지 {s}% · 진입가 = 진입 봉 시가 × (1 + NXT {n}% / KRX {k}%) · "
          "한 봉 동시 도달 시 손절 우선 · 손절 뒤 재진입 없음 · 미체결은 15:30 KRX 종가 청산 · 주문 없음</sub>".format(
              c=cfg["cost_pct"], s=cfg["stop_slippage_pct"], n=cfg["nxt_entry_slippage_pct"], k=cfg["krx_entry_slippage_pct"])]
    return "\n".join(L) + "\n"


def telegram_text(res: dict, date: str) -> str:
    cfg = res["cfg"]
    L = [f"📈 손익 시나리오 {date[5:]} (표본 {res['n_trades']}건 · 조합 {res['n_combos']:,}개)"]
    for seg in REC_SEGMENTS:
        r = res["segments"][seg]
        if not r.get("combo"):
            continue
        c = r["combo"]
        head = f"• {seg}: {ENTRY_SHORT[r['entry']]} · 손절 {c[0]:g}% · 익절 +{c[1]:g}% {mode_label(c[2])}"
        if r.get("reference_only"):
            L.append(head + f" (참고, 1주 {pct(r['basis']['mean'], 1)} {r['basis']['n']}건)")
        else:
            of = " ⚠️과적합" if r.get("overfit") else ""
            oos = r.get("oos") or {}
            L.append(head + f" → 기대 {pct(r['sel_mean'], 1)} · 검증 {pct(oos.get('mean'), 1)}({oos.get('n', 0)}건){of}"
                     f" · 신뢰도 {r['confidence']}")
    if not res["sel_days"]:
        L.append("※ 선정 구간 표본 없음 — 최근 1주 결과만 참고")
    if res["changes"]:
        L += ["", "변화:"] + [f"• {c}" for c in res["changes"][:5]]
    return "\n".join(L)


def to_json(res: dict) -> dict:
    """API·파일 저장용(튜플 → 목록, cfg는 주요 값만)."""
    def conv(o):
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(x) for x in o]
        return o
    out = conv({k: v for k, v in res.items() if k != "cfg"})
    out["assumptions"] = {k: res["cfg"][k] for k in ("cost_pct", "stop_slippage_pct", "nxt_entry_slippage_pct",
                                                      "krx_entry_slippage_pct", "nxt_min_amount", "min_n")}
    return out


def save_recs(conn, date: str, res: dict, stamp: str) -> None:
    conn.execute("DELETE FROM scenario_recs WHERE date=?", (date,))
    for seg in REC_SEGMENTS:
        r = res["segments"][seg]
        if not r.get("combo") or r.get("reference_only"):
            continue
        s, t, m = r["combo"]
        conn.execute("INSERT INTO scenario_recs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (date, seg, r["entry"], m, s, t, json.dumps(r["zone"]),
                      json.dumps({"sel_mean": r["sel_mean"], "basis_n": r["basis_n"], "confidence": r["confidence"],
                                  "oos": r.get("oos"), "overfit": r.get("overfit")}, ensure_ascii=False), stamp))
    conn.commit()


def load_prev_recs(conn, date: str) -> dict:
    d = conn.execute("SELECT MAX(date) FROM scenario_recs WHERE date<?", (date,)).fetchone()[0]
    if not d:
        return {}
    return {r["segment"]: {"entry": r["entry"], "mode": r["mode"], "stop": r["stop"], "take": r["take"],
                           "zone": json.loads(r["zone"] or "null")}
            for r in conn.execute("SELECT * FROM scenario_recs WHERE date=?", (d,))}


# ------------------------------------------------------------------ 개별 매매(trades) — API·재계산용
ENTRY_TYPE = {"nxt_early": "NXT 08:00", "nxt_late": "NXT 08:40", "krx_open": "KRX 09:00"}


def first_hit(bars: list[list] | None) -> tuple[str | None, str | None]:
    """경로의 최고가·최저가 중 먼저 나온 쪽 → ('high'|'low'|None, None이면 이유)."""
    if not bars:
        return None, "분봉 없음"
    hi = max(x[2] for x in bars)
    lo = min(x[3] for x in bars)
    ih = next(i for i, x in enumerate(bars) if x[2] >= hi)
    il = next(i for i, x in enumerate(bars) if x[3] <= lo)
    if ih == il:
        return None, f"같은 1분봉({bars[ih][0][:2]}:{bars[ih][0][2:]})에서 고가·저가가 모두 나와 순서를 알 수 없음"
    return ("high" if ih < il else "low"), None


def _r(v, d=2):
    return None if v is None else round(v, d)


def trade_rows(trades: list[dict], res: dict, cfg: dict) -> list[dict]:
    """trades × 진입 → 개별 매매 행. 청산은 '호재 전체' 추천 조합(없으면 기본 손절 -3%·익절 +5%·전량)으로 계산."""
    rec = res["segments"].get("호재 전체") or {}
    combo = tuple(rec["combo"]) if rec.get("combo") else (-3, 5, "full")
    out = []
    for t in trades:
        segs = segments_of(t)
        for em, e in t["entries"].items():
            if e["status"] == "na":
                continue
            bars = e.get("bars") or []
            o = e.get("raw_open")
            hi = max((x[2] for x in bars), default=None)
            lo = min((x[3] for x in bars), default=None)
            cl = e.get("close")
            fh, why = first_hit(bars)
            row = {"date": t["date"], "code": t["code"], "name": t.get("name"), "score": t.get("final"),
                   "basis": [x for x in ("공시", "뉴스", "정책") if x in str(t.get("basis") or "")],
                   "sentiment": t.get("sentiment"), "group": segs[0] if segs[0] in ("악재", "비교 기준") else "호재",
                   "segments": segs, "entry": em, "entry_type": ENTRY_TYPE[em], "status": e["status"],
                   "entry_price": e.get("price"), "prev_close": t.get("prev_close"),
                   "open": o, "high": hi, "low": lo, "close": cl,
                   "high_pct": _r((hi / o - 1) * 100) if o and hi else None,
                   "low_pct": _r((lo / o - 1) * 100) if o and lo else None,
                   "close_pct": _r((cl / o - 1) * 100) if o and cl else None,
                   "entry_amount": round(e["amount"]) if e.get("amount") is not None else None,
                   "first_hit": fh, "first_hit_reason": why, "bars_n": len(bars),
                   "exit": None}
            if e["status"] in ("ok", "illiquid") and bars:
                r, amb, reason = Path(e["price"], bars, cfg, cl).run(*combo)
                row["exit"] = {"combo": list(combo), "label": combo_label(combo), "reason": reason,
                               "ret_pct": _r(r * 100, 3), "ambiguous": amb}
            out.append(row)
    return out


def trades_from_rows(rows: list[dict]) -> list[dict]:
    """bars가 붙은 개별 매매 행(API ?bars=1) → analyze()에 넣을 trades. 같은 입력이면 같은 결과가 나온다."""
    by: dict[tuple, dict] = {}
    for r in rows:
        k = (r["date"], r["code"])
        t = by.setdefault(k, {"date": r["date"], "code": r["code"], "name": r.get("name"),
                              "grp": "control" if r.get("group") == "비교 기준" else "scored",
                              "sentiment": r.get("sentiment"), "final": r.get("score"),
                              "basis": ",".join(r.get("basis") or []), "prev_close": r.get("prev_close"),
                              "entries": {}})
        t["entries"][r["entry"]] = {"status": r["status"], "raw_open": r["open"], "price": r["entry_price"],
                                    "bars": r.get("bars") or [], "amount": r.get("entry_amount"), "close": r["close"]}
    for t in by.values():
        for em in ENTRY_TYPE:
            t["entries"].setdefault(em, {"status": "na"})
    return list(by.values())
