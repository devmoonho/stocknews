"""저녁 종가매매 리포트 — 시간외 시세 계산·간이 리포트·미완성 봉 기준.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news import prices, tracking  # noqa: E402
from stock_news.analysis.fallback import build_items  # noqa: E402
from stock_news.config import report_path, session_dir  # noqa: E402

FX = Path(__file__).parent / "fixtures"
BARS = json.loads((FX / "kumho_minute_20261112.json").read_text(encoding="utf-8"))


class EveningQuoteTest(unittest.TestCase):
    def test_kumho_after_hours(self):
        # 금호건설 11-12: 17:36 공급계약 공시 → KRX 종가 8,630, NXT 애프터마켓 마지막 9,150 (+6.0%)
        q = prices.evening_quote(BARS, "2026-11-12", "2026-11-12 17:36:00")
        self.assertEqual(q["krx_close"], 8630)
        self.assertEqual(q["price"], 9150)
        self.assertAlmostEqual(q["after_hours_pct"], 6.0, places=1)
        self.assertTrue(q["nxt_traded"])
        self.assertGreater(q["after_value"], 1e9)
        self.assertIsNotNone(q["since_disclosure_pct"])

    def test_cut_at_report_time(self):
        # 19:30 리포트라면 그 뒤 봉은 없다고 보고 계산
        early = [b for b in BARS if b["cntr_tm"][8:12] <= "1930"]
        q = prices.evening_quote(early, "2026-11-12")
        self.assertLessEqual(q["as_of"], "19:30")


class PartialDayTest(unittest.TestCase):
    def test_partial_until_2005(self):
        import datetime as dt
        for hhmm, partial in (("19:59", True), ("20:04", True), ("20:06", False)):
            h, m = map(int, hhmm.split(":"))
            fake = dt.datetime(2026, 11, 13, h, m, tzinfo=dt.timezone(dt.timedelta(hours=9)))
            with mock.patch.object(tracking, "now_kst", return_value=fake):
                self.assertEqual(tracking._partial_day() == "2026-11-13", partial, hhmm)


class FallbackTest(unittest.TestCase):
    def test_sorts_by_scale_then_less_reflected(self):
        def stock(code, scale, ah, signal="buy", corr=False):
            return {"code": code, "name": code, "objective": {"scale_score": scale, "scale_basis": "x", "preprice": None},
                    "evening": {"after_hours_pct": ah},
                    "disclosures": [{"rcept_no": code, "report_nm": "단일판매ㆍ공급계약체결", "confirmed": True, "signal": signal,
                                     "figures": {"is_correction": corr}, "scale_score": scale}]}
        c = {"stocks": [stock("A", 3, 1.0), stock("B", 5, 9.0), stock("C", 5, 2.0), stock("D", 5, 0, "sell"),
                        stock("E", 4, 0, corr=True)]}
        self.assertEqual([i["code"] for i in build_items(c)], ["C", "B", "A"])


class PathTest(unittest.TestCase):
    def test_session_paths(self):
        self.assertTrue(str(session_dir("2026-11-13", "evening")).endswith("2026-11-13_evening"))
        self.assertTrue(str(report_path("2026-11-13", "evening")).endswith("evening/2026-11-13.md"))
        self.assertTrue(str(report_path("2026-11-13")).endswith("reports/2026-11-13.md"))


if __name__ == "__main__":
    unittest.main()
