"""주간 리뷰: 채점 ↔ 실제 주가 반응 비교 + 가중치 재귀 조절.

목표 지표(target, config/weights.json): 기본 oc_excess = D0 시가→종가 수익률 - 같은 시장 지수의 시가→종가.
  (아침에 리포트를 보고 시가 근처에서 들어간다고 가정. 시장 전체 움직임은 뺀다.)

가중치 조절 규칙 (매주 이전 가중치에서 출발 → '재귀')
  1. 누적 표본: 호재로 채점되고 target이 있는 항목. 전체 MIN_TOTAL건 미만이면 조절하지 않는다.
  2. 항목별 순위상관(Spearman) ρ = corr(항목 점수, target). 그 항목 점수가 있는 표본이 MIN_FACTOR건 미만이면 그 항목은 고정.
  3. 제안 비중 ∝ max(ρ, 0) + FLOOR  (조절 대상 항목들끼리, 현재 그 항목들이 차지하는 비중 안에서 나눔)
  4. 새 가중치 = 이전 + clip(ALPHA × (제안 - 이전), ±MAX_STEP)  → 하한 MIN_W·상한 MAX_W → 합 1로 정규화
  → 한 주에 크게 흔들리지 않고, 데이터가 쌓일수록 근거 있는 방향으로 천천히 이동.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics

from .weights import FACTORS, load, save

MIN_TOTAL = 40
MIN_FACTOR = 20
FLOOR = 0.05
ALPHA = 0.3
MAX_STEP = 0.05
MIN_W, MAX_W = 0.05, 0.40
LABELS = {"scale": "재료 규모", "exclusivity": "최초/독점", "novelty": "신규성", "policy": "정책 수혜", "diffusion": "확산도",
          "quality": "재료의 질", "preprice": "선반영(덜 오름)"}


def pct(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v * 100:+.{digits}f}%"


def _rows(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    q = ("SELECT p.*, s.stock_name, s.factors, s.material, s.weights_version FROM price_reactions p "
         "LEFT JOIN scores s ON s.date=p.date AND s.stock_code=p.stock_code WHERE 1=1")
    args = []
    if start:
        q += " AND p.date >= ?"
        args.append(start)
    if end:
        q += " AND p.date <= ?"
        args.append(end)
    out = []
    for r in conn.execute(q + " ORDER BY p.date", args):
        d = dict(r)
        d["factors"] = json.loads(d["factors"]) if d.get("factors") else {}
        out.append(d)
    return out


def summarize(rows: list[dict], target: str) -> dict:
    def stat(group: list[dict]) -> dict:
        ys = [r[target] for r in group if r.get(target) is not None]
        return {"n": len(ys), "mean": statistics.fmean(ys) if ys else None,
                "median": statistics.median(ys) if ys else None,
                "win": (sum(y > 0 for y in ys) / len(ys)) if ys else None,
                "gap": statistics.fmean([r["gap_pct"] for r in group if r.get("gap_pct") is not None] or [0]) if group else None,
                "oh": statistics.fmean([r["oh_pct"] for r in group if r.get("oh_pct") is not None] or [0]) if group else None,
                "r5": statistics.fmean([r["ret_5d"] for r in group if r.get("ret_5d") is not None])
                if any(r.get("ret_5d") is not None for r in group) else None}
    good = [r for r in rows if r["grp"] == "scored" and r["sentiment"] == "호재"]
    return {
        "all_good": stat(good),
        "by_score": {s: stat([r for r in good if r["final"] == s]) for s in (5, 4, 3, 2, 1)},
        "by_basis": {b: stat([r for r in good if b in (r.get("basis") or "")]) for b in ("공시", "뉴스", "정책")},
        "bad": stat([r for r in rows if r["grp"] == "scored" and r["sentiment"] == "악재"]),
        "control": stat([r for r in rows if r["grp"] == "control"]),
    }


def factor_corr(rows: list[dict], target: str) -> dict[str, dict]:
    good = [r for r in rows if r["grp"] == "scored" and r["sentiment"] == "호재" and r.get(target) is not None]
    out = {}
    for k in FACTORS:
        pairs = [((r["factors"].get(k) or {}).get("score"), r[target]) for r in good]
        pairs = [(x, y) for x, y in pairs if isinstance(x, (int, float))]
        rho = None
        if len(pairs) >= 3:
            try:
                rho = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs], method="ranked")
            except statistics.StatisticsError:      # 점수가 모두 같으면 상관 계산 불가
                rho = None
        out[k] = {"n": len(pairs), "rho": rho}
    return out


def propose(current: dict[str, float], corr: dict[str, dict], total_n: int) -> tuple[dict[str, float], list[str]]:
    notes = []
    if total_n < MIN_TOTAL:
        notes.append(f"누적 표본 {total_n}건 < {MIN_TOTAL}건 — 가중치 유지(데이터 축적 중)")
        return dict(current), notes
    eligible = [k for k in FACTORS if corr[k]["n"] >= MIN_FACTOR and corr[k]["rho"] is not None]
    fixed = [k for k in FACTORS if k not in eligible]
    for k in fixed:
        notes.append(f"{LABELS[k]}: 표본 {corr[k]['n']}건 — 고정")
    if not eligible:
        notes.append("조절 가능한 항목 없음 — 유지")
        return dict(current), notes
    mass = sum(current[k] for k in eligible)
    raw = {k: max(corr[k]["rho"], 0) + FLOOR for k in eligible}
    sugg = {k: mass * raw[k] / sum(raw.values()) for k in eligible}
    new = dict(current)
    for k in eligible:
        step = max(-MAX_STEP, min(MAX_STEP, ALPHA * (sugg[k] - current[k])))
        new[k] = min(MAX_W, max(MIN_W, current[k] + step))
    # 고정 항목은 그대로 두고, 조절 항목끼리만 원래 차지하던 비중(mass)으로 다시 맞춘다
    # 다시 맞춘 뒤에도 상·하한을 지키도록: 범위를 벗어난 항목은 경계값에 고정하고 나머지로 재분배(몇 번 반복)
    free = list(eligible)
    for _ in range(6):
        pinned = sum(new[k] for k in eligible if k not in free)
        fsum = sum(new[k] for k in free)
        if not free or fsum <= 0:
            break
        for k in free:
            new[k] = new[k] * (mass - pinned) / fsum
        out_of_range = [k for k in free if not MIN_W <= new[k] <= MAX_W]
        if not out_of_range:
            break
        for k in out_of_range:
            new[k] = min(MAX_W, max(MIN_W, new[k]))
            free.remove(k)
    new = {k: round(v, 3) for k, v in new.items()}
    drift = round(1 - sum(new.values()), 3)                 # 반올림 오차는 조절 항목 중 가장 큰 것에
    big = max(eligible, key=lambda k: new[k])
    new[big] = round(new[big] + drift, 3)
    for k in eligible:
        if abs(new[k] - current[k]) >= 0.005:
            notes.append(f"{LABELS[k]}: {current[k] * 100:.1f}% → {new[k] * 100:.1f}% (ρ={corr[k]['rho']:+.2f}, n={corr[k]['n']})")
    return new, notes


def apply(conn, new: dict[str, float], prev: dict[str, float], target: str, n: int, notes: list[str]) -> int | None:
    """바뀐 게 있으면 weights.json 버전 올리고 기록. 새 버전 번호(없으면 None)."""
    if all(abs(new[k] - prev[k]) < 0.0005 for k in FACTORS):
        return None
    cfg = load()
    cfg.update(version=int(cfg["version"]) + 1, updated=f"{dt.date.today():%Y-%m-%d}", weights=new,
               note="주간 리뷰 자동 조절: " + " / ".join(notes)[:300])
    save(cfg)
    conn.execute("INSERT INTO weight_history (version, applied_at, target, sample_n, weights, prev_weights, note) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (cfg["version"], f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", target, n, json.dumps(new),
                  json.dumps(prev), cfg["note"]))
    conn.commit()
    return cfg["version"]


def render(week_start: str, week_end: str, week: dict, cum: dict, corr: dict, target: str,
           prev: dict, new: dict, new_version: int | None, notes: list[str], top: list[dict], bottom: list[dict],
           ver: int) -> str:
    T = {"oc_excess": "시가→종가 초과수익(지수 대비)", "close_excess": "전일종가→종가 초과수익",
         "oc_pct": "시가→종가", "ret_5d": "5일 수익"}.get(target, target)
    L = [f"# 주간 리뷰 {week_start} ~ {week_end}", "",
         f"- 목표 지표: **{T}** (`{target}`)",
         f"- 이번 주 호재 {week['all_good']['n']}건 · 누적 {cum['all_good']['n']}건",
         f"- 가중치: v{ver}" + (f" → **v{new_version}로 조절**" if new_version else " (변경 없음)"), ""]

    def table(title: str, groups: dict, label) -> None:
        L.append(f"### {title}")
        L.append("")
        L.append("| 구분 | 건수 | 평균 | 중앙값 | 승률 | 평균 갭 | 장중 고가 | 5일 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for key, s in groups.items():
            if not s["n"]:
                continue
            win = "-" if s["win"] is None else f"{s['win'] * 100:.0f}%"
            L.append(f"| {label(key)} | {s['n']} | {pct(s['mean'])} | {pct(s['median'])} | {win} | {pct(s['gap'])} | "
                     f"{pct(s['oh'])} | {pct(s['r5'])} |")
        L.append("")

    for name, data in (("이번 주", week), ("누적", cum)):
        L.append(f"## {name}")
        L.append("")
        table("점수별 (호재)", {**{f"{k}점": v for k, v in data["by_score"].items()}, "호재 전체": data["all_good"],
                             "악재": data["bad"], "비교 기준(채점 안 한 후보)": data["control"]}, lambda k: k)
        table("근거별 (호재)", data["by_basis"], lambda k: f"[{k}]")

    L.append("## 이번 주 잘 맞은 / 안 맞은 종목")
    L.append("")
    L.append("| 구분 | 날짜 | 종목 | 점수 | 근거 | 갭 | 목표 지표 | 재료 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for tag, group in (("▲", top), ("▼", bottom)):
        for r in group:
            L.append(f"| {tag} | {r['date'][5:]} | {r.get('stock_name') or r['stock_code']} | {r['final']} | {r.get('basis') or '-'} | "
                     f"{pct(r.get('gap_pct'), 1)} | {pct(r.get(target), 1)} | {(r.get('material') or '')[:40]} |")
    L.append("")

    L.append("## 항목별 상관 (누적, 호재)")
    L.append("")
    L.append("| 항목 | 표본 | 순위상관 ρ | 이전 가중치 | 새 가중치 |")
    L.append("|---|---|---|---|---|")
    for k in FACTORS:
        c = corr[k]
        rho = "-" if c["rho"] is None else f"{c['rho']:+.2f}"
        L.append(f"| {LABELS[k]} | {c['n']} | {rho} | {prev[k] * 100:.1f}% | {new[k] * 100:.1f}% |")
    L.append("")
    L.append("**조절 메모**")
    for n in notes or ["변경 없음"]:
        L.append(f"- {n}")
    L.append("")
    L.append(f"<sub>규칙: 누적 {MIN_TOTAL}건 이상일 때만 조절, 항목별 {MIN_FACTOR}건 이상, 한 번에 ±{MAX_STEP * 100:.0f}%p 이내, "
             f"범위 {MIN_W * 100:.0f}~{MAX_W * 100:.0f}%. 되돌리기: config/weights.json 또는 DB weight_history.</sub>")
    return "\n".join(L) + "\n"
