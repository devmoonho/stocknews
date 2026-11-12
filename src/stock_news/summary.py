"""메신저(텔레그램)용 짧은 요약 — 텔레그램 HTML parse_mode에서 쓰는 태그(<b>, <i>, <a>)만 사용.

텔레그램 메시지 한 건은 4096자 제한 → 상위 종목 위주로 줄이고, 넘치면 뒤를 자른다.
"""
from __future__ import annotations

import html

from .report import basis_tags, resolve_sources

LIMIT = 4000


def e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def render(conn, scored: dict, candidates: dict, top_n: int = 7, warnings: list[str] | None = None) -> str:
    items = scored["items"]
    good = [i for i in items if i["sentiment"] == "호재"]
    bad = [i for i in items if i["sentiment"] == "악재"]
    w = scored["window"]
    L = [f"<b>📋 {e(scored['date'])} 장전 재료 리포트</b>",
         f"<i>{e(w['start'][5:16])} ~ {e(w['end'][5:16])} · 호재 {len(good)} · 주의 {len(bad)}</i>"]
    if warnings:
        L.append("")
        L.append(f"⚠️ 수집 경고 {len(warnings)}건 — " + e(warnings[0][:120]))
    L.append("")
    if good:
        L.append("<b>호재 상위</b>")
        for n, it in enumerate(good[:top_n], 1):
            links = resolve_sources(conn, it.get("sources", []))
            link = f' <a href="{e(links[0][1])}">원문</a>' if links else ""
            tags = "".join(f"[{t}]" for t in basis_tags(conn, it))
            L.append(f"{n}. <b>{e(it['name'])}</b> {it['final']}점 {tags} — {e(it['material'])}{link}")
        if len(good) > top_n:
            L.append(f"<i>…외 {len(good) - top_n}종목은 메일/리포트 참고</i>")
    else:
        L.append("호재 종목 없음")
    if bad:
        L.append("")
        L.append("<b>주의</b>")
        for it in bad[:5]:
            L.append(f"• {e(it['name'])} — {e(it['material'])}")
    picked = [r for r in scored.get("reviewed_policies") or [] if r.get("picked")]
    if picked:
        names = {i["code"]: i["name"] for i in items}
        L.append("")
        L.append("<b>정책</b>")
        for r in picked[:3]:
            L.append(f"• {e(r['title'])} → {e(', '.join(names.get(c, c) for c in r['picked']))}")
    L.append("")
    L.append("<i>재료 강도 점수이며 매수 추천 아님. 장 시작 전 지수·수급 확인.</i>")
    text = "\n".join(L)
    if len(text) > LIMIT:
        text = text[:LIMIT - 20].rsplit("\n", 1)[0] + "\n<i>…(생략)</i>"
    return text
