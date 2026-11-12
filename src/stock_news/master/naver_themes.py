"""네이버 증권 테마 (키움 테마 보강용) — 테마 설명 + 종목별 편입 사유까지.

목록: https://m.stock.naver.com/api/stocks/theme?page=N&pageSize=100        → groups[{no, name, totalCount}]
구성: https://m.stock.naver.com/api/stocks/theme/{no}?page=N&pageSize=100   → stocks[{itemCode, stockName}],
      themeDescription(테마 설명), themeItemInfoMap{종목코드: 편입 사유}
"""
from __future__ import annotations

from .. import db
from ..health import Warnings, check_fields, check_volume
from ..http import Fetcher
from ..timewindow import now_kst
from .stocks import _DayWindow
from .themes import replace_source

SOURCE = "naver_themes"
BASE = "https://m.stock.naver.com/api/stocks/theme"
REFERER = "https://m.stock.naver.com/domestic/theme"
PAGE_SIZE = 100
MIN_THEMES = 150


def _pages(fetcher: Fetcher, url: str, list_key: str, warn: Warnings) -> tuple[list[dict], dict]:
    """페이지를 끝까지 넘겨 list_key 배열을 모은다 → (항목, 첫 페이지 응답)."""
    items: list[dict] = []
    first: dict = {}
    for page in range(1, 20):
        d = fetcher.get_json(url, {"page": page, "pageSize": PAGE_SIZE}, referer=REFERER)
        if page == 1:
            first = d
        if list_key not in d:
            warn.add(f"{SOURCE}: 응답에 '{list_key}' 없음 — 형식 변경 의심. keys={sorted(d)[:10]}")
            break
        items.extend(d[list_key] or [])
        if len(items) >= int(d.get("totalCount") or 0) or not d[list_key]:
            break
    return items, first


def update(conn, client=None) -> dict:          # client 인자는 키움 모듈과 호출 형태를 맞추려는 것
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 네이버 테마 · 구성 종목 갱신")
    fetcher = Fetcher(min_interval=0.3)
    try:
        groups, _ = _pages(fetcher, BASE, "groups", warn)
    except Exception as e:
        warn.add(f"{SOURCE}: 테마 목록 조회 실패 — {e}")
        groups = []
    if not check_fields(groups, ("no", "name"), f"{SOURCE}/list", warn):
        groups = []
    if groups and len(groups) < MIN_THEMES:
        warn.add(f"{SOURCE}: 테마 {len(groups)}개 — 예상({MIN_THEMES}+)보다 적음")
    print(f"  테마 {len(groups)}개")

    stamp = f"{started:%Y-%m-%d %H:%M:%S}"
    theme_rows, mapping, failed, no_reason = [], [], 0, 0
    for i, g in enumerate(groups, 1):
        no = str(g["no"])
        try:
            stocks, first = _pages(fetcher, f"{BASE}/{no}", "stocks", warn)
        except Exception as e:
            failed += 1
            if failed <= 3:
                warn.add(f"{SOURCE}: 테마 {g['name']}({no}) 조회 실패 — {e}")
            continue
        if i == 1 and not check_fields(stocks, ("itemCode", "stockName"), f"{SOURCE}/stocks", warn):
            break
        reasons = first.get("themeItemInfoMap") or {}
        theme_rows.append(("naver", no, str(g["name"]).strip(), (first.get("themeDescription") or "").strip() or None,
                           len(stocks), stamp))
        for s in stocks:
            code = str(s["itemCode"]).strip()
            reason = (reasons.get(code) or "").strip() or None
            no_reason += reason is None
            mapping.append(("naver", no, code, str(s.get("stockName") or "").strip(), reason, stamp))
        if i % 50 == 0:
            print(f"  구성 종목 {i}/{len(groups)}")

    if theme_rows and failed < len(groups) * 0.3:
        replace_source(conn, "naver", theme_rows, mapping)
        print(f"  매핑 {len(mapping)}건 저장 (편입 사유 없는 매핑 {no_reason}건, 실패 테마 {failed}개)")
    else:
        warn.add(f"{SOURCE}: 실패가 많아 기존 네이버 테마 매핑을 유지함 (실패 {failed}/{len(groups)})")
    if mapping and no_reason > len(mapping) * 0.5:
        warn.add(f"{SOURCE}: 편입 사유가 절반 이상 비어 있음 — themeItemInfoMap 형식 변경 의심")

    check_volume(conn, SOURCE, len(groups), warn)
    db.log_run(conn, SOURCE, stamp, f"{now_kst():%Y-%m-%d %H:%M:%S}", _DayWindow(started),
               len(groups), len(mapping), warn)
    conn.commit()
    return {"source": SOURCE, "fetched": len(groups), "mapped": len(mapping), "warnings": list(warn)}
