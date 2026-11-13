"""채점표 v2 회귀 테스트 — 11-12 장 마감 후 공시 3건(금호건설·바이브컴퍼니·케이씨).

    python3 -m unittest discover tests

실제 반응(11-13): 금호건설 상한가 → 해제 → 재도달, 바이브컴퍼니 상한가, 케이씨 약 +4~5%.
목표: 점수 순서 금호건설 ≥ 바이브컴퍼니 > 케이씨. (3건은 방향 확인용이지 통계적 검증이 아니다)
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news import db, prices  # noqa: E402
from stock_news.analysis import followup  # noqa: E402
from stock_news.analysis.disclosure_parse import _by_mcap, parse, scale_score  # noqa: E402
from stock_news.analysis.scoring import final_score  # noqa: E402
from stock_news.timewindow import Window  # noqa: E402
from stock_news.tracking import intraday_pattern, limit_up  # noqa: E402

FX = Path(__file__).parent / "fixtures"
META = json.loads((FX / "disclosures.json").read_text(encoding="utf-8"))
# 시가총액 = 상장주식수 × 11-12 종가(키움 종목 마스터 lastPrice)
CAP = {"kumho": 37_259_974 * 8_630, "kc": 11_020_535 * 40_250, "vaiv": 13_154_712 * 1_820}


def P(name: str, cap: float) -> dict:
    return parse(META[name]["report_nm"], (FX / f"{name}.txt").read_text(encoding="utf-8"), cap)


class ParseTest(unittest.TestCase):
    def test_kumho_contract(self):
        f = P("kumho_contract", CAP["kumho"])
        self.assertEqual(f["kind"], "공급계약")
        self.assertEqual(f["amount"], 372_000_000_000)
        self.assertEqual(f["revenue_ratio_pct"], 18.43)
        self.assertEqual((f["start"], f["end"]), ("2026-11-12", "2028-12-31"))
        self.assertAlmostEqual(f["years"], 2.14, places=2)
        self.assertAlmostEqual(f["annual_revenue_ratio_pct"], 8.61, places=1)   # 연환산 → 매출 기준 2점
        self.assertGreater(f["market_cap_ratio_pct"], 100)                       # 시총 대비 → 5점
        self.assertEqual(f["counterparty_type"], "대학·병원")
        self.assertFalse(f["advance_payment"])
        self.assertEqual(scale_score(f), 5)

    def test_kc_trust(self):
        f = P("kc_trust", CAP["kc"])
        self.assertEqual((f["kind"], f["method"]), ("자사주취득", "신탁"))
        self.assertEqual(f["amount"], 10_000_000_000)
        self.assertEqual((f["start"], f["end"]), ("2026-11-13", "2027-05-12"))
        self.assertEqual(f["burn"], "예정")
        self.assertAlmostEqual(f["market_cap_ratio_pct"], 2.25, places=2)      # 3점 구간 − 신탁 1
        self.assertEqual(scale_score(f), 2)

    def test_vaiv_rights_and_control(self):
        a = P("vaiv_rights_acquirer", CAP["vaiv"])
        b = P("vaiv_rights_fund", CAP["vaiv"])
        c = P("vaiv_control", CAP["vaiv"])
        self.assertEqual(a["funds"], {"운영자금": 4_999_998_990})
        self.assertEqual((a["issue_price"], a["base_price"], a["discount_pct"]), (1302, 1446, -10.0))
        self.assertEqual((a["payment_date"], a["lockup"], a["allottee_funding"]), ("2026-12-11", "1년", "차입금"))
        self.assertIn("에이아이블레이저", a["allottee"])
        self.assertTrue(b["allottee_is_fund"] and b["third_party_securities"] and b["target_undecided"])
        self.assertEqual((b["payment_date"], b["lockup"]), ("2026-11-20", "6개월"))
        # 경영권: 양수도 대금은 규모 제외, 양수인 재무
        self.assertEqual(c["kind"], "경영권변경")
        self.assertEqual(c["transfer_amount"], 2_620_000_000)
        self.assertIsNone(scale_score(c))
        self.assertEqual(c["acquirer_founded"], "2024-07-08")
        self.assertEqual(c["acquirer_fin_mil"]["equity"], -55)                   # 자본잠식
        self.assertEqual(c["closing_date"], "2026-12-11")
        inflow = a["amount"] + b["amount"]                                        # 회사 유입 약 80억
        self.assertEqual(_by_mcap(inflow / CAP["vaiv"] * 100), 5)


class PrepriceTest(unittest.TestCase):
    def test_rules(self):
        ctx = lambda r5, r20: {"asof": "2026-11-12", "ret5_pct": r5, "ret20_pct": r20}  # noqa: E731
        self.assertEqual(prices.preprice(ctx(2, 5), "정상", "0")["score"], 5)
        self.assertEqual(prices.preprice(ctx(8, 5), "정상", "0")["score"], 4)
        self.assertEqual(prices.preprice(ctx(12, -6), "정상", "0")["score"], 3)
        self.assertEqual(prices.preprice(ctx(3, 3), "투자주의", "0")["score"], 2)
        self.assertEqual(prices.preprice(ctx(3, 3), "정상", "3")["score"], 2)
        self.assertEqual(prices.preprice(ctx(35, 40), "정상", "0")["score"], 1)
        self.assertEqual(prices.preprice(ctx(3, 3), "투자경고", "0")["score"], 1)
        self.assertIsNone(prices.preprice(None, "정상", "0"))


class OrderTest(unittest.TestCase):
    """객관 점수(규모·선반영)는 파서·다이제스트 값, 나머지는 채점표 v2로 매긴 값(2026-11-13 재채점)."""
    ITEMS = {
        "금호건설": {"scale": 5, "exclusivity": 2, "novelty": 5, "quality": 4, "preprice": 3},
        "바이브컴퍼니": {"scale": 5, "exclusivity": 1, "novelty": 4, "quality": 1, "preprice": 2},
        "케이씨": {"scale": 2, "exclusivity": 1, "novelty": 5, "quality": 2, "preprice": 2},
    }

    def test_order(self):
        raw = {n: final_score({k: {"score": v} for k, v in f.items()})[0] for n, f in self.ITEMS.items()}
        self.assertGreaterEqual(raw["금호건설"], raw["바이브컴퍼니"])
        self.assertGreater(raw["바이브컴퍼니"], raw["케이씨"])
        self.assertGreater(raw["금호건설"] - raw["케이씨"], 1.0)        # v1에서는 0.21 차이였음


class IntradayTest(unittest.TestCase):
    def test_limit_price(self):
        self.assertEqual(limit_up(8630), 11210)     # 금호건설 11-13 상한가
        self.assertEqual(limit_up(1820), 2365)      # 바이브컴퍼니
        self.assertEqual(limit_up(41600), 54000)

    def test_release_and_rehit(self):
        def bar(t, cur, high):
            return {"cntr_tm": f"20261113{t}00", "cur_prc": f"+{cur}", "high_pric": f"+{high}",
                    "pred_pre": f"+{cur - 8630}"}
        bars = [bar("0900", 9500, 9600), bar("0901", 11210, 11210), bar("0902", 11000, 11100),
                bar("0903", 10900, 11000), bar("0904", 11210, 11210), bar("0905", 11210, 11210)]
        p = intraday_pattern(bars[::-1], "2026-11-13")
        self.assertEqual((p["hit"], p["first_hit"], p["releases"], p["rehit"], p["closed_at_limit"]),
                         (1, "09:01", 1, 1, 1))
        self.assertEqual(p["minutes_at_limit"], 3)


class FollowupTest(unittest.TestCase):
    def test_register_and_scan(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        f = P("vaiv_rights_fund", CAP["vaiv"])
        cands = {"stocks": [{"code": "301300", "disclosures": [{"rcept_no": "20261112000665", "figures": f}]}]}
        items = [{"code": "301300", "name": "바이브컴퍼니", "sentiment": "호재", "sources": ["rcept:20261112000665"]}]
        self.assertEqual(followup.register(conn, "2026-11-13", items, cands), 1)
        conn.execute("INSERT INTO disclosures (rcept_no, rcept_dt, received_at, stock_code, report_nm, collected_at) "
                     "VALUES ('20261120000001', '20261120', '2026-11-20 17:00:00', '301300', "
                     "'[기재정정]주요사항보고서(유상증자결정)', 'x')")
        w = Window(dt.datetime(2026, 11, 20, 15, 30), dt.datetime(2026, 11, 21, 7, 0))
        res = followup.scan(conn, w)
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["events"][0]["new_status"], "정정")
        # 예정일이 지나도 아무 공시가 없으면 기한경과
        w2 = Window(dt.datetime(2026, 11, 24, 15, 30), dt.datetime(2026, 11, 25, 7, 0))
        conn.execute("UPDATE followups SET status='대기'")
        res = followup.scan(conn, w2)
        self.assertTrue(res["events"][0]["overdue"])
        self.assertEqual(res["events"][0]["new_status"], "기한경과")


if __name__ == "__main__":
    unittest.main()
