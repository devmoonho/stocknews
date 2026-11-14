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
from .collectors.ministry_press import GOV_SOURCES, ministry_name, policy_label, posted_session

FACTOR_LABELS = {"scale": "재료 규모", "exclusivity": "최초/독점", "novelty": "신규성",
                 "policy": "정책 수혜", "diffusion": "확산도",
                 "quality": "재료의 질", "preprice": "선반영(덜 오름)",
          "theme_cluster": "테마 결집도", "leader": "테마 대장"}
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
            r = conn.execute("SELECT source, title, office_name, published_at, url FROM articles WHERE id=?",
                             (val,)).fetchone()
            if r:
                tag = policy_label(r["office_name"]) if r["source"] in GOV_SOURCES else r["office_name"]
                out.append((f"[{tag}] {r['title']} ({r['published_at'][5:16]})", r["url"]))
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
            tags.append("정책" if r and r["source"] in GOV_SOURCES else "뉴스")
        elif "korea.kr" in str(src):
            tags.append("정책")
    if str(it.get("id", "")).startswith("P-"):
        tags.append("정책")
    order = ["공시", "뉴스", "정책"]
    return [t for t in order if t in tags]


def _policy_rows(conn, it: dict) -> list:
    rows = []
    for src in it.get("sources", []):
        kind, _, val = str(src).partition(":")
        q = ("SELECT a.source, a.office_name, a.published_at, p.time_basis FROM articles a "
             "LEFT JOIN press_meta p ON p.article_id=a.id WHERE ")
        r = None
        if kind == "article":
            r = conn.execute(q + "a.id=?", (val,)).fetchone()
        elif str(src).startswith("http"):
            r = conn.execute(q + "a.url=?", (str(src),)).fetchone()
        if r and r["source"] in GOV_SOURCES:
            rows.append(r)
    return rows


def policy_ministries(conn, it: dict) -> list[str]:
    """정책 근거의 부처명(정책브리핑 표기는 수집 부처 표시명으로 통일). 부처별 집계·[정책:부처명] 표시용."""
    out = []
    for r in _policy_rows(conn, it):
        name = ministry_name(r["office_name"])
        if name not in out:
            out.append(name)
    if not out and "정책" in basis_tags(conn, it):
        out.append("미상")
    return out


def policy_session(conn, it: dict) -> str | None:
    """정책 근거의 게시 구간: '장중'(07:00~15:30 게시가 하나라도 있으면) / '장 마감 후' / None(정책 근거 없음)."""
    rows = _policy_rows(conn, it)
    if not rows:
        return None
    return "장중" if any(posted_session(r["published_at"], r["time_basis"]) == "장중" for r in rows) else "장 마감 후"


def basis_labels(conn, it: dict) -> list[str]:
    """표시용 근거: 공시 / 뉴스 / 정책:부처명 (정책은 부처마다 하나씩)."""
    tags = basis_tags(conn, it)
    out = [t for t in tags if t != "정책"]
    if "정책" in tags:
        out += [f"정책:{m}" for m in policy_ministries(conn, it)]
        if policy_session(conn, it) == "장중":
            out.append("장중 게시")
    return out


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
    if f.get("years") and f["years"] > 1:
        parts.append(f"계약 {f['years']}년(연환산 {f.get('annual_revenue_ratio_pct')}%)")
    if f.get("market_cap_ratio_pct") is not None:
        parts.append(f"시총 대비 {f['market_cap_ratio_pct']}%")
    if f.get("kind") == "자사주취득" and f.get("method"):
        parts.append(f["method"] + (f", 하루 약 {f['daily_amount'] / 1e8:,.1f}억" if f.get("daily_amount") else ""))
    if f.get("kind") == "유상증자":
        if f.get("discount_pct") is not None:
            parts.append(f"할인 {f['discount_pct']}%")
        if f.get("payment_date"):
            parts.append(f"납입 {f['payment_date']}")
        if f.get("lockup"):
            parts.append(f"보호예수 {f['lockup']}")
    if f.get("kind") == "경영권변경" and f.get("acquirer"):
        parts.append(f"양수인 {f['acquirer']}")
    if f.get("note"):
        parts.append(f["note"])
    return " · ".join(parts) or "-"


def theme_bundles(scored: dict, candidates: dict) -> list[dict]:
    """테마 신호(T-)·정부 정책(P-)에서 고른 종목을 테마별로 묶는다: 결집도, 대장 후보, 고른 종목, 안 고른 상위 종목."""
    bundles: dict[str, dict] = {}
    sigs, govs = candidates.get("theme_signals", []), candidates.get("gov_policy", [])
    for it in scored.get("items", []):
        iid = str(it.get("id", ""))
        if it.get("sentiment") != "호재" or not iid.startswith(("T-", "P-")):
            continue
        try:
            n = int(iid[2:].split(":")[0]) - 1
            if iid.startswith("T-"):
                sig = sigs[n]
                theme, cluster, ranked = " / ".join(sig["themes"]), sig.get("cluster"), \
                    [m for m in sig.get("members", []) if m.get("rank")]
            else:
                lr = govs[n].get("leader_rank", {}).get(it["code"])
                if not lr:
                    continue
                theme, cluster = lr["theme"], lr["cluster"]
                ranked = next((ld["top"] for ld in govs[n].get("leaders", []) if ld["theme"] == theme), [])
        except (ValueError, IndexError, KeyError):
            continue
        b = bundles.setdefault(theme, {"theme": theme, "cluster": cluster, "ranked": ranked, "picked": []})
        b["picked"].append(it)
    out = []
    for b in bundles.values():
        picked = {it["code"] for it in b["picked"]}
        b["leaders"] = [m for m in b["ranked"] if m["rank"] and m["rank"] <= 2]
        b["others"] = [m for m in b["ranked"] if m["code"] not in picked and (m["rank"] or 99) > 2][:4]
        b["picked"].sort(key=lambda it: -(it.get("final_raw") or 0))
        out.append(b)
    out.sort(key=lambda b: -max(it.get("final_raw") or 0 for it in b["picked"]))
    return out


def collect_status(conn, window: dict) -> tuple[list[dict], list[str]]:
    """이 시간대로 실행한 수집·마스터 갱신 기록(소스별 최신)과 경고."""
    rows = conn.execute(
        # 수집과 매칭은 각자 '지금'을 끝 시각으로 잡아 몇 초 차이가 난다 → 같은 시작, 끝이 매칭 시각 이전인 최신 기록
        "SELECT source, MAX(id) AS id FROM collect_runs WHERE window_start=? AND window_end<=? "
        "AND source NOT IN ('kiwoom_stocks','kiwoom_themes','naver_themes') GROUP BY source",
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


SOURCE_NAMES = {"naver_focus_401": "네이버 시황·전망", "korea_policy": "정책브리핑 정책뉴스",
                "ministry_press": "부처 보도자료(11개 부처)", "dart": "DART 공시",
                "kiwoom_stocks": "종목 마스터(키움)", "kiwoom_themes": "테마(키움)", "naver_themes": "테마(네이버)"}


def past_gap_by_category(conn, min_n: int = 3) -> dict[str, tuple[float, int]]:
    """재료 유형별 과거 D0 평균 시초가 갭(KRX 기준가 대비) — 저녁 '이미 반영 정도' 비교용. 표본 min_n건 이상만."""
    rows = conn.execute("SELECT s.category, p.gap_pct FROM price_reactions p JOIN scores s ON s.date=p.date AND "
                        "s.stock_code=p.stock_code WHERE p.grp='scored' AND p.sentiment='호재' AND p.gap_pct IS NOT NULL")
    acc: dict[str, list[float]] = {}
    for cat, g in rows:
        acc.setdefault(cat or "other", []).append(g)
    return {k: (sum(v) / len(v), len(v)) for k, v in acc.items() if len(v) >= min_n}


def evening_section(conn, good: list[dict]) -> list[str]:
    """저녁 종가매매 후보표: 오늘 KRX 종가·지금 NXT 가격·시간외 등락·공시 후 등락·거래대금·NXT 가능 여부."""
    from .analysis.categories import label
    past = past_gap_by_category(conn)
    L = ["## 종가매매 후보 (시간외 시세)", ""]
    if not good:
        return L + ["해당 없음", ""]
    asof = next((it["evening"]["as_of"] for it in good if it.get("evening")), "-")
    L += [f"시세 기준 {asof} (NXT 애프터마켓, 20:00 마감). **NXT 불가 종목은 애프터마켓에서 살 수 없음.**", "",
          "| 순위 | 종목 | 강도 | 근거 | KRX 종가 | 지금 | 시간외 | 공시 후 | 시간외 거래대금 | NXT | 선반영 | 과거 같은 유형 D0 갭 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, it in enumerate(good, 1):
        q = it.get("evening") or {}
        pp = ((it.get("factors") or {}).get("preprice") or {}).get("score", "-")
        cat = it.get("category") or "other"
        pg = past.get(cat)
        cmp = "-"
        if pg and q.get("after_hours_pct") is not None:
            diff = q["after_hours_pct"] - pg[0] * 100
            cmp = f"{label(cat)} 평균 {pg[0] * 100:+.1f}%({pg[1]}건) → 지금 {'덜' if diff < 0 else '더'} 오름"
        pct = lambda v: "-" if v is None else f"{v:+.1f}%"  # noqa: E731
        won = lambda v: "-" if not v else f"{v:,.0f}"  # noqa: E731
        nxt = "가능(체결 있음)" if q.get("nxt_traded") else {True: "가능", False: "**불가**"}.get(q.get("nxt_enabled"), "-")
        L.append(f"| {n} | **{it['name']}** ({it['code']}) | {'-' if it.get('final') is None else str(it['final']) + '점'} | {'·'.join(basis_labels(conn, it)) or '-'} | "
                 f"{won(q.get('krx_close'))} | {won(q.get('price'))} | {pct(q.get('after_hours_pct'))} | "
                 f"{pct(q.get('since_disclosure_pct'))} | {(q.get('after_value') or 0) / 1e8:,.1f}억 | {nxt} | {pp} | {cmp} |")
    L += ["", "<sub>시간외 = 지금 NXT 가격 / 오늘 KRX 종가 − 1. 공시 후 = 공시 접수 직전 1분봉 대비. "
          "과거 D0 갭 = 이 시스템이 기록한 같은 재료 유형의 다음날 시초가 갭 평균(표본 3건 이상일 때만). "
          "재료 강도 점수이며 매수 추천이 아님.</sub>", ""]
    return L


def _names_line(ms: list[dict], nxt: bool = False) -> str:
    out = []
    for m in ms:
        t = m.get("name") or m["code"]
        if m.get("change_pct") is not None:
            t += f" {m['change_pct']:+.1f}%"
        if m.get("limit_hit") and m.get("closed_at_limit"):
            t += "↑"
        if nxt and m.get("nxt") is False:
            t += "(NXT✕?)"
        out.append(t)
    return ", ".join(out) or "-"


def theme_outlook_section(tf: dict | None) -> list[str]:
    """'테마 전망' — 15:35 테마 집계(묶음 단위) + 재료로 만든 객관 예측.
    저녁(1차): 오늘 주도 테마 + 내일 주목 후보 · 아침(최종): 어제 주도 테마 + 오늘 주목 후보(밤사이 재료 반영)."""
    if not tf or not tf.get("prev_day"):
        return []
    ev = tf.get("phase") == "evening"
    day_word, next_word = ("오늘", f"내일({tf['date'][5:]})") if ev else ("어제", "오늘")
    title = (f"## {next_word} 테마 전망 — 1차 (오늘 장 마감 집계 + 15:30 이후 재료, 아침에 최종 갱신)" if ev
             else f"## 테마 전망 — 최종 (전일 {tf['prev_day']} 집계 + 밤사이 재료)")
    L = [title, ""]
    if any(it.get("approx") for it in tf.get("leaders_yday", [])):
        L.append(f"<sub>※ {day_word} 지표는 통합 일봉으로 근사 재구성한 값(시간외 포함)입니다.</sub>")
        L.append("")
    L.append(f"**{day_word} 주도 테마** (중앙값 초과 순, 비슷한 묶음은 관련 묶음으로)")
    L.append("")
    L.append("| 테마 | 중앙값/평균 초과 | 상승 비율·+5% | 단계 | 대장 | 2등주 | 후발주 | 거래대금 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for it in tf.get("leaders_yday", []):
        name = _md(it["name"]) + (f"<br><sub>관련: {_md(', '.join(it['related'][:3]))}</sub>" if it.get("related") else "")
        stage = f"{it['stage']}({it['streak']}일)" if it.get("stage") else "-"
        if it.get("leader_changed"):
            stage += " · 대장 교체"
        tv = f"{it['tv_ratio20']:.1f}배" if it.get("tv_ratio20") else "-"
        L.append(f"| {name} | {it['median_excess']:+.1f} / {it['avg_excess']:+.1f}%p | "
                 f"{round((it['rise_ratio'] or 0) * 100)}% · {it['up5']} | {stage} | {_md(_names_line(it['leader'], True))} | "
                 f"{_md(_names_line(it['seconds'], True))} | {_md(_names_line(it['followers']))} | {tv} |")
    L.append("")
    if tf.get("small_yday"):
        L.append("- 소형 테마(5종목 미만) 급등: " + ", ".join(f"{_md(x['name'])} {x['median_excess']:+.1f}%p" for x in tf["small_yday"]))
    h = tf.get("hedge") or {}
    if h.get("items"):
        tag = " (하락일 표본 부족 — 표시만)" if h.get("display_only") else ""
        L.append(f"- 헤지 테마 (최근 지수 하락일 {h['down_days']}일 누적 초과){tag}: " +
                 ", ".join(f"{_md(x['name'])} 초과 {x['cum_excess']:+.1f}%p·등락 {x.get('cum_pct', 0):+.1f}%(대장 {_md(x.get('leader_name') or '-')})"
                         for x in h["items"]))
    L.append("")
    L.append(f"**{next_word} 주목 테마 후보** (예측 — 대상일 15:35에 실제 결과로 채점)")
    L.append("")
    L.append("| 순위 | 테마 | 근거 | 신뢰도 | 대장 후보 | 2등주 후보 |")
    L.append("|---|---|---|---|---|---|")
    for i, it in enumerate(tf.get("forecast", []), 1):
        name = _md(it["name"]) + (f"<br><sub>관련: {_md(', '.join(it['related'][:3]))}</sub>" if it.get("related") else "")
        nx = it.get("nxt") or {}
        ld = (it.get("leader_name") or "-") + (" (NXT✕?)" if it.get("leader_code") and nx.get(it["leader_code"]) is False else "")
        sec = ", ".join((n or c) + (" (NXT✕?)" if nx.get(c) is False else "")
                        for c, n in zip(it.get("seconds", []), it.get("second_names", []))) or "-"
        L.append(f"| {i} | {name} | {_md('; '.join(it.get('basis', [])) or '-')} | {it['confidence']} ({it['score']:.2f}) | "
                 f"{_md(ld)} | {_md(sec)} |")
    L.append("")
    tr = tf.get("track") or {}
    f, b = tr.get("forecast") or {}, tr.get("baseline") or {}
    if f.get("days"):
        def pct(v):
            return "-" if v is None else f"{v * 100:.0f}%"
        L.append(f"<sub>예측 성적(최근 {f['days']}일, 표본이 적으면 방향 확인용): 상위10 적중 {pct(f['top10_ratio'])} · "
                 f"평균 초과 {f['avg_excess'] or 0:+.2f}%p · 대장 적중 {pct(f['leader_ratio'])} │ "
                 f"기준선(어제 상위 유지) 상위10 {pct(b.get('top10_ratio'))} · 평균 초과 {b.get('avg_excess') or 0:+.2f}%p</sub>")
    else:
        L.append("<sub>예측 성적: 아직 채점 기록 없음(첫 15:35 집계 뒤부터 쌓임). 표본이 쌓이기 전에는 방향 확인용.</sub>")
    L.append("<sub>점수 = 단계·폭·거래대금 증가·밤사이 재료(정책·기사·공시)의 같은 가중 평균. 신뢰도 = 0.5 이상인 근거 수. "
             "NXT✕? = 종목 마스터상 NXT 거래 불가(실제로 거래되는 경우가 있어 확인 필요).</sub>")
    L.append("")
    return L


def render(conn, scored: dict, candidates: dict) -> str:
    date = scored["date"]
    w = scored["window"]
    items = scored["items"]
    good = [i for i in items if i["sentiment"] == "호재"]
    bad = [i for i in items if i["sentiment"] == "악재"]
    runs, warnings = collect_status(conn, w)
    c = candidates.get("counts", {})
    L: list[str] = []
    evening = scored.get("session") == "evening"

    L.append(f"# {date} 저녁 종가매매 리포트" if evening else f"# {date} 장전 재료 리포트")
    L.append("")
    if scored.get("fallback"):
        L.append("> ⚠️ **Claude 채점이 시간 안에 끝나지 않아 객관 지표(공시 규모·선반영·시간외 반응)만으로 만든 간이 리포트입니다.** "
                 "점수 칸은 공시 규모 점수입니다.")
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

    if evening:
        L += evening_section(conn, good)
    # ------------------------------------------------------------ 체크리스트
    L.append("## 장 시작 전 체크리스트 (직접 기입)" if not evening else "## 내일 장 시작 전 체크리스트 (직접 기입)")
    L.append("")
    L.append("| 항목 | 확인 내용 | 메모 |")
    L.append("|---|---|---|")
    if evening:
        L.append("| 오늘 마감 | 코스피·코스닥 종가, 외국인·기관·개인 순매수 |  |")
        L.append("| 시간외 분위기 | NXT 거래대금 상위·급등 종목, 위 후보의 호가 잔량 |  |")
        L.append("| 해외 | 美 선물·환율 방향 |  |")
        L.append("| 내일 일정 | 실적 발표·경제지표·만기 등 |  |")
        L.append("| 오늘 저녁 판단 | 매수 / 관망 (매수 시 가격·비중) |  |")
    else:
        L.append("| 지수 흐름 | 美 증시 마감 / 코스피·코스닥 선물 / 환율 |  |")
        L.append("| 장초반 매수세 | 09:00~09:10 지수 방향, 거래대금 상위 업종 |  |")
        L.append("| 외국인 수급 | 선물·현물 순매수 방향 |  |")
        L.append("| 기관 수급 | 순매수 방향, 프로그램 매매 |  |")
        L.append("| 개인 수급 | 순매수 방향 |  |")
        L.append("| 오늘의 판단 | 공격 / 중립 / 관망 |  |")
    L.append("")

    L += theme_outlook_section(candidates.get("theme_forecast"))
    # ------------------------------------------------------------ 순위표
    L.append("## 호재 종목 (점수 높은 순)")
    L.append("")
    if good:
        L.append("| 순위 | 종목 | 점수 | 근거 | 재료 | 시초가 | 장초반 매수세 | 메모 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for rank, it in enumerate(good, 1):
            tags = "·".join(basis_labels(conn, it)) or "-"
            sc = "-" if it.get("final") is None else f"{it['final']}점 ({it['final_raw']})"
            L.append(f"| {rank} | **{it['name']}** ({it['code']}) | {sc} | {tags} | "
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

    # ------------------------------------------------------------ 테마 묶음
    bundles = theme_bundles(scored, candidates)
    if bundles:
        L.append(f"## 테마 묶음 ({len(bundles)})")
        L.append("")
        L.append("| 테마 | 결집도 | 대장 후보(연관도 1·2위) | 고른 종목 | 안 고른 상위 종목 |")
        L.append("|---|---|---|---|---|")
        for b in bundles:
            picked = {it["code"] for it in b["picked"]}
            lead = ", ".join(f"{m['rank']}위 {m['name']}" + (" ✔" if m["code"] in picked else "") for m in b["leaders"]) or "-"
            chosen = ", ".join(f"{it['name']}({it['final']}점)" for it in b["picked"])
            others = ", ".join(m["name"] for m in b["others"]) or "-"
            cl = (b["cluster"] or {}).get("score", "-")
            L.append(f"| {_md(b['theme'])} | {cl}점 | {_md(lead)} | {_md(chosen)} | {_md(others)} |")
        L.append("")
        L.append("<sub>대장 후보 = 재료 본문 핵심 단어와 테마 편입 사유가 겹치는 정도(드문 단어 가중) 순위. ✔ = 채점한 종목. "
                 "안 고른 상위 종목은 비교 종목으로 주가 반응을 기록한다.</sub>")
        L.append("")

    # ------------------------------------------------------------ 상세
    L.append("## 종목별 상세")
    L.append("")
    for rank, it in enumerate(good, 1):
        tags = " ".join(f"[{t}]" for t in basis_labels(conn, it))
        sc = "점수 없음(간이)" if it.get("final") is None else f"{it['final']}점 {STARS.get(it['final'], '')}"
        L.append(f"### {rank}. {it['name']} ({it['code']}) — {sc} {tags}")
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
            if g.get("intraday"):
                title += " ⏱장중 게시"
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
    press = next((r for r in runs if r["source"] == "ministry_press" and r.get("detail")), None)
    if press:
        per = json.loads(press["detail"])
        L.append("부처 보도자료(시간대 안): " + " · ".join(
            f"{v['name']} {v['in_window']}" + (" ⚠️" if v.get("error") else "") for v in per.values()))
        L.append("")
    L.append(f"<sub>생성: scored.json · candidates.json ({date}) / DB data/stock_news.db</sub>")
    return "\n".join(L) + "\n"
