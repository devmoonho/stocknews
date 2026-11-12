"""candidates.json → candidates_digest.md : Claude가 한 번에 읽을 수 있는 압축본.

전체 근거는 candidates.json에 있고, 다이제스트는 채점에 필요한 것만 짧게 보여준다.
각 종목에 [S-코드] 식별자를 붙여 scores.json과 대응시킨다.
"""
from __future__ import annotations


def _won(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if abs(v) >= 1e12:
        return f"{v / 1e12:.2f}조"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.0f}억"
    return f"{v:,.0f}원"


def _figures(f: dict | None) -> str:
    if not f:
        return ""
    parts = [f.get("kind", "")]
    if f.get("is_correction"):
        parts.append("정정공시")
    if f.get("amount") is not None:
        parts.append(f"금액 {_won(f['amount'])}")
    if f.get("revenue_ratio_pct") is not None:
        parts.append(f"매출대비 {f['revenue_ratio_pct']}%")
    if f.get("market_cap_ratio_pct") is not None:
        parts.append(f"시총대비 {f['market_cap_ratio_pct']}%")
    if f.get("counterparty"):
        parts.append(f"상대 {f['counterparty']}")
    if f.get("method"):
        parts.append(f"방식 {f['method']}")
    if f.get("note"):
        parts.append(f["note"])
    return " · ".join(p for p in parts if p)


def render(c: dict) -> str:
    L: list[str] = []
    w = c["window"]
    L.append(f"# 후보 다이제스트 {w['start']} ~ {w['end']}")
    n = c["counts"]
    L.append(f"기사 {n['articles']} · 공시 {n['disclosures_listed']}(일상공시 {n.get('disclosures_routine_skipped', 0)} 제외) "
             f"· 후보 {n['stocks']} · 테마신호 {n['theme_signals']} · 정책기사 {n['policy_articles']}")
    L.append(f"과거 기사 이력 시작: {c.get('history_available_from')} (이보다 이전 이력은 없으므로 prior=0이 곧 '신규'를 뜻하진 않음)\n")

    disc = [s for s in c["stocks"] if s["disclosures"]]
    title = [s for s in c["stocks"] if not s["disclosures"] and s["objective"]["diffusion"]["title_mentions"]]
    body_only = [s for s in c["stocks"] if not s["disclosures"] and not s["objective"]["diffusion"]["title_mentions"]]

    def head(s: dict) -> str:
        o = s["objective"]
        d = o["diffusion"]
        extra = f" ⚠{s['warning']}" if s.get("warning") else ""
        diff = ("- 객관: 확산도 제외(공시만, 기사 없음 → diffusion null)" if d["score"] is None else
                f"- 객관: 확산도 {d['score']}점(기사 {d['articles']}·언론사 {d['offices']}·제목 {d['title_mentions']})")
        return (f"### [S-{s['code']}] {s['name']} ({s['market']}, {s.get('sector') or '-'}){extra}\n"
                + diff +
                f" · 규모 {o['scale_score'] if o['scale_score'] is not None else '판단필요'}"
                f" · 최근7일 제목언급 {s['prior']['articles_7d']} · 최근30일 공시 {len(s['prior']['disclosures_30d'])}\n"
                f"- 테마: {', '.join(s['themes'][:6]) or '-'}")

    L.append(f"## A. 공시 있는 종목 ({len(disc)})")
    for s in disc:
        L.append(head(s))
        for d in s["disclosures"]:
            flag = {"buy": "▲", "sell": "▼"}.get(d["signal"], "") + ("*" if d["confirmed"] else "")
            L.append(f"- 공시 {d['received_at']} {flag} {d['report_nm']} — {_figures(d['figures'])}")
            if d.get("body_excerpt") and not (d["figures"] and d["figures"].get("amount")):
                L.append(f"  > {d['body_excerpt'][:500].replace(chr(10), ' / ')}")
        if s["prior"]["disclosures_30d"]:
            L.append("- 최근30일 공시: " + "; ".join(f"{p['rcept_dt']} {p['report_nm'][:30]}"
                                                for p in s["prior"]["disclosures_30d"][:4]))
        for a in s["articles"][:3]:
            L.append(f"- 기사 {a['published_at'][5:16]} {a['office']}: {a['title']}")
        L.append("")

    L.append(f"## B. 기사 제목에 나온 종목 ({len(title)})")
    for s in title:
        L.append(head(s))
        for a in s["articles"][:4]:
            mark = "[제목]" if a["in_title"] else "[본문]"
            L.append(f"- {mark} {a['published_at'][5:16]} {a['office']}: {a['title']}")
            L.append(f"  > {a['snippet']}")
        if s["prior"]["article_titles_7d"]:
            L.append("- 최근7일 제목: " + " | ".join(t[:40] for t in s["prior"]["article_titles_7d"][:3]))
        L.append("")

    L.append(f"## C. 본문에만 나온 종목 ({len(body_only)}) — 대부분 시황 기사 속 등락 종목 나열")
    for s in body_only:
        a = s["articles"][0]
        L.append(f"- [S-{s['code']}] {s['name']} · 기사 {s['objective']['diffusion']['articles']}건 · "
                 f"\"{a['snippet'][60:200]}\"")
    L.append("")

    L.append(f"## D. 테마 신호 ({len(c['theme_signals'])}) — 구성 종목 중 실제 수혜를 골라 [T-번호]로 채점")
    for i, t in enumerate(c["theme_signals"][:20], 1):
        L.append(f"### [T-{i}] {' / '.join(t['themes'])} · 키워드 {', '.join(t['keywords'])}")
        gov = f" · 정부 정책뉴스 {t['gov_articles']}" if t.get("gov_articles") else ""
        L.append(f"- 기사 {t['articles']} · 제목 {t['title_mentions']} · 언론사 {t['offices']}{gov}")
        if t.get("description"):
            L.append(f"- 설명: {t['description'][:200]}")
        for a in t["sample_articles"][:3]:
            tag = "[정부]" if a.get("gov") else ("[제목]" if a["in_title"] else "[본문]")
            L.append(f"- {tag} {a['office']}: {a['title']}")
        L.append(f"- 구성종목({t['members_total']}): " + ", ".join(f"{m['name']}({m['code']})" for m in t["members"][:25]))
        L.append("")

    gov = c.get("gov_policy", [])
    L.append(f"## E. 정부 정책뉴스 (korea.kr, {len(gov)}) — 수혜 종목을 골라 [P-번호:종목코드]로 채점, policy 항목 필수")
    for i, g in enumerate(gov, 1):
        d = g["diffusion"]
        L.append(f"### [P-{i}] {g['published_at'][5:16]} {g['dept']}: {g['title']}")
        kw = ", ".join(d["keywords"]) or "제목에 테마 키워드 없음"
        L.append(f"- 객관: 확산도 {d['score']}점(제목 키워드 [{kw}]를 제목에 다룬 언론 기사 {d['related_articles']}건·"
                 f"언론사 {d['offices']}, 정책브리핑 포함)")
        for rt in d.get("related_titles", []):
            L.append(f"  - 관련 기사: {rt}")
        if g["summary"]:
            L.append(f"- 요약: {g['summary'][:300]}")
        L.append(f"- 걸린 테마: {', '.join(g['themes'][:10]) or '-'}")
        if g["stocks"]:
            L.append(f"- 본문 언급 종목: {', '.join(x['name'] + '(' + x['code'] + ')' for x in g['stocks'][:10])}")
        L.append("")

    L.append(f"## F. 기타 정책 관련 기사 ({len(c['policy_articles'])})")
    for p in c["policy_articles"]:
        L.append(f"- {p['published_at'][5:16]} {p['office']}: {p['title']} [{', '.join(p['policy_terms'][:4])}]")
    return "\n".join(L) + "\n"
