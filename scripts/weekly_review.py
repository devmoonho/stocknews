#!/usr/bin/env python3
"""주간 리뷰 (cron 토요일 08:00): 채점 ↔ 실제 주가 반응 비교 → 가중치 재귀 조절 → 보고(파일·메일·텔레그램).

    python3 scripts/weekly_review.py                  # 지난 7일 리뷰 + 조건 충족 시 가중치 조절 + 발송
    python3 scripts/weekly_review.py --dry-run        # 가중치를 바꾸지 않고 보고서만 (발송 안 함)
    python3 scripts/weekly_review.py --no-send
규칙은 src/stock_news/analysis/review.py 맨 위 설명 참고.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_news import db  # noqa: E402
from stock_news.analysis import review  # noqa: E402
from stock_news.analysis.weights import current, load  # noqa: E402
from stock_news.config import REPORTS_DIR, load_env  # noqa: E402
from stock_news.timewindow import now_kst  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end", default=f"{now_kst():%Y-%m-%d}", help="리뷰 마지막 날짜(기본 오늘)")
    ap.add_argument("--dry-run", action="store_true", help="가중치 변경·발송 없이 보고서만")
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args()
    load_env()
    conn = db.connect()

    # 리뷰 전에 반응 데이터 최신화 (마지막 거래일·5일 수익률 채우기)
    try:
        from stock_news.master.kiwoom_client import KiwoomClient
        from stock_news.tracking import track
        st = track(conn, KiwoomClient())
        print(f"반응 데이터 갱신: {st['dates']}일 · {st['rows']}건")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 반응 데이터 갱신 실패(기존 데이터로 진행): {e}")

    end = dt.date.fromisoformat(a.end)
    start = end - dt.timedelta(days=6)
    target = load().get("target", "oc_excess")
    week_rows = review._rows(conn, f"{start}", f"{end}")
    all_rows = review._rows(conn, None, f"{end}")
    week, cum = review.summarize(week_rows, target), review.summarize(all_rows, target)
    corr = review.factor_corr(all_rows, target)
    corr2 = review.factor_corr(all_rows, "krx_close_excess")      # 보조 목표(시가 상한가 포함)
    prev, ver = current()
    zeroed = set(load().get("zeroed", []))
    history = review.corr_history(conn, f"{end}", target)
    new, notes, zeroed_new = review.propose(prev, corr, cum["all_good"]["n"], history, zeroed)
    new_version = None
    if not a.dry_run:
        review.save_corr(conn, f"{end}", target, corr)
        new_version = review.apply(conn, new, prev, target, cum["all_good"]["n"], notes, zeroed_new)
    elif new != prev:
        notes.append("(--dry-run: 실제로 적용하지 않음)")

    good = [r for r in week_rows if r["grp"] == "scored" and r["sentiment"] == "호재" and r.get(target) is not None]
    good.sort(key=lambda r: r[target], reverse=True)
    md = review.render(f"{start}", f"{end}", week, cum, corr, target, prev, new, new_version, notes,
                       good[:5], good[-5:][::-1] if len(good) > 5 else [], ver, corr2, zeroed_new)
    # 재료 영향 지속성(유형별·근거별 D0~D+10 경로)을 '항목별 상관' 앞에 넣는다
    from stock_news.analysis import persistence
    persist_md = persistence.render(conn, f"{end}")
    md = md.replace("## 항목별 상관", persist_md + "\n" + persistence.render_intraday(conn, f"{end}") + "\n"
                    + persistence.render_leaders(conn, f"{end}") + "\n## 항목별 상관", 1)
    from stock_news.analysis import theme_review     # 테마 예측 성적(예측 vs 기준선, 단계·서열별 다음날)
    try:
        md = md.replace("## 항목별 상관", theme_review.render(conn, f"{end}") + "\n## 항목별 상관", 1)
    except Exception as e:  # noqa: BLE001 — 테마 섹션 실패로 주간 리뷰를 막지 않는다
        print(f"⚠️  테마 예측 섹션 실패: {e!r}")
    out_dir = REPORTS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{end}.md"
    path.write_text(md, encoding="utf-8")
    print(f"주간 리뷰: {path}")
    for n in notes:
        print(f"  - {n}")

    if a.dry_run or a.no_send:
        return 0
    import os
    title = f"[주간 리뷰] {start}~{end} 호재 {week['all_good']['n']}건 · 평균 {review.pct(week['all_good']['mean'])}" + \
            (f" · 가중치 v{new_version}" if new_version else "")
    if os.environ.get("MAIL_SMTP_PASSWORD"):
        from stock_news.mailer import md_to_email_html, send
        try:
            send(title, md_to_email_html(md, title), md)
            print("✅ 메일 발송")
        except Exception as e:  # noqa: BLE001
            print(f"❌ 메일 실패: {e}")
    from stock_news.telegram_notify import TelegramNotifier
    s = week["all_good"]
    win = "-" if s["win"] is None else f"{s['win'] * 100:.0f}%"
    lines = [f"📊 주간 리뷰 {start:%m-%d}~{end:%m-%d}",
             f"호재 {s['n']}건 · 평균 {review.pct(s['mean'])} · 승률 {win}",
             "점수별: " + " / ".join(f"{k}점 {review.pct(v['mean'], 1)}({v['n']})" for k, v in week["by_score"].items() if v["n"]),
             f"비교 기준(채점 안 한 후보): {review.pct(week['control']['mean'], 1)}({week['control']['n']})",
             ("가중치 조절 → v%d" % new_version) if new_version else "가중치 유지"]
    lines += [f"• {n}" for n in notes[:4]]
    pl = [ln[2:].replace("**", "") for ln in persist_md.splitlines() if ln.startswith("- **")]
    if pl:
        lines += ["", "재료 지속성(누적):"] + [f"• {x}" for x in pl[:5]]
    TelegramNotifier().send("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
