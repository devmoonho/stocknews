"""재료 영향 지속성 — 재료 유형별·근거별(공시/뉴스/정책) 평균 누적 경로 D0 ~ D+10.

기준: D0 시가에 샀다고 보고, 매 거래일 종가까지의 누적 수익률에서 같은 시장 지수의 같은 구간 수익률을 뺀 값(초과수익).
지표
  - 갭: D0 시가가 전일 종가 대비 이미 얼마나 반영됐나(아침에 들어가기 전 상승분)
  - D+n: 평균 누적 초과수익 (괄호 안 = 그날까지 데이터가 있는 표본 수)
  - 정점: 평균 경로가 가장 높은 날과 값
  - 지속: 정점 뒤 평균 경로가 정점의 절반 이하로 처음 떨어진 날 (없으면 'D+10 이후')
  - 최대 상승 여지: D+10까지 장중 최고가 기준 평균 (시가 대비)
표본이 적은 날(MIN_N 미만)은 정점·지속 판단에서 뺀다.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from .categories import CATEGORIES, label

SHOW_DAYS = (0, 1, 2, 3, 5, 10)
MIN_N = 3


def _load(conn, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT p.date, p.stock_code, p.basis, p.gap_pct, s.category, s.stock_name FROM price_reactions p "
        "JOIN scores s ON s.date=p.date AND s.stock_code=p.stock_code "
        "WHERE p.grp='scored' AND p.sentiment='호재' AND p.date <= ?", (end,)).fetchall()
    items = {(r["date"], r["stock_code"]): {**dict(r), "path": {}} for r in rows}
    for r in conn.execute("SELECT * FROM price_paths WHERE date <= ?", (end,)):
        key = (r["date"], r["stock_code"])
        if key in items:
            items[key]["path"][r["n"]] = dict(r)
    return [it for it in items.values() if it["path"]]


def _profile(items: list[dict]) -> dict:
    by_n: dict[int, list[float]] = defaultdict(list)
    highs: list[float] = []
    for it in items:
        for n, p in it["path"].items():
            if p["cum_excess"] is not None:
                by_n[n].append(p["cum_excess"])
        last = max(it["path"])
        if it["path"][last]["max_high_ret"] is not None:
            highs.append(it["path"][last]["max_high_ret"])
    mean = {n: statistics.fmean(v) for n, v in by_n.items() if v}
    count = {n: len(v) for n, v in by_n.items()}
    win = {n: sum(x > 0 for x in v) / len(v) for n, v in by_n.items() if v}
    usable = [n for n in sorted(mean) if count[n] >= MIN_N]
    peak_n = max(usable, key=lambda n: mean[n]) if usable else None
    fade_n = None
    if peak_n is not None and mean[peak_n] > 0:
        for n in usable:
            if n > peak_n and mean[n] <= mean[peak_n] / 2:
                fade_n = n
                break
    gaps = [it["gap_pct"] for it in items if it.get("gap_pct") is not None]
    return {"n": len(items), "mean": mean, "count": count, "win": win, "peak_n": peak_n,
            "peak": mean.get(peak_n) if peak_n is not None else None, "fade_n": fade_n,
            "gap": statistics.fmean(gaps) if gaps else None, "max_high": statistics.fmean(highs) if highs else None,
            "max_days": max(count) if count else None}


def _day(n: int) -> str:
    return "D0" if n == 0 else f"D+{n}"


def _pct(v, d: int = 1) -> str:
    return "-" if v is None else f"{v * 100:+.{d}f}%"


def _sentence(name: str, p: dict) -> str:
    if p["peak_n"] is None:
        return f"- **{name}** ({p['n']}건): 아직 표본 부족 (날짜별 {MIN_N}건 미만)"
    if p["peak"] <= 0:
        why = ("시초가에 이미 반영됐을 가능성" if (p["gap"] or 0) > 0.01 else
               "시초가부터 약했고 장중에도 회복 못 함" if (p["gap"] or 0) < -0.01 else "재료 효과가 뚜렷하지 않음")
        return (f"- **{name}** ({p['n']}건): 시가 매수 기준으로 지수보다 나은 날이 없음 "
                f"(가장 나은 날 {_day(p['peak_n'])} {_pct(p['peak'])}). 시초가 갭 평균 {_pct(p['gap'])} — {why}")
    if p["fade_n"] is not None:
        fade = f"{_day(p['fade_n'])}에 절반 이하로 감소"
    elif p["max_days"] == p["peak_n"]:
        fade = "이후 거래일 데이터 대기 중"
    else:
        fade = f"{_day(p['max_days'])}까지 정점의 절반 이상 유지"
    return (f"- **{name}** ({p['n']}건): 평균 **{_day(p['peak_n'])}에 {_pct(p['peak'])}** 로 정점, {fade}. "
            f"시초가 갭 평균 {_pct(p['gap'])}, 장중 최고가 기준 최대 {_pct(p['max_high'])}")


def _table(title: str, groups: dict[str, dict]) -> list[str]:
    head = " | ".join(f"D+{n}" if n else "D0" for n in SHOW_DAYS)
    L = [f"### {title}", "", f"| 구분 | 건수 | 시초가 갭 | {head} | 정점 | 지속 |",
         "|---|---|---|" + "---|" * len(SHOW_DAYS) + "---|---|"]
    for name, p in groups.items():
        cells = []
        for n in SHOW_DAYS:
            if n not in p["mean"]:
                cells.append("-")
            else:
                c = p["count"][n]
                cells.append(_pct(p["mean"][n]) + ("" if c == p["n"] else f" ({c})"))
        peak = f"{_day(p['peak_n'])} {_pct(p['peak'])}" if p["peak_n"] is not None else "-"
        fade = "-" if p["peak_n"] is None or (p["peak"] or 0) <= 0 else (
            f"{_day(p['fade_n'])}에 반감" if p["fade_n"] is not None else
            "데이터 대기" if p["max_days"] == p["peak_n"] else f"{_day(p['max_days'])}까지 유지")
        L.append(f"| {name} | {p['n']} | {_pct(p['gap'])} | {' | '.join(cells)} | {peak} | {fade} |")
    L.append("")
    return L


def render(conn, end: str) -> str:
    items = _load(conn, end)
    L = ["## 재료 영향 지속성 (누적, 호재)", "",
         "D0 시가 매수 기준, 같은 시장 지수 대비 **누적 초과수익** 평균. 괄호 = 그날까지 데이터가 있는 표본 수.", ""]
    if not items:
        L.append("아직 경로 데이터 없음 — 평일 20:10 반응 기록이 쌓이면 표시됩니다.")
        return "\n".join(L) + "\n"

    by_cat = defaultdict(list)
    for it in items:
        by_cat[it.get("category") or "other"].append(it)
    cat_groups = {label(k): _profile(v) for k, v in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))}
    basis_groups = {}
    for b in ("공시", "뉴스", "정책"):
        g = [it for it in items if b in (it.get("basis") or "")]
        if g:
            basis_groups[f"[{b}]"] = _profile(g)
    combo = defaultdict(list)                    # 근거 × 유형 (예: [공시] 공급계약·수주)
    for it in items:
        for b in (it.get("basis") or "").split(","):
            if b:
                combo[f"[{b}] {label(it.get('category'))}"].append(it)
    combo_groups = {k: _profile(v) for k, v in sorted(combo.items(), key=lambda kv: -len(kv[1])) if len(v) >= MIN_N}

    L.append("**요약**")
    for name, p in {**cat_groups, **basis_groups}.items():
        L.append(_sentence(name, p))
    L.append("")
    L += _table("재료 유형별", cat_groups)
    L += _table("근거별 (공시 / 뉴스 / 정책)", basis_groups)
    if combo_groups:
        L += _table(f"근거 × 유형 ({MIN_N}건 이상)", combo_groups)
    L.append(f"<sub>정점·지속 판단은 그날 표본 {MIN_N}건 이상인 날만 사용. 유형 목록: "
             f"{', '.join(v[0] for v in CATEGORIES.values())}</sub>")
    L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 장중 상한가 패턴 (보조 지표)
def render_intraday(conn, end: str) -> str:
    """점수별·재료 유형별 상한가 도달·해제·종가 유지 비율 (누적). 표본이 적어도 그대로 보여 준다."""
    rows = [dict(r) for r in conn.execute(
        "SELECT i.*, s.final, s.category FROM intraday_patterns i JOIN scores s "
        "ON s.date=i.date AND s.stock_code=i.stock_code WHERE i.date <= ? AND s.sentiment='호재'", (end,))]
    L = ["## 장중 상한가 패턴 (누적, 호재)", ""]
    if not rows:
        L.append("아직 기록 없음 — 평일 20:10 반응 기록 때 채점 종목의 1분봉으로 쌓입니다.")
        return "\n".join(L) + "\n"
    L += ["| 구분 | 건수 | 상한가 도달 | 해제 후 재도달 | 종가 상한가 | 평균 해제 횟수(도달 종목) |", "|---|---|---|---|---|---|"]

    def line(name: str, g: list[dict]) -> str:
        hit = [r for r in g if r["hit"]]
        rate = lambda xs: f"{len(xs) / len(g) * 100:.0f}% ({len(xs)})"  # noqa: E731
        rel = statistics.fmean([r["releases"] for r in hit]) if hit else None
        return (f"| {name} | {len(g)} | {rate(hit)} | {rate([r for r in hit if r['rehit']])} | "
                f"{rate([r for r in g if r['closed_at_limit']])} | {'-' if rel is None else f'{rel:.1f}'} |")
    for s in (5, 4, 3, 2, 1):
        g = [r for r in rows if r["final"] == s]
        if g:
            L.append(line(f"{s}점", g))
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r.get("category") or "other"].append(r)
    for k, g in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        L.append(line(label(k), g))
    L += ["", "<sub>상한가 = 전일 종가×1.3(호가단위 내림). 해제 = 상한가 도달 뒤 1분봉이 상한가 아래로 내려온 횟수. "
          "가중치 조절 목표는 여전히 시가→종가 지수 대비 초과수익이고, 이 표는 재료 신뢰도 참고용.</sub>", ""]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 테마 대장 후보 기준 검증
def render_leaders(conn, end: str) -> str:
    """같은 테마 안에서 대장 후보(연관도 1·2위) vs 나머지(고른 종목 + 안 고른 비교 종목)의 D0 반응 (누적)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM price_reactions WHERE theme IS NOT NULL AND date <= ? AND grp IN ('scored','theme_peer')", (end,))]
    L = ["## 테마 대장 후보 기준 검증 (누적)", ""]
    if not rows:
        L.append("아직 기록 없음 — 테마·정책에서 고른 종목이 있는 날, 같은 테마 상위 종목이 비교 종목으로 쌓입니다.")
        return "\n".join(L) + "\n"

    def stat(g: list[dict], key: str) -> str:
        v = [r[key] for r in g if r.get(key) is not None]
        return "-" if not v else f"{statistics.fmean(v) * 100:+.1f}% ({len(v)})"
    lead = [r for r in rows if r.get("leader_rank") and r["leader_rank"] <= 2]
    rest = [r for r in rows if not (r.get("leader_rank") and r["leader_rank"] <= 2)]
    L += ["| 구분 | 건수 | 시초가 갭 | 전일 KRX 종가→종가 (지수 대비) | 시가→종가 (지수 대비, 시가 상한가 제외) | 시가 상한가 |",
          "|---|---|---|---|---|---|"]
    for name, g in (("대장 후보(1·2위)", lead), ("나머지", rest),
                    ("나머지 중 고른 종목", [r for r in rest if r["grp"] == "scored"]),
                    ("나머지 중 안 고른 종목", [r for r in rest if r["grp"] == "theme_peer"])):
        if not g:
            continue
        oc = [dict(r, oc_excess=None) if r.get("open_limit") else r for r in g]
        L.append(f"| {name} | {len(g)} | {stat(g, 'gap_pct')} | {stat(g, 'krx_close_excess')} | {stat(oc, 'oc_excess')} | "
                 f"{sum(bool(r.get('open_limit')) for r in g)} |")
    L += ["", "<sub>대장 후보 = 재료 본문 핵심 단어와 편입 사유가 겹치는 정도 순위(leader.py). 괄호 = 표본 수. "
          "대장 후보가 나머지보다 꾸준히 높아야 기준이 맞는 것.</sub>", ""]
    return "\n".join(L) + "\n"
