"""테마 묶음별 하루 지표 · 종목 서열(대장/2등주/후발주) · 진행 단계(태동/확산/과열/소멸).

입력은 전부 미리 받아 둔 값(스냅샷·지수·1분봉 패턴·전일 기록)이라 여기서는 네트워크를 쓰지 않는다 → 단위 테스트 가능.
순위 기준은 중앙값 초과(지수 대비)다. 평균·거래대금 가중 평균도 함께 저장해 주간 리뷰에서 비교한다.
"""
from __future__ import annotations

import json
import statistics
from typing import Iterable

from ..config import ROOT

LIFECYCLE_PATH = ROOT / "config" / "theme_lifecycle.json"
STAGES = ("태동", "확산", "과열", "소멸")


def load_lifecycle() -> dict:
    return json.loads(LIFECYCLE_PATH.read_text(encoding="utf-8"))


def _hit_key(m: dict) -> tuple:
    """서열 정렬 키: 상한가 도달(이른 시각 먼저) → 등락률 → 거래대금 → 연속 강세 일수."""
    hit = bool(m.get("limit_hit"))
    return (0 if hit else 1, m.get("first_hit") or "99:99", -(m.get("change_pct") or 0),
            -(m.get("trade_value") or 0), -(m.get("streak") or 0), m["code"])


def rank_members(members: list[dict]) -> list[dict]:
    """1위 대장 · 2~3위 2등주 · 나머지 상승 종목 후발주. 오르지 않은 종목은 라벨 없음."""
    ranked = sorted(members, key=_hit_key)
    for i, m in enumerate(ranked, 1):
        m["pos"] = i
        up = (m.get("change_pct") or 0) > 0
        m["label"] = None if not up else "대장" if i == 1 else "2등주" if i <= 3 else "후발주"
    return ranked


def wavg(vals: Iterable[tuple[float, float]]) -> float | None:
    vals = [(v, w) for v, w in vals if v is not None and w]
    tw = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / tw if tw else None


def group_metrics(members: list[dict], cfg: dict) -> dict:
    """members: [{code, change_pct, excess, trade_value, limit_hit}] (시세 있는 종목만)."""
    ch = [m["change_pct"] for m in members]
    ex = [m["excess"] for m in members]
    pos_gain = sorted((c for c in ch if c > 0), reverse=True)
    return {
        "n": len(members),
        "avg_pct": statistics.fmean(ch), "median_pct": statistics.median(ch),
        "wavg_pct": wavg((m["change_pct"], m.get("trade_value")) for m in members),
        "avg_excess": statistics.fmean(ex), "median_excess": statistics.median(ex),
        "wavg_excess": wavg((m["excess"], m.get("trade_value")) for m in members),
        "rise_ratio": sum(c > 0 for c in ch) / len(ch),
        "up5": sum(c >= cfg["up5_pct"] for c in ch),
        "limit_ups": sum(bool(m.get("limit_hit")) and bool(m.get("closed_at_limit")) for m in members),
        "trade_value": sum(m.get("trade_value") or 0 for m in members),
        "concentration": (sum(pos_gain[:2]) / sum(pos_gain)) if len(pos_gain) >= 1 and sum(pos_gain) > 0 else None,
    }


def stage_of(today: dict, prev: dict | None, cfg: dict, leader: dict | None = None) -> tuple[int, int, float, str | None]:
    """→ (strong, streak, cum_excess, stage). prev = 전일 theme_daily 행(없으면 None)."""
    strong = int(today["median_excess"] >= cfg["strong_median_excess"] and today["rise_ratio"] >= cfg["strong_rise_ratio"])
    prev_streak = (prev or {}).get("streak") or 0
    streak = prev_streak + 1 if strong else 0
    cum = ((prev or {}).get("cum_excess") or 0) + today["median_excess"] if strong and prev_streak else \
        today["median_excess"] if strong else 0.0
    stage = None
    if strong:
        if streak == 1:
            stage = "태동"
        elif streak >= cfg["overheat_days"]:
            stage = "과열"
        else:
            widen = today["rise_ratio"] >= (prev or {}).get("rise_ratio", 0) and today["up5"] >= (prev or {}).get("up5", 0)
            conc = today.get("concentration") or 0
            stage = "확산" if widen or conc < cfg["overheat_concentration"] else "과열"
            if stage == "확산" and streak > cfg["spread_max_days"]:
                stage = "과열"
    elif prev_streak:
        released = bool(leader and leader.get("limit_hit") and not leader.get("closed_at_limit"))
        if today["median_excess"] < 0 or released:
            stage = "소멸"
    return strong, streak, round(cum, 3), stage


def stock_streaks(history: dict[str, list[float]]) -> dict[str, int]:
    """{code: [오늘, 어제, …] 등락률} → 오늘부터 이어진 상승 일수."""
    out = {}
    for code, seq in history.items():
        n = 0
        for c in seq:
            if c is None or c <= 0:
                break
            n += 1
        out[code] = n
    return out


def compute(day: str, groups: list[dict], snap: dict[str, dict], index_pct: dict[str, float],
            markets: dict[str, str], patterns: dict[str, dict], prev_rows: dict[str, dict],
            tv_avg: dict[str, float], streaks: dict[str, int], cfg: dict | None = None,
            min_members: int = 5) -> tuple[list[dict], list[dict]]:
    """→ (theme_daily 행 목록, theme_members_daily 행 목록). 순위(rank)는 5종목 이상 묶음의 중앙값 초과 순."""
    cfg = cfg or load_lifecycle()
    t_rows, m_rows = [], []
    for g in groups:
        ms = []
        for code in g["members"]:
            s = snap.get(code)
            if not s or s.get("change_pct") is None:
                continue
            mk = "KOSDAQ" if str(markets.get(code, "")).startswith("KOSDAQ") else "KOSPI"
            p = patterns.get(code) or {}
            ms.append({"code": code, "change_pct": s["change_pct"], "trade_value": s.get("trade_value"),
                       "excess": s["change_pct"] - (index_pct.get(mk) or 0.0),
                       "limit_hit": p.get("hit") if p else int(s["change_pct"] >= 29.5),
                       "first_hit": p.get("first_hit"), "closed_at_limit": p.get("closed_at_limit")
                       if p else int(s["change_pct"] >= 29.5), "streak": streaks.get(code, 0)})
        if not ms:
            continue
        met = group_metrics(ms, cfg)
        ranked = rank_members(ms)
        leader = ranked[0] if ranked[0]["label"] == "대장" else None
        prev = prev_rows.get(g["group_id"])
        strong, streak, cum, stage = stage_of(met, prev, cfg, leader)
        small = int(len(g["members"]) < min_members)
        if small:
            strong, streak, cum, stage = 0, 0, 0.0, None
        avg_tv = tv_avg.get(g["group_id"])
        row = {"dt": day, "group_id": g["group_id"], "group_name": g["name"], **{k: _r(v) for k, v in met.items()},
               "tv_ratio20": _r(met["trade_value"] / avg_tv) if avg_tv else None,
               "strong": strong, "streak": streak, "cum_excess": cum, "stage": stage,
               "leader_code": leader["code"] if leader else None,
               # 대장 교체는 전일에도 강세였던 묶음만(약한 날 대장은 의미가 적어 잡음)
               "leader_changed": int(bool(prev and (prev.get("streak") or 0) > 0 and prev.get("leader_code") and leader
                                          and prev["leader_code"] != leader["code"])),
               "rank": None, "small": small}
        t_rows.append(row)
        for m in ranked:
            m_rows.append({"dt": day, "group_id": g["group_id"], "code": m["code"], "change_pct": _r(m["change_pct"]),
                           "excess": _r(m["excess"]), "trade_value": m.get("trade_value"),
                           "limit_hit": m.get("limit_hit"), "first_hit": m.get("first_hit"),
                           "closed_at_limit": m.get("closed_at_limit"), "streak": m.get("streak"),
                           "pos": m["pos"], "label": m["label"], "after_leader_pct": None})
    big = sorted((r for r in t_rows if not r["small"]), key=lambda r: -(r["median_excess"] or -99))
    for i, r in enumerate(big, 1):
        r["rank"] = i
    return t_rows, m_rows


def after_leader(leader_hit_time: str, bars: list[dict], date: str) -> float | None:
    """대장 상한가 도달 시각(HH:MM) 이후 이 종목의 추가 상승 %: 정규장 종가 / 도달 시각 가격 - 1."""
    ymd = date.replace("-", "")
    hm = leader_hit_time.replace(":", "")
    day = sorted((b for b in bars if str(b.get("cntr_tm", "")).startswith(ymd) and b["cntr_tm"][8:12] <= "1530"),
                 key=lambda b: b["cntr_tm"])
    at = [b for b in day if b["cntr_tm"][8:12] <= hm]
    if not at or not day:
        return None
    p0 = abs(float(str(at[-1]["cur_prc"]).replace("+", "").replace(",", "")))
    p1 = abs(float(str(day[-1]["cur_prc"]).replace("+", "").replace(",", "")))
    return round((p1 / p0 - 1) * 100, 2) if p0 else None


def _r(v, nd: int = 3):
    return round(v, nd) if isinstance(v, float) else v
