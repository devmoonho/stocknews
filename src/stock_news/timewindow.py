"""수집 시간대: 직전 거래일 15:30 ~ 실행 시점 (KST)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import CONFIG_DIR

KST = dt.timezone(dt.timedelta(hours=9))
MARKET_CLOSE = dt.time(15, 30)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST).replace(tzinfo=None, microsecond=0)


def load_holidays() -> set[dt.date]:
    path = CONFIG_DIR / "krx_holidays.txt"
    out: set[dt.date] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(dt.date.fromisoformat(line))
    return out


def is_trading_day(d: dt.date, holidays: set[dt.date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def prev_trading_day(d: dt.date, holidays: set[dt.date] | None = None) -> dt.date:
    """d 이전(당일 제외)의 가장 가까운 거래일."""
    holidays = load_holidays() if holidays is None else holidays
    d -= dt.timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= dt.timedelta(days=1)
    return d


@dataclass(frozen=True)
class Window:
    start: dt.datetime
    end: dt.datetime

    @property
    def report_date(self) -> dt.date:
        """리포트가 대상으로 하는 장(= 실행일)."""
        return self.end.date()

    def dates(self) -> list[dt.date]:
        """시간대에 걸친 달력 날짜(오래된 순)."""
        d, out = self.start.date(), []
        while d <= self.end.date():
            out.append(d)
            d += dt.timedelta(days=1)
        return out

    def contains(self, t: dt.datetime) -> bool:
        return self.start <= t <= self.end

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M} ~ {self.end:%Y-%m-%d %H:%M}"


def default_window(now: dt.datetime | None = None) -> Window:
    """실행 시점 기준. 장중/장후(15:30 이후)에 돌리면 당일 15:30부터."""
    now = now or now_kst()
    holidays = load_holidays()
    if is_trading_day(now.date(), holidays) and now.time() >= MARKET_CLOSE:
        start_day = now.date()
    else:
        start_day = prev_trading_day(now.date(), holidays)
    return Window(dt.datetime.combine(start_day, MARKET_CLOSE), now)
