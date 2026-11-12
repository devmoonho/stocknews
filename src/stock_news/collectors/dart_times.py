"""DART '오늘의 공시' 목록 페이지에서 접수번호 → 접수 시각(HH:MM).

DART Open API(list.json)는 접수 날짜만 주므로, 시각은 이 페이지에서 가져온다.
https://dart.fss.or.kr/dsac001/search.ax?selectDate=YYYY.MM.DD&pageIndex=N&maxResults=100
(robots.txt에서 막혀 있지 않은 경로)
"""
from __future__ import annotations

import datetime as dt
import html
import re

from ..http import Fetcher

URL = "https://dart.fss.or.kr/dsac001/search.ax"
REFERER = "https://dart.fss.or.kr/dsac001/mainAll.do"
_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_RCP = re.compile(r"rcpNo=(\d{14})")
_PAGES = re.compile(r"\[(\d+)/(\d+)\]\s*\[총\s*([\d,]+)건\]")
_CLS = {"유": "Y", "코": "K", "넥": "N", "기": "E"}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_page(page: str) -> tuple[list[dict], int, int]:
    """(행 목록, 전체 페이지 수, 전체 건수)."""
    rows = []
    for tr in _ROW.findall(page):
        tds = _TD.findall(tr)
        m = _RCP.search(tr)
        if len(tds) < 5 or not m:
            continue
        time_s = _text(tds[0])
        if not re.fullmatch(r"\d{2}:\d{2}", time_s):
            continue
        corp_td = _text(tds[1])                      # "유 대양금속"
        cls_mark, _, corp_name = corp_td.partition(" ")
        if cls_mark not in _CLS:
            cls_mark, corp_name = "", corp_td
        rows.append({"rcept_no": m.group(1), "time": time_s, "corp_name": corp_name,
                     "corp_cls": _CLS.get(cls_mark, ""), "report_nm": _text(tds[2]),
                     "flr_nm": _text(tds[3])})
    pm = _PAGES.search(page)
    total_pages = int(pm.group(2)) if pm else 1
    total = int(pm.group(3).replace(",", "")) if pm else len(rows)
    return rows, total_pages, total


def fetch_day(fetcher: Fetcher, day: dt.date, max_pages: int = 40) -> tuple[list[dict], int]:
    """그날 공시 전체(시각 포함)와 페이지에 표시된 총 건수."""
    out: list[dict] = []
    page_no, total_pages, total = 1, 1, 0
    while page_no <= min(total_pages, max_pages):
        page = fetcher.get_text(URL, {"selectDate": f"{day:%Y.%m.%d}", "pageIndex": page_no, "mdayCnt": 0,
                                      "maxResults": 100, "maxLinks": 10, "sort": "", "series": "",
                                      "currentPage": page_no}, referer=REFERER)
        rows, total_pages, total = parse_page(page)
        out.extend(rows)
        page_no += 1
    return out, total
