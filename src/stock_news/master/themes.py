"""테마-종목 매핑 (키움 ka90001 테마그룹별요청 → ka90002 테마구성종목요청).

매일 전체를 다시 받아 theme_stocks를 통째로 교체한다(빠진 편입/편출을 그대로 반영).
"""
from __future__ import annotations

from .. import db
from ..health import Warnings, check_fields, check_volume
from ..timewindow import now_kst
from .kiwoom_client import KiwoomClient
from .stocks import _DayWindow

SOURCE = "kiwoom_themes"
PATH = "/api/dostk/thme"
GROUP_PAYLOAD = {"qry_tp": "0",          # 0 전체검색
                 "stk_cd": "", "date_tp": "1", "thema_nm": "",
                 "flu_pl_amt_tp": "1",   # 1 상위기간수익률
                 "stex_tp": "1"}         # 1 KRX
MIN_THEMES = 50


def update(conn, client: KiwoomClient) -> dict:
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 테마 그룹 · 구성 종목 갱신")
    try:
        groups = client.call_all("ka90001", PATH, GROUP_PAYLOAD, "thema_grp")
    except Exception as e:
        warn.add(f"{SOURCE}: 테마 그룹 조회 실패 — {e}")
        groups = []
    if not check_fields(groups, ("thema_grp_cd", "thema_nm"), f"{SOURCE}/ka90001", warn):
        groups = []
    if groups and len(groups) < MIN_THEMES:
        warn.add(f"{SOURCE}: 테마 {len(groups)}개 — 예상({MIN_THEMES}+)보다 적음")
    print(f"  테마 그룹 {len(groups)}개")

    stamp = f"{started:%Y-%m-%d %H:%M:%S}"
    mapping: list[tuple] = []
    failed = 0
    for i, g in enumerate(groups, 1):
        code = str(g["thema_grp_cd"]).strip()
        try:
            comps = client.call_all("ka90002", PATH, {"date_tp": "1", "thema_grp_cd": code, "stex_tp": "1"},
                                    "thema_comp_stk")
        except Exception as e:
            failed += 1
            if failed <= 3:
                warn.add(f"{SOURCE}: 테마 {g['thema_nm']}({code}) 구성종목 실패 — {e}")
            continue
        if i == 1 and not check_fields(comps, ("stk_cd", "stk_nm"), f"{SOURCE}/ka90002", warn):
            break
        for c in comps:
            mapping.append(("kiwoom", code, str(c["stk_cd"]).strip().lstrip("A"), str(c.get("stk_nm", "")).strip(),
                            None, stamp))
        if i % 50 == 0:
            print(f"  구성 종목 {i}/{len(groups)}")

    if groups and failed < len(groups) * 0.3:       # 대부분 성공했을 때만 교체 (실패 시 어제 매핑 유지)
        replace_source(conn, "kiwoom",
                       [("kiwoom", str(g["thema_grp_cd"]).strip(), str(g["thema_nm"]).strip(), None,
                         _int(g.get("stk_num")), stamp) for g in groups],
                       mapping)
        print(f"  매핑 {len(mapping)}건 저장 (실패 테마 {failed}개)")
    else:
        warn.add(f"{SOURCE}: 실패가 많아 기존 테마 매핑을 유지함 (실패 {failed}/{len(groups)})")

    check_volume(conn, SOURCE, len(groups), warn)
    db.log_run(conn, SOURCE, stamp, f"{now_kst():%Y-%m-%d %H:%M:%S}", _DayWindow(started),
               len(groups), len(mapping), warn)
    conn.commit()
    return {"source": SOURCE, "fetched": len(groups), "mapped": len(mapping), "warnings": list(warn)}


def replace_source(conn, source: str, theme_rows: list[tuple], mapping: list[tuple]) -> None:
    """한 출처의 테마·매핑을 통째로 교체. theme_rows: (source, code, name, desc, count, updated_at),
    mapping: (source, theme_code, stock_code, stock_name, reason, updated_at)."""
    conn.execute("DELETE FROM themes WHERE source=?", (source,))
    conn.execute("DELETE FROM theme_stocks WHERE source=?", (source,))
    conn.executemany("INSERT OR REPLACE INTO themes (source, theme_code, name, description, stock_count, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", theme_rows)
    conn.executemany("INSERT OR IGNORE INTO theme_stocks (source, theme_code, stock_code, stock_name, reason, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", mapping)


def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
