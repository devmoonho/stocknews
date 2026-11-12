"""4단계: scored.json → reports/YYYY-MM-DD.md

구성
  1. 요약 (시간대, 수집 건수, 경고)
  2. 장 시작 전 체크리스트 (수동 기입): 지수 흐름 / 장초반 매수세 / 외국인·기관·개인 수급
  3. 호재 종목 순위표 (점수 높은 순, 근거 [공시]·[뉴스]·[정책]) + 종목별 수동 확인 칸
  3-1. 공시 기반 종목 (공시명·규모 수치·접수 시각)
  4. 종목별 상세: 재료 요약, 항목별 점수·한 줄 근거, 원문 링크
  5. 주의 종목(악재)
  6. 정부 정책뉴스 검토 결과
  7. 수집 현황
"""
from __future__ import annotations

import json

from .analysis.scoring import WEIGHTS

FACTOR_LABELS = {"scale": "재료 규모", "exclusivity": "최초/독점", "novelty": "신규성",
                 "policy": "정책 수혜", "diffusion": "확산도"}
STARS = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆"}


def _md(s: str | None) -> str:
    """표 안에서 깨지지 않게."""
    return (s or "").replace("|", "／").replace("\n", " ").strip()


def resolve_sources(conn, sources: list[str]) -> list[tuple[str, str]]:
    """['rcept:…', 'article:12', 'https://…'] → [(표시 이름, URL)]"""
    out = []
    for src in sources or []:
        kind, _, val = str(src).partition(":")
        if kind == "rcept":
            r = conn.execute("SELECT report_nm, received_at, rcept_dt, url FROM disclosures WHERE rcept_no=?",
                             (val,)).fetchone()
            if r:
                when = (r["received_at"] or r["rcept_dt"] or "")[5:16]
                out.append((f"[공시] {r['report_nm']} ({when})", r["url"]))
                continue
            out.append((f"[공시] {val}", f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={val}"))
        elif kind == "article":
            r = conn.execute("SELECT title, office_name, published_at, url FROM articles WHERE id=?",
                             (val,)).fetchone()
            if r:
                out.append((f"[{r['office_name']}] {r['title']} ({r['published_at'][5:16]})", r["url"]))
        elif kind in ("url", "http", "https"):
            url = val if kind == "url" else src
            if not any(u == url for _, u in out):
                out.append(("[링크]", url))
    # 같은 URL 중복 제거
    seen, uniq = set(), []
    for name, url in out:
        if url not in seen:
            seen.add(url)
            uniq.append((name, url))
    return uniq


def basis_tags(conn, it: dict) -> list[str]:
    """근거 종류: 공시 / 뉴스 / 정책 (sources와 id로 판단)."""
    tags = []
    for src in it.get("sources", []):
        kind, _, val = str(src).partition(":")
        if kind == "rcept":
            tags.append("공시")
        elif kind == "article":
            r = conn.execute("SELECT source FROM articles WHERE id=?", (val,)).fetchone()
            tags.append("정책" if r and r["source"] == "korea_policy" else "뉴스")
        elif "korea.kr" in str(src):
            tags.append("정책")
    if str(it.get("id", "")).startswith("P-"):
        tags.append("정책")
    order = ["공시", "뉴스", "정책"]
    return [t for t in order if t in tags]


def disclosure_rows(candidates: dict, it: dict) -> list[dict]:
    """채점 항목이 근거로 든 공시들 + 추출 수치(candidates.json)."""
    wanted = {str(x).split(":", 1)[1] for x in it.get("sources", []) if str(x).startswith("rcept:")}
    out = []
    for st in candidates.get("stocks", []):
        if st["code"] != it["code"]:
            continue
        for d in st["disclosures"]:
            if d["rcept_no"] in wanted:
                out.append(d)
    return out


def figures_text(f: dict | None) -> str:
    if not f:
        return "-"
    parts = []
    if f.get("amount") is not None:
        v = f["amount"]
        parts.append(f"{v / 1e8:,.0f}억" if v >= 1e8 else f"{v:,.0f}원")
    if f.get("revenue_ratio_pct") is not None:
        parts.append(f"매출 대비 {f['revenue_ratio_pct']}%")
    if f.get("market_cap_ratio_pct") is not None:
        parts.append(f"시총 대비 {f['market_cap_ratio_pct']}%")
    if f.get("note"):
        parts.append(f["note"])
    return " · ".join(parts) or "-"


def collect_status(conn, window: dict) -> tuple[list[dict], list[str]]:
    """이 시간대로 실행한 수집·마스터 갱신 기록(소스별 최신)과 경고."""
    rows = conn.execute(
        "SELECT source, MAX(id) AS id FROM collect_runs WHERE window_start=? AND window_end=? GROUP BY source",
        (window["start"], window["end"])).fetchall()
    runs = []
    for r in rows:
        run = dict(conn.execute("SELECT * FROM collect_runs WHERE id=?", (r["id"],)).fetchone())
        run["warnings"] = json.loads(run["warnings"] or "[]")
        runs.append(run)
    # 종목 마스터는 시간대와 무관 → 그날 최신 갱신 기록
    for src in ("kiwoom_stocks", "kiwoom_themes", "naver_themes"):
        r = conn.execute("SELECT * FROM collect_runs WHERE source=? ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        if r:
            run = dict(r)
            run["warnings"] = json.loads(run["warnings"] or "[]")
            runs.append(run)
    warnings = [w for run in runs for w in run["warnings"]]
    return runs, warnings


SOURCE_NAMES = {"naver_focus_401": "네이버 시황·전망", "korea_policy": "정책브리핑 정책뉴스", "dart": "DART 공시",
                "kiwoom_stocks": "종목 마스터(키움)", "kiwoom_themes": "테마(키움)", "naver_themes": "테마(네이버)"}


def render(conn, scored: dict, candidates: dict) -> str:
    date = scored["date"]
    w = scored["window"]
    items = scored["items"]
    good = [i for i in items if i["sentiment"] == "호재"]
    bad = [i for i in items if i["sentiment"] == "악재"]
    runs, warnings = collect_status(conn, w)
    c = candidates.get("counts", {})
    L: list[str] = []

    L.append(f"# {date} 장전 재료 리포트")
    L.append("")
    L.append(f"- 대상 시간대: **{w['start'][:16]} ~ {w['end'][:16]}**")
    L.append(f"- 수집: 기사 {c.get('articles', '-')}건(정부 정책뉴스 {c.get('gov_policy', '-')}건 포함) · "
             f"공시 {c.get('disclosures_listed', '-')}건 → 후보 {c.get('stocks', '-')}종목 검토")
    L.append(f"- 결과: 호재 **{len(good)}종목** · 주의(악재) {len(bad)}종목")
    L.append("- 점수는 고정 채점표(`config/scoring_rubric.md`) 기준의 재료 강도이며 **매수 추천이 아님**. 최종 판단은 직접.")
    if warnings:
        L.append("")
        L.append("> ⚠️ **수집 경고** — 결과가 불완전할 수 있음")
        for x in warnings:
            L.append(f"> - {x}")
    L.append("")

    # ------------------------------------------------------------ 체크리스트
    L.append("## 장 시작 전 체크리스트 (직접 기입)")
    L.append("")
    L.append("| 항목 | 확인 내용 | 메모 |")
    L.append("|---|---|---|")
    L.append("| 지수 흐름 | 美 증시 마감 / 코스피·코스닥 선물 / 환율 |  |")
    L.append("| 장초반 매수세 | 09:00~09:10 지수 방향, 거래대금 상위 업종 |  |")
    L.append("| 외국인 수급 | 선물·현물 순매수 방향 |  |")
    L.append("| 기관 수급 | 순매수 방향, 프로그램 매매 |  |")
    L.append("| 개인 수급 | 순매수 방향 |  |")
    L.append("| 오늘의 판단 | 공격 / 중립 / 관망 |  |")
    L.append("")

    # ------------------------------------------------------------ 순위표
    L.append("## 호재 종목 (점수 높은 순)")
    L.append("")
    if good:
        L.append("| 순위 | 종목 | 점수 | 근거 | 재료 | 시초가 | 장초반 매수세 | 메모 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for rank, it in enumerate(good, 1):
            tags = "·".join(basis_tags(conn, it)) or "-"
            L.append(f"| {rank} | **{it['name']}** ({it['code']}) | {it['final']}점 ({it['final_raw']}) | {tags} | "
                     f"{_md(it['material'])} |  |  |  |")
    else:
        L.append("해당 없음")
    L.append("")
    weights = " · ".join(f"{FACTOR_LABELS[k]} {int(v * 100)}%" for k, v in WEIGHTS.items())
    L.append(f"<sub>가중치: {weights} (정책 재료가 아니면 정책 수혜 제외 후 재계산)</sub>")
    L.append("")

    # ------------------------------------------------------------ 공시 기반
    disc_items = [(n, it) for n, it in enumerate(good, 1) if "공시" in basis_tags(conn, it)]
    L.append(f"## 공시 기반 종목 ({len(disc_items)})")
    L.append("")
    if disc_items:
        L.append("| 순위 | 종목 | 점수 | 공시 | 규모 | 접수 |")
        L.append("|---|---|---|---|---|---|")
        for n, it in disc_items:
            for d in disclosure_rows(candidates, it) or [{}]:
                L.append(f"| {n} | **{it['name']}** | {it['final']}점 | "
                         f"[{_md(d.get('report_nm', '-'))}]({d.get('url', '')}) | {_md(figures_text(d.get('figures')))} | "
                         f"{_md(str(d.get('received_at', ''))[5:16])} |")
        L.append("")
        L.append("<sub>기사 없이 공시만 있는 종목은 확산도를 빼고 계산(밤사이 공시는 아침까지 기사가 없는 게 보통).</sub>")
    else:
        L.append("해당 없음")
    L.append("")

    # ------------------------------------------------------------ 상세
    L.append("## 종목별 상세")
    L.append("")
    for rank, it in enumerate(good, 1):
        tags = " ".join(f"[{t}]" for t in basis_tags(conn, it))
        L.append(f"### {rank}. {it['name']} ({it['code']}) — {it['final']}점 {STARS.get(it['final'], '')} {tags}")
        L.append("")
        L.append(f"**재료**: {it['material']}")
        L.append("")
        L.append("| 항목 | 점수 | 근거 |")
        L.append("|---|---|---|")
        for k in WEIGHTS:
            f = (it.get("factors") or {}).get(k)
            if f is None:
                if k == "diffusion":
                    L.append(f"| {FACTOR_LABELS[k]} | 제외 | 공시만 있고 관련 기사 없음 |")
                continue
            L.append(f"| {FACTOR_LABELS[k]} | {f['score']} | {_md(f.get('reason'))} |")
        L.append("")
        if it.get("caution"):
            L.append(f"⚠️ {it['caution']}")
            L.append("")
        links = resolve_sources(conn, it.get("sources", []))
        if links:
            L.append("원문:")
            for name, url in links:
                L.append(f"- [{_md(name)}]({url})")
            L.append("")

    # ------------------------------------------------------------ 악재
    L.append("## 주의 종목 (악재)")
    L.append("")
    if bad:
        L.append("| 종목 | 내용 | 원문 |")
        L.append("|---|---|---|")
        for it in bad:
            links = resolve_sources(conn, it.get("sources", []))
            link = " · ".join(f"[{i + 1}]({u})" for i, (_, u) in enumerate(links[:3]))
            note = it["material"] + (f" — {it['caution']}" if it.get("caution") else "")
            L.append(f"| **{it['name']}** ({it['code']}) | {_md(note)} | {link} |")
    else:
        L.append("해당 없음")
    L.append("")

    # ------------------------------------------------------------ 정책
    reviewed = scored.get("reviewed_policies") or []
    gov = {f"P-{i}": g for i, g in enumerate(candidates.get("gov_policy", []), 1)}
    names = {i["code"]: i["name"] for i in items}
    L.append("## 정부 정책뉴스 검토")
    L.append("")
    if reviewed:
        L.append("| 정책 | 부처 | 수혜 종목 | 판단 |")
        L.append("|---|---|---|---|")
        for r in reviewed:
            g = gov.get(r.get("id"), {})
            title = f"[{_md(r.get('title') or g.get('title'))}]({g['url']})" if g.get("url") else _md(r.get("title"))
            picked = ", ".join(names.get(code, code) for code in r.get("picked", [])) or "-"
            L.append(f"| {title} | {_md(g.get('dept'))} | {picked} | {_md(r.get('note'))} |")
    elif gov:
        L.append("검토 기록 없음 (scores.json의 reviewed_policies 누락)")
    else:
        L.append("시간대 안의 정책뉴스 없음")
    L.append("")

    # ------------------------------------------------------------ 수집 현황
    L.append("## 수집 현황")
    L.append("")
    L.append("| 소스 | 건수 | 실행 시각 | 경고 |")
    L.append("|---|---|---|---|")
    for run in runs:
        L.append(f"| {SOURCE_NAMES.get(run['source'], run['source'])} | {run['fetched']} | "
                 f"{(run['finished_at'] or run['started_at'])[5:16]} | {len(run['warnings']) or '-'} |")
    L.append("")
    L.append(f"<sub>생성: scored.json · candidates.json ({date}) / DB data/stock_news.db</sub>")
    return "\n".join(L) + "\n"
