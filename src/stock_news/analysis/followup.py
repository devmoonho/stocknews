"""후속 이벤트 추적 — 채점한 재료의 일정(제3자배정 유증 납입일·경영권 거래종결일)을 등록하고,
매일 아침 그 종목의 새 공시로 상태를 갱신해 다이제스트 G에 올린다(Claude가 재채점).

상태: 대기 → 완료(납입·최대주주 변경 확인) / 정정(일정·조건 변경) / 철회(철회·해제·미이행) / 기한경과(예정일 지남, 공시 없음)
"""
from __future__ import annotations

import datetime as dt
import re

from ..timewindow import Window, now_kst

WATCH_DAYS = 120             # 예정일이 지나고도 이만큼 지나면 추적 종료
OPEN = ("대기", "정정", "기한경과")

# 새 공시 제목 → 후속 이벤트 종류 (위에서부터)
EVENT_RULES = [
    ("철회", r"철회|취소|해제|해지|미납|납입\s*불이행|무산"),
    ("완료", r"최대주주\s*변경(?!을수반)|증권발행실적|유상증자\s*납입|신주\s*상장|추가\s*상장|발행결과"),
    ("정정", r"정정|변경"),
    ("지분", r"대량보유|소유상황|임원ㆍ주요주주"),
]


def register(conn, date: str, items: list[dict], candidates: dict) -> int:
    """score.py 저장 후 호출: 호재로 채점된 종목의 근거 공시 중 유증 납입일·거래종결일이 있으면 등록."""
    figs = {d["rcept_no"]: (st, d) for st in candidates.get("stocks", []) for d in st.get("disclosures", [])}
    n = 0
    for it in items:
        if it.get("sentiment") != "호재":
            continue
        for src in it.get("sources", []):
            kind, _, no = str(src).partition(":")
            if kind != "rcept" or no not in figs:
                continue
            st, d = figs[no]
            f = d.get("figures") or {}
            rows = []
            if f.get("kind") == "유상증자" and f.get("payment_date"):
                rows.append(("유증납입", f["payment_date"],
                             f"{(f.get('amount') or 0) / 1e8:,.1f}억 · {f.get('allottee') or '대상자 미상'} · 보호예수 "
                             f"{f.get('lockup') or '-'}"))
            if f.get("kind") == "경영권변경" and f.get("closing_date"):
                rows.append(("거래종결", f["closing_date"],
                             f"양수인 {f.get('acquirer') or '-'} · 양수도 {(f.get('transfer_amount') or 0) / 1e8:,.1f}억"))
            for k, due, detail in rows:
                cur = conn.execute("INSERT OR IGNORE INTO followups (origin_rcept, kind, stock_code, stock_name, "
                                   "origin_date, due_date, detail, status, updated_at) VALUES (?,?,?,?,?,?,?,'대기',?)",
                                   (no, k, it["code"], it["name"], date, due, detail,
                                    f"{now_kst():%Y-%m-%d %H:%M:%S}"))
                n += cur.rowcount
    conn.commit()
    return n


def watched_codes(conn) -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT DISTINCT stock_code FROM followups WHERE status IN "
                                       f"({','.join('?' * len(OPEN))})", OPEN)}


def _event_kind(report_nm: str) -> str | None:
    t = report_nm.replace(" ", "")
    for kind, rx in EVENT_RULES:
        if re.search(rx, t):
            return kind
    return None


def scan(conn, window: Window, write: bool = True) -> dict:
    """시간대 안의 새 공시로 추적 중인 일정의 상태를 갱신 → {"events": 다이제스트 G에 올릴 것, "pending": 추적 중 목록}."""
    s, e = f"{window.start:%Y-%m-%d %H:%M:%S}", f"{window.end:%Y-%m-%d %H:%M:%S}"
    today = f"{window.end:%Y-%m-%d}"
    out = []
    rows = conn.execute(f"SELECT * FROM followups WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY due_date",
                        OPEN).fetchall()
    for f in rows:
        f = dict(f)
        if f["due_date"] and f["due_date"] < f"{dt.date.fromisoformat(today) - dt.timedelta(days=WATCH_DAYS):%Y-%m-%d}":
            if write:
                conn.execute("UPDATE followups SET status='종료', updated_at=? WHERE origin_rcept=? AND kind=?",
                             (f"{now_kst():%Y-%m-%d %H:%M:%S}", f["origin_rcept"], f["kind"]))
            continue
        news = conn.execute(
            "SELECT rcept_no, report_nm, received_at, rcept_dt, body FROM disclosures WHERE stock_code=? AND rcept_no<>? "
            "AND (received_at BETWEEN ? AND ? OR (received_at IS NULL AND rcept_dt > ? AND rcept_dt <= ?)) "
            "ORDER BY rcept_no", (f["stock_code"], f["origin_rcept"], s, e, f"{window.start:%Y%m%d}",
                                  f"{window.end:%Y%m%d}")).fetchall()
        events = []
        status = f["status"]
        for d in news:
            k = _event_kind(d["report_nm"])
            if not k:
                continue
            ev = {"rcept_no": d["rcept_no"], "report_nm": d["report_nm"], "received_at": d["received_at"] or d["rcept_dt"],
                  "event": k, "body_excerpt": (d["body"] or "")[:600] or None}
            if k == "지분" and d["body"]:
                ev["pledge"] = bool(re.search(r"담보|질권", d["body"]))
            events.append(ev)
            if k in ("철회", "완료", "정정"):
                status = k if not (k == "정정" and status == "완료") else status
        overdue = status in ("대기", "기한경과") and f["due_date"] and f["due_date"] < today
        if overdue and not events:
            status = "기한경과"
        if events or (overdue and f["status"] != "기한경과"):
            out.append({**f, "new_status": status, "events": events, "overdue": overdue})
        if write and status != f["status"]:
            conn.execute("UPDATE followups SET status=?, resolved_rcept=?, updated_at=? WHERE origin_rcept=? AND kind=?",
                         (status, events[-1]["rcept_no"] if events else None, f"{now_kst():%Y-%m-%d %H:%M:%S}",
                          f["origin_rcept"], f["kind"]))
    if write:
        conn.commit()
    pending = [dict(r) for r in conn.execute(
        f"SELECT stock_code, stock_name, kind, due_date, status FROM followups WHERE status IN "
        f"({','.join('?' * len(OPEN))}) ORDER BY due_date", OPEN)]
    return {"events": out, "pending": pending}
