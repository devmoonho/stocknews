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
    """공시 추출 수치 한 줄 + 재료의 질(quality) 판단 힌트."""
    if not f:
        return ""
    k = f.get("kind", "")
    parts = [k]
    if f.get("is_correction"):
        parts.append("정정공시")
    if f.get("amount") is not None:
        parts.append(("회사 유입 " if k == "유상증자" else "금액 ") + _won(f["amount"]))
    if f.get("revenue_ratio_pct") is not None:
        parts.append(f"매출대비 {f['revenue_ratio_pct']}%")
    if f.get("years") and f["years"] > 1:
        parts.append(f"계약 {f['years']}년({f.get('start')}~{f.get('end')}) → 연환산 매출대비 {f.get('annual_revenue_ratio_pct')}%")
    if f.get("market_cap_ratio_pct") is not None:
        parts.append(f"시총대비 {f['market_cap_ratio_pct']}%")
    if f.get("counterparty"):
        parts.append(f"상대 {f['counterparty']}[{f.get('counterparty_type') or '-'}]")
    if f.get("contract_name"):
        parts.append(f"계약명 {f['contract_name'][:30]}")
    if f.get("advance_payment") is not None:
        parts.append("선급금 " + ("있음" if f["advance_payment"] else "없음"))
    if k in ("자사주취득", "자사주소각"):
        if f.get("method"):
            parts.append(f"방식 {f['method']}")
        parts.append(f"소각 {f.get('burn') or '없음'}")
        if f.get("trading_days"):
            dv = f" = 20일 평균 거래대금의 {f['daily_vs_value_pct']}%" if f.get("daily_vs_value_pct") is not None else ""
            parts.append(f"{f['start']}~{f['end']} {f['trading_days']}거래일, 하루 약 {_won(f['daily_amount'])}{dv}")
    elif f.get("method"):
        parts.append(f"방식 {f['method']}")
    if k == "유상증자":
        if f.get("funds"):
            parts.append("용도 " + ", ".join(f"{n} {_won(v)}" for n, v in f["funds"].items()))
        if f.get("discount_pct") is not None:
            parts.append(f"발행가 {f['issue_price']:,.0f}원(기준주가 대비 {f['discount_pct']:+}%)"
                         if f.get("issue_price") else f"할인율 {f['discount_pct']}%")
        if f.get("dilution_pct") is not None:
            parts.append(f"신주 {f['dilution_pct']}%(기존 주식수 대비)")
        if f.get("payment_date"):
            parts.append(f"납입일 {f['payment_date']}")
        parts.append(f"보호예수 {f.get('lockup') or '언급 없음'}")
        if f.get("allottee"):
            fund = "[투자조합]" if f.get("allottee_is_fund") and "투자조합" not in f["allottee"] else ""
            parts.append(f"배정 {f['allottee']}{fund}(관계 {f.get('allottee_relation') or '-'}"
                         f", 조달 {f.get('allottee_funding') or '-'})")
        if f.get("third_party_securities"):
            parts.append("⚑타법인증권 취득 목적" + ("(대상 미확정)" if f.get("target_undecided") else "")
                         + " — 대상 회사·인수자 특수관계 확인 필요")
    if k == "경영권변경":
        if f.get("transfer_amount") is not None:
            parts.append(f"양수도 대금 {_won(f['transfer_amount'])}(기존 대주주에게 — 규모 제외)")
        acq = f.get("acquirer")
        if acq:
            fin = f.get("acquirer_fin_mil") or {}
            fin_s = ", ".join(f"{n} {fin[key]:,.0f}" for key, n in
                              (("capital", "자본금"), ("equity", "자본총계"), ("revenue", "매출"), ("net_income", "순손익"))
                              if key in fin)
            parts.append(f"양수인 {acq}" + ("[투자조합]" if f.get("acquirer_is_fund") else "")
                         + (f" 설립 {f['acquirer_founded']}" if f.get("acquirer_founded") else "")
                         + (f", 최대주주 {f['acquirer_owner']} {f.get('acquirer_owner_pct') or ''}%" if f.get("acquirer_owner") else "")
                         + (f", 재무(백만원) {fin_s}" if fin_s else "")
                         + (" ⚑자본잠식" if fin.get("equity") is not None and fin["equity"] < 0 else ""))
        if f.get("acquirer_stake_pct"):
            parts.append(f"거래 후 지분 {f['acquirer_stake_pct']}%")
        if f.get("closing_date"):
            parts.append(f"거래종결 {f['closing_date']}")
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
        scale = (f"규모 {o['scale_score']}점({o.get('scale_basis') or '공시 수치'})" if o['scale_score'] is not None
                 else "규모 판단필요")
        pp = o.get("preprice")
        pre = f"선반영 {pp['score']}점({pp['reason'].replace(' — 객관 점수', '')})" if pp else "선반영 데이터 없음"
        tc = o.get("theme_cluster")
        if tc:
            pre += f" · 테마 결집도 {tc['score']}점"
        pr = s.get("price") or {}
        cap = f" · 시총 {_won(pr['market_cap'])}" if pr.get("market_cap") else ""
        return (f"### [S-{s['code']}] {s['name']} ({s['market']}, {s.get('sector') or '-'}){extra}\n"
                + diff + f" · {scale}\n- 객관: {pre}{cap}"
                f" · 최근7일 제목언급 {s['prior']['articles_7d']} · 최근30일 공시 {len(s['prior']['disclosures_30d'])}\n"
                f"- 테마: {', '.join(s['themes'][:6]) or '-'}")

    L.append(f"## A. 공시 있는 종목 ({len(disc)})")
    for s in disc:
        L.append(head(s))
        for d in s["disclosures"]:
            flag = {"buy": "▲", "sell": "▼"}.get(d["signal"], "") + ("*" if d["confirmed"] else "")
            L.append(f"- 공시 {d['received_at']} {flag} {d['report_nm']} — {_figures(d['figures'])}")
            if d.get("body_excerpt") and not (d["figures"] and (d["figures"].get("amount")
                                                                 or d["figures"].get("kind") == "경영권변경")):
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
        if t.get("cluster"):
            L.append(f"- 객관: 결집도 {t['cluster']['score']}점({t['cluster']['reason'].replace(' — 객관 점수', '')})")
        if t.get("description"):
            L.append(f"- 설명: {t['description'][:200]}")
        for a in t["sample_articles"][:3]:
            tag = "[정부]" if a.get("gov") else ("[제목]" if a["in_title"] else "[본문]")
            L.append(f"- {tag} {a['office']}: {a['title']}")
        top = [m for m in t["members"] if m.get("rank")][:5]
        if top:
            L.append("- 대장 후보(편입 사유 연관도 순 — 1위 대장 5점·2위 4점·나머지 3점): " +
                     "; ".join(f"{m['rank']}위 {m['name']}({m['code']}) [{', '.join(m['matched'][:4])}]" for m in top))
        L.append(f"- 구성종목({t['members_total']}, 연관도 순): " + ", ".join(f"{m['name']}({m['code']})" for m in t["members"][:25]))
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
        for ld in g.get("leaders", []):
            if ld["top"]:
                L.append(f"- 대장 후보 [{ld['theme']}] 결집도 {ld['cluster']['score']}점: " +
                         "; ".join(f"{m['rank']}위 {m['name']}({m['code']}) [{', '.join(m['matched'][:4])}]" for m in ld["top"][:5]))
        if g["stocks"]:
            L.append(f"- 본문 언급 종목: {', '.join(x['name'] + '(' + x['code'] + ')' for x in g['stocks'][:10])}")
        L.append("")

    L.append(f"## F. 기타 정책 관련 기사 ({len(c['policy_articles'])})")
    for p in c["policy_articles"]:
        L.append(f"- {p['published_at'][5:16]} {p['office']}: {p['title']} [{', '.join(p['policy_terms'][:4])}]")
    L.append("")
    fu = c.get("followups", [])
    L.append(f"## G. 후속 이벤트 ({len(fu)}) — 이전에 채점한 재료의 일정·관련 공시. [F-번호:종목코드]로 재채점(완료=확정, 철회·미이행=악재)")
    for i, f in enumerate(fu, 1):
        L.append(f"### [F-{i}:{f['stock_code']}] {f['stock_name']} · {f['kind']} 예정 {f['due_date']} · "
                 f"상태 {f['status']} → {f['new_status']}" + (" ⚠예정일 지남, 완료 공시 없음" if f.get("overdue") and not f["events"] else ""))
        L.append(f"- 원 재료: {f['origin_date']} 채점 · 공시 {f['origin_rcept']} · {f['detail']}")
        for ev in f["events"]:
            extra = " · 담보·질권 언급" if ev.get("pledge") else ""
            L.append(f"- [{ev['event']}] {ev['received_at']} {ev['report_nm']} (rcept:{ev['rcept_no']}){extra}")
            if ev.get("body_excerpt"):
                L.append(f"  > {ev['body_excerpt'][:300].replace(chr(10), ' / ')}")
        L.append("")
    pend = c.get("followups_pending", [])
    if pend:
        L.append("- 추적 중: " + "; ".join(f"{p['stock_name']} {p['kind']} {p['due_date']}({p['status']})" for p in pend[:12]))
        L.append("")

    return "\n".join(L) + "\n"
