"""주간 리뷰 '테마 예측' 섹션 — 예측 vs 기준선, 중복 회피 전/후, 등락 기준 비교, 단계·서열별 다음날 성과, 2등주 추종.

누적(end 이하 전체) 기준. 표본이 적은 초기에는 방향 확인용이라고 표시한다. 근사 재구성(approx) 날짜는 따로 센다.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from . import theme_groups


def _avg(v: list[float]) -> str:
    v = [x for x in v if x is not None]
    return f"{statistics.fmean(v):+.2f}" if v else "-"


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}% ({a}/{b})" if b else "-"


def render(conn, end: str) -> str:
    L = ["## 테마 예측 (누적)", ""]
    days = [r[0] for r in conn.execute("SELECT DISTINCT dt FROM theme_daily WHERE dt<=? ORDER BY dt", (end,))]
    if not days:
        L.append("아직 기록 없음 — 평일 15:35 테마 집계부터 쌓입니다.")
        return "\n".join(L) + "\n"
    approx = conn.execute("SELECT COUNT(DISTINCT dt) FROM theme_daily WHERE dt<=? AND approx=1", (end,)).fetchone()[0]
    L.append(f"집계 {len(days)}거래일 ({days[0]} ~ {days[-1]}){f', 그중 근사 재구성 {approx}일' if approx else ''}. "
             f"표본이 수 주 쌓이기 전에는 **방향 확인용**.")
    L.append("")

    # 1. 예측 vs 기준선, 중복 회피 전/후
    L += ["### 예측 적중 — 저녁 1차(15:30 이후 재료) vs 아침 최종(밤사이 재료) vs 기준선 '어제 상위 유지'", "",
          "| 단계 | 구분 | 중복 회피 | 일수 | 상위5 평균 초과(%p) | 상위10 적중 | 대장 적중 | 2등주 적중 |",
          "|---|---|---|---|---|---|---|---|"]
    combos = [(ph, pl, k, kl) for ph, pl in (("evening", "저녁 1차"), ("morning", "아침 최종"))
              for k, kl in (("forecast", "예측"), ("baseline", "기준선"))]
    for phase, pl, kind, label in combos:
        for dd in (1, 0):
            rs = conn.execute("SELECT * FROM theme_forecast_results WHERE phase=? AND kind=? AND dedupe=? AND date<=?",
                              (phase, kind, dd, end)).fetchall()
            if not rs:
                continue
            L.append(f"| {pl} | {label} | {'적용' if dd else '미적용'} | {len(rs)} | {_avg([r['avg_excess'] for r in rs])} | "
                     f"{_pct(sum(r['top10_hits'] for r in rs), sum(r['n'] for r in rs))} | "
                     f"{_pct(sum(r['leader_hits'] for r in rs), sum(r['leader_n'] for r in rs))} | "
                     f"{_pct(sum(r['second_hits'] for r in rs), sum(r['second_n'] for r in rs))} |")
    L.append("")

    # 2~3. 등락 기준 비교 · 단계별 다음날
    rows = defaultdict(dict)
    for r in conn.execute("SELECT * FROM theme_daily WHERE dt<=? AND small=0", (end,)):
        rows[r["dt"]][r["group_id"]] = dict(r)
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    by_metric: dict[str, list[float]] = defaultdict(list)
    by_stage: dict[str, list[float]] = defaultdict(list)
    for d, groups in rows.items():
        n = nxt.get(d)
        if not n or n not in rows:
            continue
        for metric in ("median_excess", "avg_excess", "wavg_excess"):
            top = sorted((g for g in groups.values() if g[metric] is not None), key=lambda g: -g[metric])[:10]
            by_metric[metric] += [rows[n][g["group_id"]]["median_excess"] for g in top if g["group_id"] in rows[n]]
        for g in groups.values():
            if g["group_id"] in rows[n]:
                by_stage[g["stage"] or "없음"].append(rows[n][g["group_id"]]["median_excess"])
    if by_metric:
        L += ["### 테마 등락 기준 비교 — 그날 상위 10개 묶음의 다음날 중앙값 초과 평균", "",
              "| 기준 | 표본 | 다음날 평균(%p) |", "|---|---|---|"]
        for metric, label in (("median_excess", "중앙값"), ("avg_excess", "단순 평균"), ("wavg_excess", "거래대금 가중")):
            L.append(f"| {label} | {len(by_metric[metric])} | {_avg(by_metric[metric])} |")
        L.append("")
        L += ["### 진행 단계별 다음날 성과", "", "| 단계 | 표본 | 다음날 중앙값 초과 평균(%p) | 다음날 플러스 비율 |", "|---|---|---|---|"]
        for st in ("태동", "확산", "과열", "소멸", "없음"):
            v = by_stage.get(st, [])
            if v:
                L.append(f"| {st} | {len(v)} | {_avg(v)} | {_pct(sum(x > 0 for x in v), len(v))} |")
        L.append("")

    # 4. 서열별 D+1
    lab = conn.execute("SELECT label, COUNT(*) AS n, AVG(next_excess) AS a, SUM(next_excess > 0) AS p "
                       "FROM theme_members_daily WHERE dt<=? AND label IS NOT NULL AND next_excess IS NOT NULL "
                       "GROUP BY label", (end,)).fetchall()
    if lab:
        L += ["### 서열별 다음날 성과 (종목, 지수 대비 초과)", "", "| 서열 | 표본 | 평균(%p) | 플러스 비율 |", "|---|---|---|---|"]
        order = {"대장": 0, "2등주": 1, "후발주": 2}
        for r in sorted(lab, key=lambda r: order.get(r["label"], 9)):
            L.append(f"| {r['label']} | {r['n']} | {r['a']:+.2f} | {_pct(r['p'] or 0, r['n'])} |")
        L.append("")

    # 5. 대장 상한가 시 2등주 추종
    fol = conn.execute(
        "SELECT m.after_leader_pct, m.next_excess FROM theme_members_daily m JOIN theme_members_daily l "
        "ON l.dt=m.dt AND l.group_id=m.group_id AND l.label='대장' AND l.limit_hit=1 AND l.closed_at_limit=1 "
        "WHERE m.label='2등주' AND m.dt<=?", (end,)).fetchall()
    if fol:
        d0 = [r["after_leader_pct"] for r in fol if r["after_leader_pct"] is not None]
        d1 = [r["next_excess"] for r in fol if r["next_excess"] is not None]
        L.append(f"- **대장 상한가 유지 시 2등주**: {len(fol)}건 · 대장 도달 뒤 추가 상승 플러스 "
                 f"{_pct(sum(x > 0 for x in d0), len(d0))}, 평균 {_avg(d0)}% · 다음날 초과 평균 {_avg(d1)}%p")
    lc = conn.execute("SELECT COUNT(*), SUM(leader_changed) FROM theme_daily WHERE dt<=? AND rank<=20", (end,)).fetchone()
    if lc[0]:
        L.append(f"- **대장 교체 빈도**(상위 20 묶음): {_pct(lc[1] or 0, lc[0])}")
    L.append("")
    # 6. 묶음 크기 점검
    L.append("### 테마 묶음 크기 점검")
    L.append("")
    L.append("```")
    L.append(theme_groups.size_report(theme_groups.load_groups(conn)))
    L.append("```")
    return "\n".join(L) + "\n"
