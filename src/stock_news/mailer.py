"""리포트 메일 — 메일 클라이언트(Gmail 등)에 맞춘 HTML(인라인 스타일, 표 레이아웃) + 텍스트 대체본.

메일 클라이언트는 <style>·외부 CSS·스크립트를 대부분 무시하므로 모든 스타일을 인라인으로 넣고,
레이아웃은 table로 짠다. 폭은 최대 640px, 휴대폰에서도 한 열로 읽힌다.

발송: Gmail SMTP(smtp.gmail.com:465, SSL) + 앱 비밀번호.
  .env: MAIL_SMTP_USER=보내는 Gmail 주소, MAIL_SMTP_PASSWORD=앱 비밀번호(16자리), MAIL_TO=받는 주소(쉼표로 여러 명)
"""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .analysis.scoring import WEIGHTS
from .report import (FACTOR_LABELS, SOURCE_NAMES, basis_tags, collect_status, disclosure_rows, figures_text,
                     theme_bundles, _names_line,
                     resolve_sources)

# 색 (밝은 배경 기준 — 대부분 메일 앱의 다크모드는 자동 반전)
INK, MUTED, LINE, BG, CARD = "#1f2328", "#59636e", "#d8dee4", "#f6f8fa", "#ffffff"
ACCENT, WARN_BG, WARN_INK, BAD = "#0b5cad", "#fff8e1", "#7a5300", "#b42318"
SCORE_BG = {5: "#0b5cad", 4: "#2f7fd1", 3: "#5c9be0", 2: "#9dbfe8", 1: "#cfdff3"}
SCORE_INK = {5: "#ffffff", 4: "#ffffff", 3: "#ffffff", 2: "#0b2f57", 1: "#0b2f57"}
FONT = "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif"


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _badge(score: int | None) -> str:
    if score is None:
        return ""
    return (f'<span style="display:inline-block;min-width:22px;padding:2px 7px;border-radius:10px;'
            f'background:{SCORE_BG.get(score, LINE)};color:{SCORE_INK.get(score, INK)};font-weight:700;'
            f'font-size:13px;text-align:center;">{score}</span>')


def _h2(text: str) -> str:
    return (f'<tr><td style="padding:22px 0 8px 0;font-size:16px;font-weight:700;color:{INK};'
            f'border-bottom:2px solid {INK};">{e(text)}</td></tr>')


def _table(head: list[str], rows: list[list[str]], widths: list[str] | None = None) -> str:
    th = "".join(f'<th align="left" style="padding:7px 8px;background:{BG};border-bottom:1px solid {LINE};'
                 f'font-size:12px;color:{MUTED};font-weight:600;{("width:" + widths[i] + ";") if widths else ""}">'
                 f'{h}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(
        f'<td valign="top" style="padding:7px 8px;border-bottom:1px solid {LINE};font-size:13px;color:{INK};'
        f'line-height:1.5;">{c}</td>' for c in r) + "</tr>" for r in rows)
    return (f'<tr><td style="padding-top:8px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="border-collapse:collapse;border:1px solid {LINE};">'
            f'<tr>{th}</tr>{body}</table></td></tr>')


TAG_COLOR = {"공시": ("#e8f1fb", "#0b5cad"), "뉴스": ("#eef6ee", "#2d6a2d"), "정책": ("#f6effb", "#6b3fa0")}


def _tags(tags: list[str]) -> str:
    return " ".join(f'<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;'
                    f'background:{TAG_COLOR[t][0]};color:{TAG_COLOR[t][1]};">{t}</span>' for t in tags)


def _link(name: str, url: str) -> str:
    return f'<a href="{e(url)}" style="color:{ACCENT};text-decoration:none;">{e(name)}</a>'


def _note(text: str) -> str:
    return f'<tr><td style="font-size:11px;color:{MUTED};padding-top:6px;line-height:1.5;">{text}</td></tr>'


def theme_outlook_html(tf: dict | None) -> list[str]:
    """메일 '테마 전망' (report.theme_outlook_section과 같은 내용, 저녁 1차/아침 최종)."""
    if not tf or not tf.get("prev_day"):
        return []
    ev = tf.get("phase") == "evening"
    day_word, next_word = ("오늘", f"내일({tf['date'][5:]})") if ev else ("어제", "오늘")
    R = [_h2(f"{next_word} 테마 전망 — 1차 (장 마감 집계 + 15:30 이후 재료)" if ev
             else f"테마 전망 — 최종 (전일 {tf['prev_day']} 집계 + 밤사이 재료)")]
    sub = lambda t: f'<br><span style="color:{MUTED};font-size:11px;">{e(t)}</span>'  # noqa: E731
    rows = []
    for it in tf.get("leaders_yday", []):
        stage = f"{it['stage']}({it['streak']}일)" if it.get("stage") else "-"
        if it.get("leader_changed"):
            stage += " · 대장 교체"
        rows.append([f"<b>{e(it['name'])}</b>" + (sub("관련: " + ", ".join(it["related"][:3])) if it.get("related") else ""),
                     f"{it['median_excess']:+.1f}%p" + sub(f"상승 {round((it['rise_ratio'] or 0) * 100)}% · +5% {it['up5']}"
                                                           + (f" · 거래대금 {it['tv_ratio20']:.1f}배" if it.get('tv_ratio20') else "")),
                     e(stage),
                     "대장 " + e(_names_line(it["leader"], True)) + sub("2등 " + _names_line(it["seconds"], True))
                     + sub("후발 " + _names_line(it["followers"]))])
    if rows:
        R.append(f'<tr><td style="font-size:13px;font-weight:600;padding-top:8px;">{day_word} 주도 테마</td></tr>')
        R.append(_table(["테마", "중앙값 초과", "단계", "서열"], rows, ["30%", "22%", "14%", "34%"]))
    h = tf.get("hedge") or {}
    if h.get("items"):
        R.append(_note("헤지 테마(지수 하락일 " + str(h["down_days"]) + "일 누적" + (", 표시만" if h.get("display_only") else "")
                       + "): " + e(", ".join(f"{x['name']} 초과 {x['cum_excess']:+.1f}%p·등락 {x.get('cum_pct', 0):+.1f}%" for x in h["items"]))))
    rows = []
    for i, it in enumerate(tf.get("forecast", []), 1):
        nx = it.get("nxt") or {}
        ld = (it.get("leader_name") or "-") + (" (NXT✕?)" if it.get("leader_code") and nx.get(it["leader_code"]) is False else "")
        sec = ", ".join((n or c) + (" (NXT✕?)" if nx.get(c) is False else "")
                        for c, n in zip(it.get("seconds", []), it.get("second_names", []))) or "-"
        rows.append([str(i), f"<b>{e(it['name'])}</b>" + (sub("관련: " + ", ".join(it["related"][:3])) if it.get("related") else ""),
                     e("; ".join(it.get("basis", [])) or "-"), f"{e(it['confidence'])}" + sub(f"{it['score']:.2f}"),
                     e(ld) + sub("2등 " + sec)])
    if rows:
        R.append(f'<tr><td style="font-size:13px;font-weight:600;padding-top:12px;">{next_word} 주목 테마 후보 (예측)</td></tr>')
        R.append(_table(["#", "테마", "근거", "신뢰도", "대장·2등 후보"], rows, ["5%", "25%", "38%", "10%", "22%"]))
    f = (tf.get("track") or {}).get("forecast") or {}
    b = (tf.get("track") or {}).get("baseline") or {}
    if f.get("days"):
        R.append(_note(f"예측 성적(최근 {f['days']}일, 방향 확인용): 상위10 적중 {round((f['top10_ratio'] or 0) * 100)}% · "
                       f"기준선 {round((b.get('top10_ratio') or 0) * 100)}%"))
    else:
        R.append(_note("예측 성적: 아직 채점 기록 없음 — 표본이 쌓이기 전에는 방향 확인용."))
    R.append(_note("NXT✕? = 종목 마스터상 NXT 거래 불가(실제로 거래되는 경우가 있어 확인 필요)."))
    return R


def render_html(conn, scored: dict, candidates: dict) -> str:
    date, w, items = scored["date"], scored["window"], scored["items"]
    good = [i for i in items if i["sentiment"] == "호재"]
    bad = [i for i in items if i["sentiment"] == "악재"]
    runs, warnings = collect_status(conn, w)
    c = candidates.get("counts", {})
    R: list[str] = []
    evening = scored.get("session") == "evening"
    kind = "저녁 종가매매 리포트" if evening else "장전 재료 리포트"

    # 머리말
    R.append(f'<tr><td style="padding:4px 0 2px 0;font-size:12px;color:{MUTED};">{kind}</td></tr>')
    R.append(f'<tr><td style="font-size:22px;font-weight:700;color:{INK};padding-bottom:6px;">{e(date)}</td></tr>')
    R.append(f'<tr><td style="font-size:13px;color:{MUTED};line-height:1.6;">'
             f'대상 {e(w["start"][:16])} ~ {e(w["end"][:16])}<br>'
             f'기사 {e(c.get("articles", "-"))}건(정부 정책뉴스 {e(c.get("gov_policy", "-"))}) · '
             f'공시 {e(c.get("disclosures_listed", "-"))}건 · 후보 {e(c.get("stocks", "-"))}종목 검토 → '
             f'<b style="color:{INK};">호재 {len(good)}</b> · 주의 {len(bad)}</td></tr>')
    if scored.get("fallback"):
        R.append(f'<tr><td style="padding-top:12px;"><div style="background:{WARN_BG};color:{WARN_INK};'
                 f'border:1px solid #f0d58a;border-radius:6px;padding:10px 12px;font-size:13px;">'
                 f'<b>⚠ Claude 채점 미완료</b> — 객관 지표(공시 규모·선반영·시간외 반응)만으로 만든 간이 리포트</div></td></tr>')
    if warnings:
        lis = "".join(f"<li style='margin:2px 0;'>{e(x)}</li>" for x in warnings)
        R.append(f'<tr><td style="padding-top:12px;"><div style="background:{WARN_BG};color:{WARN_INK};'
                 f'border:1px solid #f0d58a;border-radius:6px;padding:10px 12px;font-size:13px;">'
                 f'<b>⚠ 수집 경고</b> — 결과가 불완전할 수 있음<ul style="margin:6px 0 0 18px;padding:0;">{lis}</ul>'
                 f'</div></td></tr>')

    # 상위 종목 요약 카드
    if good:
        top = good[:3]
        cells = "".join(
            f'<td valign="top" width="33%" style="padding:10px;border:1px solid {LINE};border-radius:6px;">'
            f'{_badge(it["final"])} <span style="font-size:14px;font-weight:700;color:{INK};">{e(it["name"])}</span>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:4px;line-height:1.45;">{e(it["material"])}</div></td>'
            for it in top)
        R.append(f'<tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" '
                 f'cellspacing="6" style="border-collapse:separate;"><tr>{cells}</tr></table></td></tr>')

    if evening and good:                # 종가매매 후보 (시간외 시세)
        R.append(_h2("종가매매 후보 (시간외 시세)"))
        asof = next((it["evening"]["as_of"] for it in good if it.get("evening")), "-")
        pct = lambda v: "-" if v is None else f"{v:+.1f}%"  # noqa: E731
        rows = []
        for n, it in enumerate(good, 1):
            q = it.get("evening") or {}
            nxt = {True: "가능", False: '<b style="color:#b42318;">불가</b>'}.get(q.get("nxt_enabled"), "-")
            krx, now = q.get("krx_close") or 0, q.get("price") or 0
            rows.append([f'{n}. <b>{e(it["name"])}</b> {_badge(it["final"])}',
                         f'{krx:,.0f} → <b>{now:,.0f}</b>',
                         f'<b>{pct(q.get("after_hours_pct"))}</b><br><span style="color:{MUTED};font-size:11px;">공시 후 '
                         f'{pct(q.get("since_disclosure_pct"))}</span>',
                         f'{(q.get("after_value") or 0) / 1e8:,.1f}억', nxt])
        R.append(_table(["종목", "KRX 종가 → 지금", "시간외", "시간외 대금", "NXT"], rows, ["34%", "26%", "16%", "14%", "10%"]))
        R.append(f'<tr><td style="font-size:11px;color:{MUTED};padding-top:6px;">시세 기준 {e(asof)} · NXT 불가 종목은 '
                 f'애프터마켓에서 살 수 없음 · 과거 같은 유형 평균 갭 비교는 첨부 리포트(.md) 참고</td></tr>')

    # 체크리스트 (수동 기입용 — 메일을 인쇄하거나 답장에 적어 쓰도록 빈칸)
    R.append(_h2("내일 장 시작 전 체크리스트" if evening else "장 시작 전 체크리스트"))
    blank = f'<span style="color:{LINE};">______________</span>'
    R.append(_table(["항목", "확인", "메모"], [
        ["오늘 마감", "코스피·코스닥 종가, 외국인·기관·개인 순매수", blank],
        ["시간외 분위기", "NXT 거래대금 상위·급등, 후보 호가 잔량", blank],
        ["해외", "美 선물·환율 방향", blank],
        ["내일 일정", "실적 발표·경제지표·만기", blank],
        ["오늘 저녁 판단", "매수 / 관망 (가격·비중)", blank],
    ] if evening else [
        ["지수 흐름", "美 증시 마감 / 코스피·코스닥 선물 / 환율", blank],
        ["장초반 매수세", "09:00~09:10 지수 방향, 거래대금 상위 업종", blank],
        ["외국인 수급", "선물·현물 순매수 방향", blank],
        ["기관 수급", "순매수, 프로그램 매매", blank],
        ["개인 수급", "순매수 방향", blank],
        ["오늘의 판단", "공격 / 중립 / 관망", blank],
    ], ["22%", "48%", "30%"]))

    R += theme_outlook_html(candidates.get("theme_forecast"))
    # 순위표
    R.append(_h2(f"호재 종목 ({len(good)})"))
    if good:
        R.append(_table(["#", "종목", "점수", "재료"], [
            [str(n), f'<b>{e(it["name"])}</b><br><span style="color:{MUTED};font-size:11px;">{e(it["code"])}</span>',
             f'{_badge(it["final"])}<br><span style="color:{MUTED};font-size:11px;">{e(it["final_raw"])}</span>',
             f'{_tags(basis_tags(conn, it))}<br>{e(it["material"])}'] for n, it in enumerate(good, 1)],
            ["6%", "22%", "12%", "60%"]))
        weights = " · ".join(f"{FACTOR_LABELS[k]} {int(v * 100)}%" for k, v in WEIGHTS.items())
        R.append(f'<tr><td style="font-size:11px;color:{MUTED};padding-top:6px;">가중치: {e(weights)}</td></tr>')
    else:
        R.append(f'<tr><td style="font-size:13px;color:{MUTED};padding-top:8px;">해당 없음</td></tr>')

    # 공시 기반
    disc_items = [(n, it) for n, it in enumerate(good, 1) if "공시" in basis_tags(conn, it)]
    if disc_items:
        R.append(_h2(f"공시 기반 종목 ({len(disc_items)})"))
        rows = []
        for n, it in disc_items:
            for d in disclosure_rows(candidates, it) or [{}]:
                rows.append([f'{n}. <b>{e(it["name"])}</b> {_badge(it["final"])}',
                             _link(d.get("report_nm", "-"), d.get("url", "")) if d.get("url") else "-",
                             e(figures_text(d.get("figures"))), e(str(d.get("received_at", ""))[5:16])])
        R.append(_table(["종목", "공시", "규모", "접수"], rows, ["26%", "34%", "26%", "14%"]))
        R.append(f'<tr><td style="font-size:11px;color:{MUTED};padding-top:6px;">'
                 f'기사 없이 공시만 있는 종목은 확산도를 빼고 계산</td></tr>')

    # 테마 묶음
    bundles = theme_bundles(scored, candidates)
    if bundles:
        R.append(_h2(f"테마 묶음 ({len(bundles)})"))
        rows = []
        for b in bundles:
            picked = {it["code"] for it in b["picked"]}
            lead = ", ".join(f"{m['rank']}위 {m['name']}" + (" ✔" if m["code"] in picked else "") for m in b["leaders"]) or "-"
            rows.append([f"<b>{e(b['theme'])}</b><br>결집도 {(b['cluster'] or {}).get('score', '-')}점", e(lead),
                         e(", ".join(f"{it['name']}({it['final']}점)" for it in b["picked"])),
                         e(", ".join(m["name"] for m in b["others"]) or "-")])
        R.append(_table(["테마", "대장 후보", "고른 종목", "안 고른 상위"], rows, ["28%", "26%", "26%", "20%"]))

    # 종목별 상세
    if good:
        R.append(_h2("종목별 근거"))
    for n, it in enumerate(good, 1):
        rows = ""
        for k in WEIGHTS:
            f = (it.get("factors") or {}).get(k)
            if f is None:
                if k == "diffusion":
                    rows += (f'<tr><td style="padding:3px 6px 3px 0;font-size:12px;color:{MUTED};">{FACTOR_LABELS[k]}</td>'
                             f'<td style="padding:3px 6px;font-size:11px;color:{MUTED};">제외</td>'
                             f'<td style="padding:3px 0;font-size:12px;color:{MUTED};">공시만 있고 관련 기사 없음</td></tr>')
                continue
            rows += (f'<tr><td valign="top" style="padding:3px 6px 3px 0;font-size:12px;color:{MUTED};'
                     f'white-space:nowrap;width:70px;">{FACTOR_LABELS[k]}</td>'
                     f'<td valign="top" style="padding:3px 6px;width:26px;">{_badge(f["score"])}</td>'
                     f'<td valign="top" style="padding:3px 0;font-size:12px;color:{INK};line-height:1.45;">'
                     f'{e(f.get("reason"))}</td></tr>')
        links = resolve_sources(conn, it.get("sources", []))
        link_html = "<br>".join("· " + _link(name, url) for name, url in links)
        caution = (f'<div style="font-size:12px;color:{WARN_INK};padding-top:6px;">⚠ {e(it["caution"])}</div>'
                   if it.get("caution") else "")
        R.append(
            f'<tr><td style="padding:12px 0;border-bottom:1px solid {LINE};">'
            f'<div style="font-size:15px;font-weight:700;color:{INK};">{n}. {e(it["name"])} '
            f'<span style="font-weight:400;color:{MUTED};font-size:12px;">{e(it["code"])}</span> {_badge(it["final"])} '
            f'{_tags(basis_tags(conn, it))}</div>'
            f'<div style="font-size:13px;color:{INK};padding:4px 0 6px 0;">{e(it["material"])}</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{rows}</table>'
            f'{caution}<div style="font-size:12px;padding-top:6px;line-height:1.6;">{link_html}</div></td></tr>')

    # 악재
    R.append(_h2(f"주의 종목 ({len(bad)})"))
    if bad:
        rows = []
        for it in bad:
            links = resolve_sources(conn, it.get("sources", []))
            src = " ".join(_link(f"[{i + 1}]", u) for i, (_, u) in enumerate(links[:3]))
            note = e(it["material"]) + (f'<br><span style="color:{MUTED};">{e(it["caution"])}</span>'
                                        if it.get("caution") else "")
            rows.append([f'<b style="color:{BAD};">{e(it["name"])}</b>', note, src])
        R.append(_table(["종목", "내용", "원문"], rows, ["22%", "63%", "15%"]))
    else:
        R.append(f'<tr><td style="font-size:13px;color:{MUTED};padding-top:8px;">해당 없음</td></tr>')

    # 정책
    reviewed = scored.get("reviewed_policies") or []
    gov = {f"P-{i}": g for i, g in enumerate(candidates.get("gov_policy", []), 1)}
    names = {i["code"]: i["name"] for i in items}
    if reviewed:
        R.append(_h2(f"정부 정책뉴스 검토 ({len(reviewed)})"))
        rows = []
        for r in reviewed:
            g = gov.get(r.get("id"), {})
            title = _link(r.get("title") or g.get("title", ""), g["url"]) if g.get("url") else e(r.get("title"))
            picked = ", ".join(names.get(x, x) for x in r.get("picked", []))
            rows.append([f'{title}<br><span style="color:{MUTED};font-size:11px;">{e(g.get("dept"))}</span>',
                         f'<b>{e(picked)}</b>' if picked else f'<span style="color:{MUTED};">-</span>',
                         e(r.get("note"))])
        R.append(_table(["정책", "수혜", "판단"], rows, ["45%", "20%", "35%"]))

    # 수집 현황
    R.append(_h2("수집 현황"))
    R.append(_table(["소스", "건수", "실행", "경고"], [
        [e(SOURCE_NAMES.get(r["source"], r["source"])), e(r["fetched"]),
         e((r["finished_at"] or r["started_at"])[5:16]), e(len(r["warnings"]) or "-")] for r in runs]))

    R.append(f'<tr><td style="padding-top:18px;font-size:11px;color:{MUTED};line-height:1.6;">'
             f'점수는 고정 채점표 기준의 재료 강도이며 매수 추천이 아닙니다. 최종 판단은 직접 하세요.<br>'
             f'원본: reports/{"evening/" if evening else ""}{e(date)}.md</td></tr>')

    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(date)} {kind}</title></head>'
            f'<body style="margin:0;padding:0;background:{BG};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};">'
            f'<tr><td align="center" style="padding:16px 10px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:640px;background:{CARD};border:1px solid {LINE};border-radius:8px;">'
            f'<tr><td style="padding:18px 18px 22px 18px;font-family:{FONT};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(R)}</table>'
            f'</td></tr></table></td></tr></table></body></html>')


def subject(scored: dict) -> str:
    good = [i for i in scored["items"] if i["sentiment"] == "호재"]
    top = ", ".join(f"{i['name']}({i['final']})" for i in good[:3])
    tag = "[종가매매]" if scored.get("session") == "evening" else "[장전 리포트]"
    fb = " (간이)" if scored.get("fallback") else ""
    return f"{tag}{fb} {scored['date']} 호재 {len(good)}종목" + (f" — {top}" if top else "")


def send(subject_text: str, html_body: str, text_body: str, to: list[str] | None = None) -> list[str]:
    user = os.environ.get("MAIL_SMTP_USER", "").strip()
    password = os.environ.get("MAIL_SMTP_PASSWORD", "").replace(" ", "").strip()
    to = to or [x.strip() for x in os.environ.get("MAIL_TO", "").split(",") if x.strip()]
    host = os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("MAIL_SMTP_PORT", "465"))
    if not (user and password and to):
        raise ValueError("MAIL_SMTP_USER / MAIL_SMTP_PASSWORD / MAIL_TO 가 .env에 없습니다")
    msg = EmailMessage()
    msg["Subject"] = subject_text
    msg["From"] = formataddr(("장전 리포트", user))
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
    return to


# ---------------------------------------------------------------- 마크다운 → 메일 HTML (주간 리뷰 등 단순 문서용)
def md_to_email_html(md: str, title: str) -> str:
    """제목(#), 표(|), 목록(-), 굵게(**), 링크, <sub> 정도만 처리하는 간단 변환기."""
    import re as _re

    def inline(t: str) -> str:
        t = e(t)
        t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = _re.sub(r"`(.+?)`", r'<code style="background:#eef1f4;padding:0 3px;border-radius:3px;">\1</code>', t)
        t = _re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", rf'<a href="\2" style="color:{ACCENT};">\1</a>', t)
        t = t.replace("&lt;sub&gt;", f'<span style="font-size:11px;color:{MUTED};">').replace("&lt;/sub&gt;", "</span>")
        return t

    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            rows = [[c.strip() for c in r.strip("|").split("|")] for r in block if not _re.match(r"^\|[-| ]+\|$", r)]
            if rows:
                out.append(_table([inline(c) for c in rows[0]], [[inline(c) for c in r] for r in rows[1:]]))
            continue
        if ln.startswith("# "):
            out.append(f'<tr><td style="font-size:20px;font-weight:700;color:{INK};padding-bottom:6px;">{inline(ln[2:])}</td></tr>')
        elif ln.startswith("## "):
            out.append(_h2(ln[3:]))
        elif ln.startswith("### "):
            out.append(f'<tr><td style="padding:12px 0 2px 0;font-size:14px;font-weight:700;color:{INK};">{inline(ln[4:])}</td></tr>')
        elif ln.startswith("- "):
            out.append(f'<tr><td style="font-size:13px;color:{INK};padding:2px 0 2px 10px;">• {inline(ln[2:])}</td></tr>')
        elif ln.strip():
            out.append(f'<tr><td style="font-size:13px;color:{INK};padding:4px 0;">{inline(ln)}</td></tr>')
        i += 1
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{e(title)}</title></head>'
            f'<body style="margin:0;padding:0;background:{BG};"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="background:{BG};"><tr><td align="center" style="padding:16px 10px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;background:{CARD};'
            f'border:1px solid {LINE};border-radius:8px;"><tr><td style="padding:18px;font-family:{FONT};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(out)}</table>'
            f'</td></tr></table></td></tr></table></body></html>')
