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
    evening = scored.get("session") == "evening"
    title = "🌙 {} 저녁 종가매매 리포트" if evening else "📋 {} 장전 재료 리포트"
    L = [f"<b>{e(title.format(scored['date']))}</b>",
         f"<i>{e(w['start'][5:16])} ~ {e(w['end'][5:16])} · 호재 {len(good)} · 주의 {len(bad)}</i>"]
    if scored.get("fallback"):
        L.append("⚠️ Claude 채점 미완료 — 공시 규모·시간외 반응 등 객관 지표만으로 만든 간이 리포트")
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
            q = it.get("evening") or {}
            quote = ""
            if evening and q.get("after_hours_pct") is not None:
                nxt = "" if q.get("nxt_enabled") is not False else " · NXT 불가"
                quote = f" <i>(시간외 {q['after_hours_pct']:+.1f}% · {q.get('price') or 0:,.0f}원{nxt})</i>"
            sc = "" if it.get("final") is None else f" {it['final']}점"
            L.append(f"{n}. <b>{e(it['name'])}</b>{sc} {tags}{quote} — {e(it['material'])}{link}")
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
    tf = candidates.get("theme_forecast")
    if tf and tf.get("forecast"):
        L.append("")
        L.append(f"<b>내일({e(tf['date'][5:])}) 주목 테마 (1차 예측)</b>" if tf.get("phase") == "evening"
                 else "<b>오늘 주목 테마 (예측)</b>")
        for it in tf["forecast"][:3]:
            basis = "; ".join(it.get("basis", [])[:2]) or "-"
            L.append(f"• {e(it['name'])} — {e(basis)} · 대장 {e(it.get('leader_name') or '-')} ({e(it['confidence'])})")
    L.append("")
    L.append("<i>재료 강도 점수이며 매수 추천 아님. " +
             ("시간외 가격은 리포트 시각 기준, NXT 마감 20:00.</i>" if evening else "장 시작 전 지수·수급 확인.</i>"))
    text = "\n".join(L)
    if len(text) > LIMIT:
        text = text[:LIMIT - 20].rsplit("\n", 1)[0] + "\n<i>…(생략)</i>"
    return text
