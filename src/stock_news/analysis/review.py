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
ZERO_WEEKS = 3          # 연속 반대 상관이면 가중치 0
RESTORE_WEEKS = 2       # 0인 항목이 연속 양의 상관이면 복귀
LABELS = {"scale": "재료 규모", "exclusivity": "최초/독점", "novelty": "신규성", "policy": "정책 수혜", "diffusion": "확산도",
          "quality": "재료의 질", "preprice": "선반영(덜 오름)",
          "theme_cluster": "테마 결집도", "leader": "테마 대장"}


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
    # 장 전 추가 채점(preopen_scores)도 같은 날 반응으로 함께 본다 — 아침 scores가 없을 때 그쪽 값
    q = q.replace("SELECT p.*, s.stock_name, s.factors, s.material, s.weights_version FROM",
                  "SELECT p.*, COALESCE(s.stock_name, ps.stock_name) AS stock_name, COALESCE(s.factors, ps.factors) AS factors, "
                  "COALESCE(s.material, ps.material) AS material, COALESCE(s.weights_version, ps.weights_version) AS weights_version, "
                  "COALESCE(s.sources, ps.sources) AS score_sources, (s.stock_code IS NULL AND ps.stock_code IS NOT NULL) AS preopen FROM")
    q = q.replace("LEFT JOIN scores s ON s.date=p.date AND s.stock_code=p.stock_code",
                  "LEFT JOIN scores s ON s.date=p.date AND s.stock_code=p.stock_code "
                  "LEFT JOIN preopen_scores ps ON ps.date=p.date AND ps.stock_code=p.stock_code")
    out = []
    for r in conn.execute(q + " ORDER BY p.date", args):
        d = dict(r)
        d["factors"] = json.loads(d["factors"]) if d.get("factors") else {}
        d["ministries"] = _ministries(conn, d)
        d["policy_session"] = _session(conn, d)
        d["disclosure_session"] = _disc_session(conn, d)
        if d.get("open_limit"):            # 시가가 상한가 → 시가에 살 수 없음: 시가 기준 목표에서 제외(0으로 세면 강한 재료를 감점)
            d["oc_excess_open_limit"], d["oc_excess"], d["oc_pct"] = d.get("oc_excess"), None, None
        out.append(d)
    return out


def _ministries(conn, d: dict) -> list[str]:
    """정책 근거 종목의 부처명(정책브리핑 표기는 수집 부처 표시명으로 통일). 정책 근거가 아니면 []."""
    if "정책" not in (d.get("basis") or ""):
        return []
    from ..report import policy_ministries
    return policy_ministries(conn, {"sources": json.loads(d.get("score_sources") or "[]"), "id": "P-"})


def _session(conn, d: dict) -> str | None:
    if "정책" not in (d.get("basis") or ""):
        return None
    from ..report import policy_session
    return policy_session(conn, {"sources": json.loads(d.get("score_sources") or "[]"), "id": "P-"})


def _disc_session(conn, d: dict) -> str | None:
    if "공시" not in (d.get("basis") or ""):
        return None
    from ..report import disclosure_session
    return disclosure_session(conn, {"sources": json.loads(d.get("score_sources") or "[]")})


SESSIONS3 = ("장 마감 후", "장 전", "장중")


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
        "open_limit": sum(bool(r.get("open_limit")) for r in good),
        "all_good": stat(good),
        "by_score": {s: stat([r for r in good if r["final"] == s]) for s in (5, 4, 3, 2, 1)},
        "by_basis": {b: stat([r for r in good if b in (r.get("basis") or "")]) for b in ("공시", "뉴스", "정책")},
        # 정책 게시 구간별: 장 마감 후(15:30~다음 07:00) / 장중(07:00~15:30 — 이미 반영됐을 수 있음)
        # 게시·접수 구간별: 장 마감 후(15:30~07:00) / 장 전(07:00~08:00) / 장중(08:00~15:30) — 공시·정책 각각
        "by_policy_session": {k: stat([r for r in good if r.get("policy_session") == k]) for k in SESSIONS3},
        "by_disclosure_session": {k: stat([r for r in good if r.get("disclosure_session") == k]) for k in SESSIONS3},
        # 부처별(정책 근거 호재): 한 종목이 여러 부처 정책에 걸리면 각 부처에 한 번씩 센다
        "by_ministry": dict(sorted(((m, stat([r for r in good if m in r.get("ministries", [])]))
                                    for m in {m for r in good for m in r.get("ministries", [])}),
                                   key=lambda kv: -kv[1]["n"])),
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


def zero_rules(corr: dict[str, dict], history: dict[str, list[float]], zeroed: set[str]) -> tuple[set[str], list[str]]:
    """가중치 0 규칙 → (이번 주 0으로 둘 항목, 메모).
    표본이 충분한(MIN_FACTOR) 항목이 ZERO_WEEKS주 연속 ρ<0이면 0(점수는 계속 기록, 리포트엔 '경고 전용').
    0인 항목이 RESTORE_WEEKS주 연속 ρ>0이면 하한(MIN_W)으로 복귀."""
    notes, out = [], set(zeroed)
    for k in FACTORS:
        c = corr.get(k) or {}
        if c.get("rho") is None or c.get("n", 0) < MIN_FACTOR:
            continue
        seq = (history.get(k) or [])[-(ZERO_WEEKS - 1):] + [c["rho"]]
        if k not in zeroed and len(seq) >= ZERO_WEEKS and all(x < 0 for x in seq):
            out.add(k)
            notes.append(f"{LABELS[k]}: {ZERO_WEEKS}주 연속 반대 상관(ρ {', '.join(f'{x:+.2f}' for x in seq)}) → 가중치 0, 경고 전용")
        back = (history.get(k) or [])[-(RESTORE_WEEKS - 1):] + [c["rho"]]
        if k in zeroed and len(back) >= RESTORE_WEEKS and all(x > 0 for x in back):
            out.discard(k)
            notes.append(f"{LABELS[k]}: {RESTORE_WEEKS}주 연속 양의 상관 → 하한 {MIN_W * 100:.0f}%로 복귀")
    return out, notes


def propose(current: dict[str, float], corr: dict[str, dict], total_n: int,
            history: dict[str, list[float]] | None = None, zeroed: set[str] | None = None
            ) -> tuple[dict[str, float], list[str], set[str]]:
    """→ (새 가중치, 메모, 0으로 둔 항목)."""
    notes = []
    zeroed = set(zeroed or ())
    current = {k: current.get(k, 0.0) for k in FACTORS}
    if total_n < MIN_TOTAL:
        notes.append(f"누적 표본 {total_n}건 < {MIN_TOTAL}건 — 가중치 유지(데이터 축적 중)")
        return dict(current), notes, zeroed
    new_zero, znotes = zero_rules(corr, history or {}, zeroed)
    notes += znotes
    restored = zeroed - new_zero
    for k in restored:
        current[k] = MIN_W                       # 복귀 항목은 하한에서 다시 시작
    eligible = [k for k in FACTORS if corr[k]["n"] >= MIN_FACTOR and corr[k]["rho"] is not None and k not in new_zero]
    fixed = [k for k in FACTORS if k not in eligible and k not in new_zero]
    for k in fixed:
        notes.append(f"{LABELS[k]}: 표본 {corr[k]['n']}건 — 고정")
    if not eligible:
        notes.append("조절 가능한 항목 없음 — 유지")
        return dict(current), notes, new_zero
    # 0으로 내린 항목의 비중은 조절 항목들이 나눠 갖는다. 고정 항목은 그대로
    mass = 1 - sum(current[k] for k in fixed)
    raw = {k: max(corr[k]["rho"], 0) + FLOOR for k in eligible}
    sugg = {k: mass * raw[k] / sum(raw.values()) for k in eligible}
    new = dict(current)
    for k in new_zero:
        new[k] = 0.0
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
    return new, notes, new_zero


def save_corr(conn, week_end: str, target: str, corr: dict[str, dict]) -> None:
    for k, c in corr.items():
        conn.execute("INSERT OR REPLACE INTO factor_corr_history (week_end, target, factor, n, rho) VALUES (?,?,?,?,?)",
                     (week_end, target, k, c["n"], c["rho"]))
    conn.commit()


def corr_history(conn, before: str, target: str, weeks: int = 4) -> dict[str, list[float]]:
    """이번 주 이전의 주별 ρ (오래된 것 → 최근). 표본이 MIN_FACTOR 미만인 주는 뺀다."""
    out: dict[str, list[float]] = {}
    for k in FACTORS:
        rows = conn.execute("SELECT rho FROM factor_corr_history WHERE factor=? AND target=? AND week_end<? AND n>=? "
                            "AND rho IS NOT NULL ORDER BY week_end DESC LIMIT ?", (k, target, before, MIN_FACTOR, weeks))
        out[k] = [r[0] for r in rows][::-1]
    return out


def apply(conn, new: dict[str, float], prev: dict[str, float], target: str, n: int, notes: list[str],
          zeroed: set[str] | None = None) -> int | None:
    """바뀐 게 있으면 weights.json 버전 올리고 기록. 새 버전 번호(없으면 None)."""
    cfg = load()
    if all(abs(new[k] - prev.get(k, 0)) < 0.0005 for k in FACTORS) and set(cfg.get("zeroed", [])) == set(zeroed or ()):
        return None
    cfg.update(version=int(cfg["version"]) + 1, updated=f"{dt.date.today():%Y-%m-%d}", weights=new,
               zeroed=sorted(zeroed or ()), note="주간 리뷰 자동 조절: " + " / ".join(notes)[:300])
    save(cfg)
    conn.execute("INSERT INTO weight_history (version, applied_at, target, sample_n, weights, prev_weights, note) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (cfg["version"], f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", target, n, json.dumps(new),
                  json.dumps(prev), cfg["note"]))
    conn.commit()
    return cfg["version"]


def render(week_start: str, week_end: str, week: dict, cum: dict, corr: dict, target: str,
           prev: dict, new: dict, new_version: int | None, notes: list[str], top: list[dict], bottom: list[dict],
           ver: int, corr2: dict | None = None, zeroed: set[str] | None = None) -> str:
    zeroed = zeroed or set()
    T = {"oc_excess": "시가→종가 초과수익(지수 대비)", "close_excess": "전일종가→종가 초과수익",
         "oc_pct": "시가→종가", "ret_5d": "5일 수익"}.get(target, target)
    L = [f"# 주간 리뷰 {week_start} ~ {week_end}", "",
         f"- 목표 지표: **{T}** (`{target}`)",
         f"- 이번 주 호재 {week['all_good']['n']}건 · 누적 {cum['all_good']['n']}건",
         f"- 가중치: v{ver}" + (f" → **v{new_version}로 조절**" if new_version else " (변경 없음)"),
         f"- 시가 상한가(매수 불가) {cum.get('open_limit', 0)}건은 시가→종가 목표에서 제외(누적)",
         "- 주의: v2·v3 초기 가중치는 사례 5건(11-12~13)에 맞춘 값이라 과적합 가능성이 있다. 아래 상관이 쌓이면서 조정된다.", ""]

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
        L.append("### 게시·접수 구간별 (호재) — 장 마감 후 15:30~07:00 · 장 전 07:00~08:00 · 장중 08:00~15:30")
        L.append("")
        L.append("| 근거 | 구간 | 건수 | 기대수익(평균) | 승률 | 중앙값 |")
        L.append("|---|---|---|---|---|---|")
        for kind, key in (("공시", "by_disclosure_session"), ("정책", "by_policy_session")):
            for ses in SESSIONS3:
                st = (data.get(key) or {}).get(ses) or {"n": 0}
                if not st["n"]:
                    L.append(f"| {kind} | {ses} | 0 | - | - | - |")
                    continue
                win = "-" if st["win"] is None else f"{st['win'] * 100:.0f}%"
                L.append(f"| {kind} | {ses} | {st['n']} | {pct(st['mean'])} | {win} | {pct(st['median'])} |")
        L.append("")
        L.append("<sub>공시는 가장 먼저 접수된 공시, 정책은 가장 먼저 게시된 근거 기준. 장 전 = 07:45 '장 전 추가'에서 채점한 종목 포함.</sub>")
        L.append("")
        L.append("### 부처별 (정책 근거 호재)")
        L.append("")
        if data.get("by_ministry"):
            L.append("| 부처 | 건수 | 기대수익(평균) | 승률 | 중앙값 | 5일 |")
            L.append("|---|---|---|---|---|---|")
            for m, st in data["by_ministry"].items():
                win = "-" if st["win"] is None else f"{st['win'] * 100:.0f}%"
                warn = " ⚠️ 표본 부족" if st["n"] < 10 else ""
                L.append(f"| [정책:{m}]{warn} | {st['n']} | {pct(st['mean'])} | {win} | {pct(st['median'])} | {pct(st['r5'])} |")
            L.append("")
            L.append(f"<sub>기대수익·승률 = 목표 지표({target}) 기준. 한 종목이 여러 부처 정책에 걸리면 부처마다 센다.</sub>")
        else:
            L.append("정책 근거 호재 없음")
        L.append("")

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
    L.append("| 항목 | 표본 | 순위상관 ρ | 보조 ρ(KRX 종가 기준) | 이전 가중치 | 새 가중치 |")
    L.append("|---|---|---|---|---|---|")
    for k in FACTORS:
        c = corr[k]
        rho = "-" if c["rho"] is None else f"{c['rho']:+.2f}"
        c2 = (corr2 or {}).get(k) or {}
        rho2 = "-" if c2.get("rho") is None else f"{c2['rho']:+.2f}"
        flag = " (경고 전용, 0%)" if k in zeroed else ""
        L.append(f"| {LABELS[k]}{flag} | {c['n']} | {rho} | {rho2} | {prev.get(k, 0) * 100:.1f}% | {new.get(k, 0) * 100:.1f}% |")
    L.append("")
    L.append("**조절 메모**")
    for n in notes or ["변경 없음"]:
        L.append(f"- {n}")
    L.append("")
    L.append(f"<sub>규칙: 누적 {MIN_TOTAL}건 이상일 때만 조절, 항목별 {MIN_FACTOR}건 이상, 한 번에 ±{MAX_STEP * 100:.0f}%p 이내, "
             f"범위 {MIN_W * 100:.0f}~{MAX_W * 100:.0f}%. {ZERO_WEEKS}주 연속 반대 상관이면 0(경고 전용), "
             f"{RESTORE_WEEKS}주 연속 양의 상관이면 {MIN_W * 100:.0f}%로 복귀. "
             f"보조 ρ는 KRX 기준가 대비 종가(시가 상한가 포함) — 참고용. 되돌리기: config/weights.json 또는 DB weight_history.</sub>")
    return "\n".join(L) + "\n"
