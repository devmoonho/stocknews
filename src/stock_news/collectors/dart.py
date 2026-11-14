"""DART 공시 수집: 시간대 안의 공시 전체 저장 + 주요 공시는 본문까지.

- 목록/종목코드: DART Open API list.json (dart_client.DartClient, ras 프로젝트에서 가져온 코드)
- 접수 시각: DART 오늘의 공시 페이지 (dart_times)
- 둘을 접수번호로 합친다. 페이지에만 있는 공시도 저장(종목코드는 비어 있을 수 있음).
"""
from __future__ import annotations

import datetime as dt
import time

from .. import db
from ..config import CACHE_DIR
from ..health import Warnings, check_volume
from ..http import Fetcher
from ..timewindow import Window, now_kst
from . import dart_times
from .dart_client import DartClient, classify_signal, is_confirmed_material

SOURCE = "dart"
MAX_BODIES = 120          # 하루 본문 조회 상한 (DART 일 2만 건 한도 대비 넉넉히 작게)
BODY_MAX_CHARS = 20000
BODY_MAX_ATTEMPTS = 3      # 실행 3번(예: 저녁 19:10·19:30·아침 07:00)에 걸쳐 실패하면 본문 없음으로 확정


# 제목 신호 규칙(dart_client)에 없지만 호재 판단에 본문이 필요한 공시
EXTRA_MAJOR_KEYWORDS = ("투자판단관련주요경영사항", "특허권취득", "기술이전", "기술도입", "임상", "품목허가",
                        "잠정", "공정공시", "신규시설투자", "유형자산취득", "주식교환", "공개매수")


def _is_major(row: dict) -> bool:
    """본문까지 받을 공시: 상장사(종목코드 있음) + (확정형 재료 또는 매수/매도 신호 또는 추가 키워드)."""
    if not row.get("stock_code"):
        return False
    title = row["report_nm"].replace(" ", "")
    return bool(row["confirmed"] or row["signal"] != "neutral" or any(k in title for k in EXTRA_MAJOR_KEYWORDS))


def collect(conn, window: Window, fetch_bodies: bool = True) -> dict:
    started = now_kst()
    warn = Warnings()
    print(f"[{SOURCE}] 공시 수집: {window}")
    client = DartClient(cache_dir=CACHE_DIR / "dart")

    # 1) API 목록 (시간대에 걸친 날짜 전체, 정정 전 원본도 포함해 전부)
    bgn, end = window.start.date(), window.end.date()
    try:
        api_rows = client.list_disclosures(bgn_de=f"{bgn:%Y%m%d}", end_de=f"{end:%Y%m%d}", last_reprt_at=False)
    except Exception as e:
        warn.add(f"dart: list.json 조회 실패 — {e}")
        api_rows = []
    by_no = {r.rcept_no: r for r in api_rows}

    # 2) 시각 (날짜별 오늘의 공시 페이지)
    fetcher = Fetcher(min_interval=0.5)
    times: dict[str, tuple[dt.date, dict]] = {}
    for day in window.dates():
        try:
            rows, total = dart_times.fetch_day(fetcher, day)
        except Exception as e:
            warn.add(f"dart: {day} 오늘의공시 페이지 조회 실패 — {e}")
            continue
        if total and len(rows) < total:
            warn.add(f"dart: {day} 페이지 표시 {total}건 중 {len(rows)}건만 파싱 — 페이지 구조 변경 의심")
        if total == 0 and zero_suspicious(day, now_kst()):
            warn.add(f"dart: {day} 평일인데 오늘의공시 0건 — 페이지 구조 변경 의심")
        for r in rows:
            times[r["rcept_no"]] = (day, r)

    # 3) 합치고 시간대로 거르기
    collected_at = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    merged: list[dict] = []
    intraday: list[dict] = []          # 시간대 밖 15:30 전 공시(장중) → intraday_disclosures
    no_time = 0
    for rcept_no in set(by_no) | set(times):
        api = by_no.get(rcept_no)
        tday, trow = times.get(rcept_no, (None, None))
        if tday is not None:
            hh, mm = map(int, trow["time"].split(":"))
            received = dt.datetime.combine(tday, dt.time(hh, mm))
            if not window.contains(received):
                if received.time() < dt.time(15, 30):
                    intraday.append(_row(rcept_no, api, tday, trow, received, collected_at))
                continue
        else:
            # 시각을 모르면 날짜로만 판단: 시작일 다음 날부터면 포함, 시작일 당일이면 제외(장중일 수 있음)
            no_time += 1
            rdt = dt.datetime.strptime(api.rcept_dt, "%Y%m%d").date()
            if not (bgn < rdt <= end):
                continue
            received = None
        merged.append(_row(rcept_no, api, tday, trow, received, collected_at))
    # 오늘의공시 페이지에는 지분공시(소유상황·대량보유)·펀드 공시 일부가 빠져 있어 시각 없는 공시는 늘 생긴다.
    # 상장사의 주요 공시가 시각 없이 남는 경우만 페이지 구조 변경을 의심한다.
    major_no_time = [d for d in merged if d["rcept_time"] is None and _is_major(d)]
    if len(major_no_time) > 3:
        warn.add(f"dart: 접수 시각을 못 찾은 주요 공시 {len(major_no_time)}건(전체 시각 미상 {no_time}건) "
                 f"— 오늘의공시 페이지 확인 필요. 예: {major_no_time[0]['corp_name']} {major_no_time[0]['report_nm']}")

    inserted = sum(db.upsert_disclosure(conn, d) for d in merged)
    # 장중 공시: 아침(전일 15:30~07시)·저녁(15:30~19:30) 시간대 밖이라 리포트 재료가 아니다(장중 주가에 바로 반영).
    # 다음 날 주가 반응 비교용으로 따로 저장한다(시간대 공시 테이블에 이미 있는 것은 제외).
    have = {r[0] for r in conn.execute("SELECT rcept_no FROM disclosures WHERE rcept_dt IN (%s)" %
                                       ",".join("?" * len(window.dates())), [f"{x:%Y%m%d}" for x in window.dates()])}
    n_intra = 0
    for d in intraday:
        if d["rcept_no"] in have:
            continue
        cur = conn.execute("INSERT OR IGNORE INTO intraday_disclosures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [d[k] for k in ("rcept_no", "rcept_dt", "rcept_time", "received_at", "corp_name", "corp_code",
                                           "stock_code", "corp_cls", "report_nm", "flr_nm", "signal", "confirmed", "url",
                                           "collected_at")])
        n_intra += cur.rowcount
    conn.commit()
    from ..analysis.followup import watched_codes
    watched = watched_codes(conn)                 # 후속 일정 추적 중인 종목은 지분·정정 공시도 본문까지
    majors = [d for d in merged if _is_major(d) or (d.get("stock_code") in watched)]
    print(f"  공시 {len(merged)}건 (신규 {inserted}건, 주요 {len(majors)}건) · 장중 공시 별도 저장 {n_intra}건")

    # 4) 주요 공시 본문
    if fetch_bodies and majors:
        done = {r[0] for r in conn.execute(
            "SELECT rcept_no FROM disclosures WHERE body IS NOT NULL AND rcept_no IN (%s)" %
            ",".join("?" * len(majors)), [d["rcept_no"] for d in majors])}
        todo = [d for d in majors if d["rcept_no"] not in done]
        if len(todo) > MAX_BODIES:
            warn.add(f"dart: 주요 공시 {len(todo)}건 중 {MAX_BODIES}건만 본문 조회")
            todo = sorted(todo, key=lambda d: (not d["confirmed"], d["rcept_no"]))[:MAX_BODIES]
        failed, gave_up, errs = 0, 0, []
        for d in todo:
            text, err = None, None
            for attempt in range(2):                 # 접수 직후엔 문서가 아직 안 열리는 등 일시 실패가 잦다 → 잠깐 뒤 한 번 더
                try:
                    text = client.document_text(d["rcept_no"], max_chars=BODY_MAX_CHARS)
                    break
                except Exception as e:  # noqa: BLE001
                    err = e
                    if attempt == 0:
                        time.sleep(2)
            stamp = f"{now_kst():%Y-%m-%d %H:%M:%S}"
            if text is not None:
                db.set_disclosure_body(conn, d["rcept_no"], text, stamp)
                continue
            failed += 1
            errs.append(f"{type(err).__name__}: {str(err)[:60]}")
            n = conn.execute("UPDATE disclosures SET body_attempts=COALESCE(body_attempts,0)+1 WHERE rcept_no=? "
                             "RETURNING body_attempts", (d["rcept_no"],)).fetchone()[0]
            if n >= BODY_MAX_ATTEMPTS:               # 여러 실행에 걸쳐 계속 실패(첨부정정·금감원 문서 등 본문 없음) → 빈 본문으로 확정
                db.set_disclosure_body(conn, d["rcept_no"], "", stamp)
                gave_up += 1
        conn.commit()
        print(f"  주요 공시 본문 {len(todo) - failed}/{len(todo)}건 저장"
              + (f" · 실패 {failed}건(다음 실행 때 재시도, {gave_up}건은 {BODY_MAX_ATTEMPTS}회 실패로 포기)" if failed else ""))
        if failed >= 5 and failed > len(todo) * 0.3:
            warn.add(f"dart: 본문 조회 실패 {failed}/{len(todo)}건(다음 실행 때 재시도) — 예: {errs[0]}")

    check_volume(conn, SOURCE, len(merged), warn)
    db.log_run(conn, SOURCE, f"{started:%Y-%m-%d %H:%M:%S}", f"{now_kst():%Y-%m-%d %H:%M:%S}",
               window, len(merged), inserted, warn)
    conn.commit()
    return {"source": SOURCE, "fetched": len(merged), "inserted": inserted, "major": len(majors),
            "warnings": list(warn)}


def _row(rcept_no, api, tday, trow, received, collected_at) -> dict:
    """API 목록 행(api)과 오늘의공시 행(trow)을 합친 공시 한 건."""
    report_nm = api.report_nm if api else trow["report_nm"]
    return {
        "rcept_no": rcept_no,
        "rcept_dt": api.rcept_dt if api else f"{tday:%Y%m%d}",
        "rcept_time": trow["time"] if trow else None,
        "received_at": f"{received:%Y-%m-%d %H:%M:00}" if received else None,
        "corp_name": api.corp_name if api else trow["corp_name"],
        "corp_code": api.corp_code if api else None,
        "stock_code": (api.stock_code if api else "") or None,
        "corp_cls": api.corp_cls if api else trow["corp_cls"],
        "report_nm": report_nm,
        "flr_nm": api.flr_nm if api else trow["flr_nm"],
        "rm": api.rm if api else None,
        "signal": classify_signal(report_nm),
        "confirmed": int(is_confirmed_material(report_nm)),
        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        "collected_at": collected_at,
    }


def zero_suspicious(day: dt.date, now: dt.datetime) -> bool:
    """평일 오늘의공시 0건이 이상한가. DART 접수는 07:30부터라 오늘 날짜는 07:30 전이면 0건이 정상."""
    if day.weekday() >= 5:
        return False
    return day < now.date() or now.time() >= dt.time(7, 30)
