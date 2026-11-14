"""리포트 '이후 주가'(after_prices)와 DART 0건 경고 시각(07:30).

    python3 -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news import db  # noqa: E402
from stock_news.collectors.dart import zero_suspicious  # noqa: E402
from stock_news.tracking import after_prices  # noqa: E402


class AfterPricesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute("INSERT INTO stocks (code, name, market) VALUES ('000001', '가', 'KOSDAQ')")
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]
        closes = [1000, 1100, 1150, 1120, 1200, 1250, 1300]
        for i, d in enumerate(days):
            base = closes[i - 1] - 10 if i else None          # 기준가 = 전일 KRX 종가(통합 종가와 조금 다름)
            self.conn.execute("INSERT INTO price_daily (code, dt, open, high, low, close, volume, base_price) "
                              "VALUES ('000001', ?, ?, ?, ?, ?, 1000, ?)",
                              (d, closes[i] - 50, closes[i] + 30, closes[i] - 80, closes[i], base))
            self.conn.execute("INSERT INTO index_daily VALUES ('101', ?, 100, 101, 99, 101)", (d,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_complete(self):
        a = after_prices(self.conn, "000001", "2026-11-03")
        self.assertEqual(a["status"], "완료")
        self.assertEqual((a["open"], a["high"], a["low"]), (1050, 1130, 1020))
        self.assertEqual(a["close"], 1090)                      # 다음날 기준가 = KRX 종가
        self.assertEqual(a["close_basis"], "KRX 종가")
        self.assertAlmostEqual(a["gap_pct"], round((1050 / 990 - 1) * 100, 2))
        self.assertAlmostEqual(a["oc_pct"], round((1090 / 1050 - 1) * 100, 2))
        self.assertAlmostEqual(a["excess_pct"], round((1090 / 1050 - 1 - 0.01) * 100, 2))
        self.assertAlmostEqual(a["ret_1d_pct"], round((1150 / 1050 - 1) * 100, 2))
        self.assertAlmostEqual(a["ret_5d_pct"], round((1300 / 1050 - 1) * 100, 2))

    def test_partial_and_pending(self):
        a = after_prices(self.conn, "000001", "2026-11-09")
        self.assertEqual(a["status"], "집계 중")
        self.assertIsNotNone(a["ret_1d_pct"])
        self.assertIsNone(a["ret_3d_pct"])
        last = after_prices(self.conn, "000001", "2026-11-10")
        self.assertEqual(last["status"], "집계 중")
        self.assertIn("통합", last["close_basis"])            # 다음날 기준가가 아직 없음
        none = after_prices(self.conn, "000001", "2026-11-11")
        self.assertEqual(none["status"], "집계 전")
        self.assertIsNone(none["open"])
        self.assertIsNone(none["ret_1d_pct"])


class DartZeroWarnTest(unittest.TestCase):
    def test_before_0730_skips_today(self):
        day = dt.date(2026, 11, 16)                               # 월요일
        self.assertFalse(zero_suspicious(day, dt.datetime(2026, 11, 16, 7, 3)))
        self.assertFalse(zero_suspicious(day, dt.datetime(2026, 11, 16, 7, 29)))
        self.assertTrue(zero_suspicious(day, dt.datetime(2026, 11, 16, 7, 30)))
        self.assertTrue(zero_suspicious(day, dt.datetime(2026, 11, 16, 19, 30)))

    def test_past_weekday_always_and_weekend_never(self):
        self.assertTrue(zero_suspicious(dt.date(2026, 11, 13), dt.datetime(2026, 11, 16, 7, 3)))
        self.assertFalse(zero_suspicious(dt.date(2026, 11, 14), dt.datetime(2026, 11, 16, 7, 3)))


if __name__ == "__main__":
    unittest.main()
