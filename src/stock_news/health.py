"""수집 결과 이상 감지 — 0건, 필드 누락(형식 변경), 평소 대비 급감."""
from __future__ import annotations

import sqlite3
from typing import Iterable

from .db import recent_fetched_counts


class Warnings(list):
    def add(self, msg: str) -> None:
        self.append(msg)
        print(f"  ⚠️  {msg}")


def check_fields(items: Iterable[dict], required: Iterable[str], label: str, warn: Warnings) -> bool:
    """응답 항목에 필수 필드가 있는지. 하나라도 없으면 형식 변경 의심."""
    required = list(required)
    items = list(items)
    if not items:
        return True
    missing = sorted({k for it in items for k in required if k not in it})
    if missing:
        warn.add(f"{label}: 응답 형식 변경 의심 — 필드 없음 {missing}. 사용 가능한 필드: {sorted(items[0].keys())}")
        return False
    return True


QUIET_VOLUME = False      # 장 전 추가(07:50·08:00)처럼 짧은 짧은 시간대는 건수 경고를 하지 않는다(collect.py --session preopen)


def check_volume(conn: sqlite3.Connection, source: str, fetched: int, warn: Warnings) -> None:
    """0건이거나 최근 평균 대비 20% 미만이면 경고."""
    if QUIET_VOLUME:
        return
    if fetched == 0:
        warn.add(f"{source}: 수집 0건 — 엔드포인트/형식 변경 또는 차단 여부 확인 필요")
        return
    past = recent_fetched_counts(conn, source)
    if len(past) >= 3:
        avg = sum(past) / len(past)
        if fetched < avg * 0.2:
            warn.add(f"{source}: 수집 {fetched}건 — 최근 평균 {avg:.0f}건 대비 크게 적음")
