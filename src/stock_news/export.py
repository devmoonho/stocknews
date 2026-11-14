"""분석(Claude)용 입력 덤프: data/work/YYYY-MM-DD/."""
from __future__ import annotations

import json
from pathlib import Path

from .config import WORK_DIR
from .timewindow import Window


def export_window(conn, window: Window, results: list[dict], out: Path | None = None) -> Path:
    out = out or WORK_DIR / f"{window.report_date:%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    s, e = f"{window.start:%Y-%m-%d %H:%M:%S}", f"{window.end:%Y-%m-%d %H:%M:%S}"

    articles = [dict(r) for r in conn.execute(
        "SELECT id, source, office_name, title, summary, body, published_at, url FROM articles "
        "WHERE published_at BETWEEN ? AND ? ORDER BY published_at DESC", (s, e))]
    disclosures = [dict(r) for r in conn.execute(
        "SELECT rcept_no, received_at, rcept_dt, rcept_time, corp_name, stock_code, corp_cls, report_nm, "
        "flr_nm, rm, signal, confirmed, url, body FROM disclosures "
        "WHERE received_at BETWEEN ? AND ? "
        "   OR (received_at IS NULL AND rcept_dt > ? AND rcept_dt <= ?) "
        "ORDER BY confirmed DESC, received_at DESC", (s, e, f"{window.start:%Y%m%d}", f"{window.end:%Y%m%d}"))]

    def dump(name: str, obj) -> None:
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    dump("articles.json", articles)
    dump("disclosures.json", disclosures)
    dump("collect_summary.json", {
        "window": {"start": s, "end": e},
        "counts": {"articles": len(articles), "disclosures": len(disclosures),
                   "articles_with_body": sum(1 for a in articles if a["body"]),
                   "disclosures_major": sum(1 for d in disclosures if d["body"])},
        "sources": results,
        "warnings": [w for r in results for w in r.get("warnings", [])],
    })
    return out
