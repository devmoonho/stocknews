"""수집 결과 → 종목별 근거 묶음(candidates.json). Claude가 채점할 입력.

종목 후보가 되는 경우
  1) 기사 제목/본문에 종목명(별칭)이 직접 나옴
  2) 시간대 안에 그 종목의 공시가 있음
테마 신호(theme_signals): 기사에 테마 키워드가 나오면 테마 단위로 묶고 구성 종목(후보만) 목록과 편입 사유를 붙인다.
테마만으로는 종목을 후보에 넣지 않는다 — 어떤 구성 종목이 실제 수혜인지는 Claude가 고른다.

객관 지표(결정적으로 계산, Claude는 그대로 사용):
  - diffusion.score : 확산도(언론사 수) 1~5
  - scale.score     : 재료 규모(공시 수치) 1~5, 수치가 없으면 null
                      공급계약은 매출(연환산)·시총 대비 중 높은 쪽, 제3자배정 유증은 같은 종목 유입액 합계의 시총 대비
  - preprice.score  : 선반영(직전 5·20일 상승률, 투자주의 등) 1~5, 일봉이 없으면 null — prices.py
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .. import prices
from ..timewindow import Window, is_trading_day, load_holidays
from . import followup, leader as ldr
from .disclosure_parse import _by_mcap, is_third_party, parse as parse_disclosure, scale_basis, scale_score
from .matching import StockMatcher, ThemeMatcher, policy_terms, snippet

PRIOR_DAYS = 7               # 신규/기존 재료 판단용으로 과거 며칠을 볼지
MAX_ARTICLES_PER_STOCK = 8
MAX_THEME_MEMBERS = 40


def diffusion_score(offices: int, articles: int) -> int:
    if offices >= 5:
        return 5
    if offices >= 3:
        return 4
    if offices == 2 or articles >= 3:
        return 3
    if articles >= 1:
        return 2
    return 1          # 공시만 있고 기사 없음


def _trading_days(start: str, end: str) -> int:
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    hol = load_holidays()
    return sum(is_trading_day(d0 + dt.timedelta(days=i), hol) for i in range((d1 - d0).days + 1))


def build(conn, window: Window, write_followups: bool = False) -> dict:
    s, e = f"{window.start:%Y-%m-%d %H:%M:%S}", f"{window.end:%Y-%m-%d %H:%M:%S}"
    asof = f"{window.start:%Y-%m-%d}"          # 재료 직전 거래일 — 가격은 이날 종가까지만
    sm, tm = StockMatcher.from_db(conn), ThemeMatcher.from_db(conn)
    stocks = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, market, sector, audit_info, order_warning, is_excluded, exclude_reason, raw FROM stocks")}
    theme_info = {f"{r['source']}:{r['theme_code']}": dict(r) for r in conn.execute(
        "SELECT source, theme_code, name, description FROM themes")}
    stock_themes: dict[str, list[str]] = defaultdict(list)
    members: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT source, theme_code, stock_code, stock_name, reason FROM theme_stocks"):
        key = f"{r['source']}:{r['theme_code']}"
        stock_themes[r["stock_code"]].append(key)
        members[key].append(dict(r))
    pol = policy_terms()

    # ---------------------------------------------------------------- 기사
    articles = [dict(r) for r in conn.execute(
        "SELECT id, source, office_name, title, summary, body, published_at, url FROM articles "
        "WHERE published_at BETWEEN ? AND ? ORDER BY published_at", (s, e))]
    per_stock: dict[str, dict] = defaultdict(lambda: {"articles": [], "disclosures": []})
    theme_hits: dict[str, dict] = defaultdict(lambda: {"articles": {}, "keywords": set()})
    policy_articles = []
    gov_articles = []

    def theme_name(key: str) -> str | None:
        info = theme_info.get(key)
        return info["name"] if info else None
    for a in articles:
        title, text = a["title"], a["body"] or a["summary"] or ""
        full = f"{title}\n{text}"
        seen_codes: dict[str, tuple] = {}
        hit_count: dict[str, int] = defaultdict(int)
        for h in sm.find(full):          # 경계 규칙을 통과한 매칭만 센다('에코프로비엠' 안의 '에코프로'는 제외)
            hit_count[h.key] += 1
            if h.key not in seen_codes:
                seen_codes[h.key] = (h.term, h.start)
        for code, (term, pos) in seen_codes.items():
            per_stock[code]["articles"].append({
                "id": a["id"], "title": title, "office": a["office_name"], "published_at": a["published_at"],
                "url": a["url"], "in_title": pos < len(title), "matched": term,
                "count_in_text": hit_count[code], "snippet": snippet(full, pos)})
        is_gov = a["source"] == "korea_policy"
        art_themes: dict[str, str] = {}
        head_len = len(title) + 1
        gov_head = f"{title}\n{a['summary'] or ''}" if is_gov else ""
        for h in tm.find(full):
            # 정부 정책뉴스는 본문이 길어 일반 테마가 많이 걸린다 → 제목·요약에 있거나 본문에 2번 이상 나온 키워드만
            if is_gov and h.term not in gov_head and full.count(h.term) < 3:
                continue
            th = theme_hits[h.key]
            th["keywords"].add(h.term)
            th["articles"].setdefault(a["id"], {"id": a["id"], "title": title, "office": a["office_name"],
                                                "gov": is_gov, "in_title": h.start < len(title),
                                                "snippet": snippet(full, h.start, 80)})
            art_themes.setdefault(h.key, h.term)
        if is_gov:
            # 정부 정책뉴스: 키워드와 상관없이 전부 정책 재료 후보. 걸린 테마·종목을 같이 보여준다.
            gov_articles.append({
                "id": a["id"], "title": title, "dept": a["office_name"], "published_at": a["published_at"],
                "url": a["url"], "summary": (a["summary"] or "")[:400], "_theme_keys": list(art_themes),
                "themes": sorted({theme_name(k) for k in art_themes if theme_name(k)}),
                # 확산도 키워드: 제목 + 요약 첫 문단(부제)에 나온 테마 키워드
                "_title_terms": sorted({t for t in art_themes.values()
                                        if t in title or t in (a["summary"] or "")[:200]}),
                "stocks": [{"code": c, "name": stocks[c]["name"]} for c in seen_codes
                           if c in stocks and not stocks[c]["is_excluded"]]})
            continue
        hit_pol = [t for t in pol if t in full]
        if len(hit_pol) >= 2 or any(t in title for t in pol):
            policy_articles.append({"id": a["id"], "title": title, "office": a["office_name"],
                                    "published_at": a["published_at"], "url": a["url"], "policy_terms": hit_pol[:6]})

    # 정부 정책뉴스의 확산도: 정책뉴스 제목에 나온 테마 키워드를 '제목에' 다룬 언론 기사 수 (+ 정책브리핑 1)
    news = [a for a in articles if a["source"] != "korea_policy"]
    for g in gov_articles:
        # 'AI'·'5G'처럼 짧은 영문 약어는 시황 기사 제목에도 흔해 확산도를 부풀린다 → 제외
        terms = [t for t in g.pop("_title_terms") if not (t.isascii() and len(t) <= 2)]
        rel = [a for a in news if terms and any(t in a["title"] for t in terms)]
        offices = {a["office_name"] for a in rel} | {"정책브리핑"}
        g["diffusion"] = {"keywords": terms, "related_articles": len(rel), "offices": len(offices),
                          "score": diffusion_score(len(offices), len(rel) + 1),
                          "related_titles": [a["title"] for a in rel[:3]]}

    # ---------------------------------------------------------------- 공시
    discs = [dict(r) for r in conn.execute(
        "SELECT * FROM disclosures WHERE stock_code IS NOT NULL AND ("
        "received_at BETWEEN ? AND ? OR (received_at IS NULL AND rcept_dt > ? AND rcept_dt <= ?))",
        (s, e, f"{window.start:%Y%m%d}", f"{window.end:%Y%m%d}"))]
    routine = 0
    for d in discs:
        if not (d["body"] or d["confirmed"] or d["signal"] != "neutral"):
            routine += 1                     # 소유상황·대량보유·IR 개최 등 일상 공시는 후보 근거에서 제외
            continue
        mcap = prices.market_cap(conn, d["stock_code"], stocks.get(d["stock_code"], {}).get("raw"), asof)
        parsed = parse_disclosure(d["report_nm"], d["body"], mcap)
        per_stock[d["stock_code"]]["disclosures"].append({
            "rcept_no": d["rcept_no"], "report_nm": d["report_nm"], "received_at": d["received_at"] or d["rcept_dt"],
            "signal": d["signal"], "confirmed": bool(d["confirmed"]), "url": d["url"],
            # 정정공시는 변경 후 '총액'이 나오므로 자동 규모 점수를 쓰지 않는다 → 늘어난 금액 기준으로 Claude가 판단
            "figures": parsed,
            "scale_score": None if (parsed or {}).get("is_correction") else scale_score(parsed),
            "scale_basis": None if (parsed or {}).get("is_correction") else scale_basis(parsed),
            "body_excerpt": (d["body"] or "")[:1200] or None})

    # ---------------------------------------------------------------- 과거 언급(신규/기존 재료 판단 보조)
    prior_s = f"{window.start - dt.timedelta(days=PRIOR_DAYS):%Y-%m-%d %H:%M:%S}"
    prior_titles = conn.execute("SELECT title, published_at FROM articles WHERE published_at >= ? AND published_at < ?",
                                (prior_s, s)).fetchall()
    prior_mentions: dict[str, list] = defaultdict(list)
    for t, p in prior_titles:
        for h in {h.key for h in sm.find(t)}:
            prior_mentions[h].append({"title": t, "published_at": p})
    prior_disc = defaultdict(list)
    for r in conn.execute("SELECT stock_code, report_nm, rcept_dt FROM disclosures WHERE stock_code IS NOT NULL "
                          "AND rcept_dt >= ? AND rcept_dt < ?",
                          (f"{window.start - dt.timedelta(days=30):%Y%m%d}", f"{window.start:%Y%m%d}")):
        prior_disc[r[0]].append({"report_nm": r[1], "rcept_dt": r[2]})
    history_from = conn.execute("SELECT MIN(published_at) FROM articles").fetchone()[0]

    # ---------------------------------------------------------------- 종목별 묶음
    out_stocks, excluded = [], []
    for code, ev in per_stock.items():
        info = stocks.get(code)
        if info is None:
            continue
        if info["is_excluded"]:
            excluded.append({"code": code, "name": info["name"], "reason": info["exclude_reason"],
                             "articles": len(ev["articles"]), "disclosures": len(ev["disclosures"])})
            continue
        arts = sorted(ev["articles"], key=lambda x: (not x["in_title"], -x["count_in_text"]))
        # 확산도는 '그 종목을 다룬 기사'만 센다: 제목에 나오거나 본문에 2번 이상. 시황 기사 속 한 번 스친 언급은 제외.
        subst = [x for x in arts if x["in_title"] or x["count_in_text"] >= 2]
        offices = {x["office"] for x in subst}
        title_hits = sum(x["in_title"] for x in arts)
        scales = [d["scale_score"] for d in ev["disclosures"]
                  if d["scale_score"] is not None and not is_third_party(d["figures"])]
        basis = [d["scale_basis"] for d in ev["disclosures"]
                 if d["scale_score"] is not None and not is_third_party(d["figures"]) and d["scale_score"] == max(scales)]
        # 제3자배정 유증(경영권 변경과 같이 나오는 경우가 많음): 같은 종목의 유입액을 합쳐 시총 대비로 한 번만 점수
        third = [d["figures"] for d in ev["disclosures"] if is_third_party(d["figures"]) and not d["figures"]["is_correction"]
                 and d["figures"].get("amount")]
        mcap = prices.market_cap(conn, code, info["raw"], asof)
        inflow = None
        if third and mcap:
            total = sum(f["amount"] for f in third)
            ratio = round(total / mcap * 100, 2)
            inflow = {"amount": total, "market_cap_ratio_pct": ratio, "issues": len(third), "score": _by_mcap(ratio)}
            if not scales or inflow["score"] > max(scales):
                basis = [f"제3자배정 유증 {len(third)}건 회사 유입 {total / 1e8:,.1f}억 = 시총 대비 {ratio}%"]
            scales.append(inflow["score"])
        ctx = prices.context(conn, code, asof, info["raw"])
        for d in ev["disclosures"]:                 # 자사주: 매수 기간의 하루 평균 매수 가능액 → 거래대금 대비
            f = d["figures"] or {}
            if f.get("kind") == "자사주취득" and f.get("amount") and f.get("start") and f.get("end"):
                days = _trading_days(f["start"], f["end"])
                if days:
                    f["trading_days"] = days
                    f["daily_amount"] = round(f["amount"] / days)
                    if ctx and ctx.get("avg_value20"):
                        f["daily_vs_value_pct"] = round(f["daily_amount"] / ctx["avg_value20"] * 100, 1)
        themes = sorted(stock_themes.get(code, []), key=lambda k: not k.startswith("naver"))
        out_stocks.append({
            "code": code, "name": info["name"], "market": info["market"], "sector": info["sector"],
            "warning": None if info["audit_info"] in (None, "정상") else info["audit_info"],
            "price": {**(ctx or {}), "market_cap": mcap},
            "themes": [theme_info[k]["name"] for k in themes if k in theme_info][:8],
            "objective": {
                "diffusion": {"articles": len(subst), "offices": len(offices), "title_mentions": title_hits,
                              "passing_mentions": len(arts) - len(subst),
                              # 공시만 있고 그 종목을 다룬 기사가 없으면 확산도는 채점에서 뺀다(null) —
                              # 밤사이 공시는 아침까지 기사가 안 나오는 게 보통이라 감점 사유가 아님 (2026-11-12 채점표 변경)
                              "disclosure_only": bool(ev["disclosures"]) and not subst,
                              "score": None if (ev["disclosures"] and not subst)
                              else diffusion_score(len(offices), len(subst))},
                "scale_score": max(scales) if scales else None,
                "scale_basis": basis[0] if scales and basis else None,
                "third_party_inflow": inflow,
                "preprice": prices.preprice(ctx, info["audit_info"], info["order_warning"]),
            },
            "prior": {"articles_7d": len(prior_mentions.get(code, [])),
                      "article_titles_7d": [p["title"] for p in prior_mentions.get(code, [])][:5],
                      "disclosures_30d": prior_disc.get(code, [])[:8]},
            "disclosures": ev["disclosures"],
            "articles": arts[:MAX_ARTICLES_PER_STOCK],
            "articles_omitted": max(0, len(arts) - MAX_ARTICLES_PER_STOCK),
        })
    # 근거가 강한 순(공시 확정재료·제목 언급·언론사 수)으로 정렬 — 채점 순서 참고용일 뿐 점수 아님
    out_stocks.sort(key=lambda x: (
        -max([3 if d["confirmed"] else 1 for d in x["disclosures"]] or [0]),
        -x["objective"]["diffusion"]["title_mentions"], -x["objective"]["diffusion"]["offices"]))

    # ---------------------------------------------------------------- 테마 신호
    candidate_codes = {c for c, i in stocks.items() if not i["is_excluded"]}
    # 같은 키워드·같은 기사로 걸린 테마(예: 네이버 '증권'과 키움 '증권')는 하나로 합친다
    grouped: dict[tuple, dict] = {}
    for key, th in theme_hits.items():
        info = theme_info.get(key)
        if not info:
            continue
        arts = list(th["articles"].values())
        in_title = sum(a["in_title"] for a in arts)
        gov = sum(a["gov"] for a in arts)
        if in_title == 0 and len(arts) < 3 and not gov:
            continue                       # 제목에 없고 본문에서 두어 번 스친 테마는 생략(정부 정책뉴스는 예외)
        gk = (tuple(sorted(th["keywords"])), tuple(sorted(th["articles"])))
        g = grouped.setdefault(gk, {"themes": [], "keys": [], "arts": arts, "in_title": in_title, "gov": gov})
        g["themes"].append(info["name"])
        g["keys"].append(key)
    signals = []
    for (kws, _), g in grouped.items():
        mem: dict[str, dict] = {}
        desc = None
        for key in sorted(g["keys"], key=lambda k: not k.startswith("naver")):
            desc = desc or theme_info[key]["description"]
            for m in members.get(key, []):
                if m["stock_code"] in candidate_codes and m["stock_code"] not in mem:
                    mem[m["stock_code"]] = {"code": m["stock_code"], "name": m["stock_name"],
                                            "reason": (m["reason"] or "")[:160] or None}
        arts = g["arts"]
        signals.append({"themes": g["themes"], "keywords": list(kws), "_keys": g["keys"], "_arts": arts,
                        "description": (desc or "")[:300] or None,
                        "articles": len(arts), "title_mentions": g["in_title"], "gov_articles": g["gov"],
                        "offices": len({a["office"] for a in arts}),
                        "sample_articles": sorted(arts, key=lambda a: (not a["gov"], not a["in_title"]))[:5],
                        "members": list(mem.values())[:MAX_THEME_MEMBERS], "members_total": len(mem)})
    signals.sort(key=lambda x: (-(x["gov_articles"] > 0), -x["title_mentions"], -x["offices"], -x["articles"]))

    # ---------------------------------------------------------------- 테마 결집도 · 대장 후보 (leader.py)
    rel = ldr.Relevance([r[0] for r in conn.execute("SELECT reason FROM theme_stocks WHERE reason IS NOT NULL")])
    art_by_id = {a["id"]: a for a in articles}
    with_material = {x["code"] for x in out_stocks
                     if x["disclosures"] or x["objective"]["diffusion"]["title_mentions"]}
    cap_cache: dict[str, float | None] = {}

    def member_list(keys: list[str]) -> list[dict]:
        seen: dict[str, dict] = {}
        for key in sorted(keys, key=lambda k: not k.startswith("naver")):      # 네이버 편입 사유 우선
            for m in members.get(key, []):
                c = m["stock_code"]
                if c not in candidate_codes:
                    continue
                if c not in seen or (not seen[c]["reason"] and m["reason"]):
                    if c not in cap_cache:
                        cap_cache[c] = prices.market_cap(conn, c, stocks[c]["raw"], asof)
                    seen[c] = {"code": c, "name": m["stock_name"], "reason": m["reason"] or None,   # 점수는 전문으로
                               "n_themes": len(stock_themes.get(c, [])), "market_cap": cap_cache[c]}
        return list(seen.values())

    def gov_parts(a: dict) -> tuple[str, str]:
        full = art_by_id.get(a["id"]) or {}
        return f"{full.get('title', '')}\n{full.get('summary') or ''}", full.get("body") or ""

    for sig in signals:
        arts = sig.pop("_arts")
        sig["_art_ids"] = {a["id"] for a in arts}
        head = "\n".join(gov_parts(a)[0] if a.get("gov") else a["title"] for a in arts)
        body = "\n".join(gov_parts(a)[1] if a.get("gov") else a.get("snippet", "") for a in arts)
        ranked = ldr.rank_members(rel, head, body, member_list(sig["_keys"]))
        sig["members"] = [{**{k: m[k] for k in ("code", "name", "rank", "relevance", "matched")},
                           "reason": (m["reason"] or "")[:160] or None} for m in ranked[:MAX_THEME_MEMBERS]]
        sig["members_total"] = len(ranked)
        sig["leader_rank"] = {m["code"]: m["rank"] for m in ranked}
        mat_n = sum(m["code"] in with_material for m in ranked)
        pts = ldr.cluster_points(sig["gov_articles"], sig["title_mentions"], sig["offices"], mat_n)
        sig["cluster"] = {"score": ldr.cluster_score(pts), "points": pts,
                          "reason": f"정부 {sig['gov_articles']}×2 · 제목기사 {sig['title_mentions']} · 언론사 {sig['offices']} · "
                                    f"재료 있는 구성종목 {mat_n} = {pts} — 객관 점수"}
    for g in gov_articles:
        keys = set(g.pop("_theme_keys"))
        linked = [sig for sig in signals if keys & set(sig["_keys"])]
        g["cluster"] = max((sig["cluster"] for sig in linked), key=lambda c: c["points"], default=None)
        head, body = gov_parts(g)
        g["leaders"], g["leader_rank"] = [], {}
        for sig in linked:                       # 정책 본문으로 테마별 순위를 다시 매긴다
            ranked = ldr.rank_members(rel, head, body, member_list(sig["_keys"]))
            theme = " / ".join(sig["themes"])
            g["leaders"].append({"theme": theme, "members_total": len(ranked), "cluster": sig["cluster"],
                                 "top": [{k: m[k] for k in ("code", "name", "rank", "relevance", "matched")}
                                         for m in ranked[:12] if m["rank"]]})
            for m in ranked:                     # 종목마다 가장 높은 순위가 나온 테마(그 테마의 결집도를 쓴다)
                cur = g["leader_rank"].get(m["code"])
                if cur is None or (m["rank"] or 999) < (cur["rank"] or 999):
                    g["leader_rank"][m["code"]] = {"rank": m["rank"], "theme": theme, "cluster": sig["cluster"]}
    for x in out_stocks:
        # 종목 근거(S-): 그 종목이 속한 테마 신호 중, 그 종목을 다룬 기사가 신호 기사에 들어 있는 것만(가장 높은 결집도).
        # 공시만 있는 종목이 우연히 속한 일반 테마(예: 'AI')의 결집도를 받지 않게.
        mine = set(stock_themes.get(x["code"], []))
        own = {a["id"] for a in x["articles"]}
        best = max((sig["cluster"] for sig in signals if set(sig["_keys"]) & mine and own & sig["_art_ids"]),
                   key=lambda c: c["points"], default=None)
        x["objective"]["theme_cluster"] = best
    for sig in signals:
        sig.pop("_keys")
        sig.pop("_art_ids")

    fu = followup.scan(conn, window, write=write_followups)

    return {
        "window": {"start": s, "end": e},
        "generated_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "history_available_from": history_from,
        "counts": {"articles": len(articles), "disclosures_listed": len(discs), "disclosures_routine_skipped": routine,
                   "stocks": len(out_stocks),
                   "theme_signals": len(signals), "gov_policy": len(gov_articles),
                   "policy_articles": len(policy_articles),
                   "excluded_mentions": len(excluded)},
        "stocks": out_stocks,
        "theme_signals": signals,
        "gov_policy": gov_articles,
        "policy_articles": policy_articles,
        "excluded_mentions": excluded,
        "followups": fu["events"],            # 다이제스트 G — [F-번호:코드]
        "followups_pending": fu["pending"],
    }
