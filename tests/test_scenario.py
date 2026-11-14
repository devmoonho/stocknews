"""일일 손익 시나리오 — 경로 판정·슬리피지·트레일(%/호가)·진입 시점·유동성·아웃오브샘플·범위 확장·안정 구간.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news.analysis import scenario as sc  # noqa: E402


def cfg(**kw) -> dict:
    c = sc.load_config()
    c.update({"cost_pct": 0.0, "stop_slippage_pct": 0.0, "nxt_entry_slippage_pct": 0.0, "krx_entry_slippage_pct": 0.0})
    c.update(kw)
    return c


def b(t, o, h, l, c, v=100000):
    return [t, float(o), float(h), float(l), float(c), float(v)]


def run(bars, stop, take, mode="full", entry=10000.0, close=None, **kw):
    return sc.Path(entry, bars, cfg(**kw), close).run(stop, take, mode)


class PathTest(unittest.TestCase):
    def test_stop_first(self):
        bars = [b("0900", 10000, 10050, 9950, 10000), b("0901", 10000, 10010, 9790, 9800), b("0902", 9800, 10300, 9800, 10250)]
        r, amb, why = run(bars, -2, 2)
        self.assertAlmostEqual(r, -0.02)
        self.assertEqual((amb, why), (False, "손절"))

    def test_take_first(self):
        bars = [b("0900", 10000, 10210, 9950, 10200), b("0901", 10200, 10200, 9700, 9750)]
        r, _, why = run(bars, -2, 2)
        self.assertAlmostEqual(r, 0.02)
        self.assertEqual(why, "익절")

    def test_same_bar_both_is_stop_and_ambiguous(self):
        bars = [b("0900", 10000, 10300, 9700, 10000)]
        r, amb, why = run(bars, -2, 2)
        self.assertAlmostEqual(r, -0.02)
        self.assertTrue(amb)
        self.assertEqual(why, "손절")

    def test_gap_down_fills_at_open(self):
        bars = [b("0900", 10000, 10050, 9950, 10000), b("0901", 9500, 9600, 9400, 9550)]
        r, _, _ = run(bars, -2, 5)
        self.assertAlmostEqual(r, 9500 / 10000 - 1)

    def test_entry_bar_stop_immediately(self):
        bars = [b("0900", 10000, 10000, 9700, 9900), b("0901", 9900, 11000, 9900, 11000)]
        r, _, why = run(bars, -2, 5)
        self.assertAlmostEqual(r, -0.02)
        self.assertEqual(why, "손절")

    def test_no_reentry_after_stop(self):
        base = [b("0900", 10000, 10000, 9790, 9800)]
        r1, _, _ = run(base + [b("0901", 9800, 9800, 9700, 9700)], -2, 5)
        r2, _, _ = run(base + [b("0901", 9800, 12000, 9800, 12000)], -2, 5)
        self.assertEqual(r1, r2)

    def test_close_exit_uses_krx_close(self):
        bars = [b("0900", 10000, 10100, 9900, 10050), b("1519", 10050, 10100, 10000, 10080)]
        r, _, why = run(bars, -2, 5, close=10120)          # 15:30 봉이 빠져도 스냅샷 종가로 청산
        self.assertAlmostEqual(r, 0.012)
        self.assertEqual(why, "종가")

    def test_costs_and_stop_slippage(self):
        bars = [b("0900", 10000, 10050, 9700, 9800)]
        r, _, _ = run(bars, -2, 5, cost_pct=0.25, stop_slippage_pct=0.2)
        self.assertAlmostEqual(r, -0.02 - 0.0025 - 0.002)
        bars = [b("0900", 10000, 10600, 9950, 10500)]
        r, _, _ = run(bars, -2, 5, cost_pct=0.25, stop_slippage_pct=0.2)
        self.assertAlmostEqual(r, 0.05 - 0.0025)            # 익절엔 손절 슬리피지 없음

    def test_gap_stop_also_slippage(self):
        bars = [b("0900", 10000, 10050, 9950, 10000), b("0901", 9500, 9600, 9400, 9550)]
        r, _, _ = run(bars, -2, 5, stop_slippage_pct=0.2)
        self.assertAlmostEqual(r, -0.05 - 0.002)

    def test_half_take_then_pct_trail(self):
        bars = [b("0900", 10000, 10250, 9950, 10200), b("0901", 10200, 10600, 10400, 10550),
                b("0902", 10550, 10560, 10300, 10400), b("0903", 10400, 10450, 10200, 10250)]
        r, _, why = run(bars, -2, 2, "pct3", stop_slippage_pct=0.2)
        trail = 10600 * 0.97
        self.assertAlmostEqual(r, 0.5 * 0.02 + 0.5 * (trail / 10000 - 1 - 0.002))
        self.assertEqual(why, "익절+트레일")

    def test_half_take_then_breakeven(self):
        bars = [b("0900", 10000, 10210, 9950, 10200), b("0901", 10200, 10210, 9990, 10000)]
        r, _, _ = run(bars, -2, 2, "pct5")                  # 고점 10210의 5% 아래(9699)보다 본전(10000)이 높다
        self.assertAlmostEqual(r, 0.5 * 0.02 + 0.5 * 0.0)

    def test_half_take_no_trail_hit_closes(self):
        bars = [b("0900", 10000, 10210, 9950, 10200), b("0901", 10200, 10300, 10150, 10300)]
        r, _, _ = run(bars, -2, 2, "pct5", close=10300)
        self.assertAlmostEqual(r, 0.5 * 0.02 + 0.5 * 0.03)


class TickTrailTest(unittest.TestCase):
    def test_ticks_cross_price_band(self):
        # 2,010 → 호가 5원: 2,005 · 2,000 → 2,000 아래는 1원: 1,999 · 1,998 · 1,997
        self.assertEqual(sc.trail_level(2010, "tick5"), 1997)
        # 5,020(호가 10원) → 5,010 · 5,000 → 5,000 아래는 5원: 4,995 · 4,990 · 4,985
        self.assertEqual(sc.trail_level(5020, "tick5"), 4985)

    def test_ticks_simple(self):
        self.assertEqual(sc.trail_level(30000, "tick10"), 29500)   # 20,000~50,000: 50원
        # 20,030은 호가 50원 구간 → 호가에 맞춰 20,000 → 아래는 10원: 19,990 · 19,980 · 19,970 · 19,960 · 19,950
        self.assertEqual(sc.trail_level(20030, "tick5"), 19950)
        self.assertAlmostEqual(sc.trail_level(10000, "pct3"), 9700)


class EntryTest(unittest.TestCase):
    def md(self, nxt=None, krx=None, prev=10000.0):
        return {"KRX": krx or [b("0900", 10000, 10100, 9900, 10000)], "NXT": nxt or [], "prev_close": prev}

    def test_entry_slippage(self):
        c = cfg(nxt_entry_slippage_pct=0.3, krx_entry_slippage_pct=0.1)
        e = sc.make_entries(self.md(nxt=[b("0800", 10000, 10100, 9950, 10050, 10000)]), c)
        self.assertAlmostEqual(e["nxt_early"]["price"], 10030)
        self.assertAlmostEqual(e["krx_open"]["price"], 10010)
        self.assertEqual(e["nxt_late"]["status"], "na")     # 08:40 봉 없음

    def test_nxt_path_premarket_take(self):
        c = cfg()
        nxt = [b("0800", 10000, 10050, 9950, 10000, 10000), b("0801", 10000, 10600, 10000, 10500, 10000),
               b("0849", 10500, 10500, 10400, 10450, 10000)]
        krx = [b("0900", 9000, 9100, 8000, 8100)]
        e = sc.make_entries(self.md(nxt=nxt, krx=krx), c)["nxt_early"]
        self.assertEqual([x[0] for x in e["bars"]], ["0800", "0801", "0849", "0900"])
        r, _, why = sc.Path(e["price"], e["bars"], c, e["close"]).run(-5, 5, "full")
        self.assertAlmostEqual(r, 0.05)                      # 프리마켓에서 익절 체결
        self.assertEqual(why, "익절")

    def test_nxt_late_entry_from_0840(self):
        nxt = [b("0800", 10000, 10000, 10000, 10000, 10000), b("0840", 10100, 10200, 10050, 10150, 10000)]
        e = sc.make_entries(self.md(nxt=nxt), cfg())["nxt_late"]
        self.assertEqual(e["raw_open"], 10100)
        self.assertEqual(e["bars"][0][0], "0840")

    def test_illiquid_low_and_high_price(self):
        c = cfg()
        # 저가주: 1,000원 × 5,000주 = 500만 원 → 부족 (주식 수는 많아도)
        low = sc.make_entries({"KRX": [b("0900", 1000, 1010, 990, 1000)], "prev_close": 1000.0,
                               "NXT": [b("0800", 1000, 1000, 1000, 1000, 5000)]}, c)
        self.assertEqual(low["nxt_early"]["status"], "illiquid")
        # 고가주: 500,000원 × 30주 = 1,500만 원 → 충분 (주식 수는 적어도)
        high = sc.make_entries({"KRX": [b("0900", 500000, 501000, 499000, 500000)], "prev_close": 500000.0,
                                "NXT": [b("0800", 500000, 500000, 500000, 500000, 30)]}, c)
        self.assertEqual(high["nxt_early"]["status"], "ok")
        self.assertAlmostEqual(sc.amount(b("0800", 0, 1200, 900, 900, 100)), 100000)   # 대표가 (1200+900+900)/3=1000

    def test_krx_open_never_illiquid(self):
        e = sc.make_entries({"KRX": [b("0900", 1000, 1000, 1000, 1000, 1)], "NXT": [], "prev_close": 1000.0}, cfg())
        self.assertEqual(e["krx_open"]["status"], "ok")

    def test_open_at_limit_excluded(self):
        e = sc.make_entries({"KRX": [b("0900", 13000, 13000, 13000, 13000)], "NXT": [], "prev_close": 10000.0}, cfg())
        self.assertEqual(e["krx_open"]["status"], "limit")

    def test_illiquid_excluded_from_stats(self):
        c = cfg()
        t = {"date": "d", "grp": "scored", "sentiment": "호재", "final": 3, "basis": "공시",
             "entries": sc.make_entries({"KRX": [b("0900", 1000, 1010, 990, 1000)], "prev_close": 1000.0,
                                         "NXT": [b("0800", 1000, 1000, 1000, 1000, 5000)]}, c)}
        g = sc.Grid([t], c)
        self.assertEqual(g.agg([0], "nxt_early", (-2, 2, "full"))["n"], 0)
        self.assertEqual(g.agg([0], "krx_open", (-2, 2, "full"))["n"], 1)


def trade(date, bars, final=3, basis="공시", sentiment="호재", grp="scored", c=None):
    c = c or cfg()
    return {"date": date, "code": "000000", "name": "x", "grp": grp, "sentiment": sentiment, "final": final,
            "basis": basis, "entries": sc.make_entries({"KRX": bars, "NXT": [], "prev_close": bars[0][1]}, c)}


UP = [b("0900", 10000, 10000, 9990, 10000), b("0930", 10000, 11000, 10000, 11000), b("1000", 11000, 11600, 11000, 11500)]
DOWN = [b("0900", 10000, 10000, 9990, 10000), b("0930", 10000, 10000, 9300, 9300), b("1000", 9300, 9400, 9000, 9100)]
DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]


class AnalyzeTest(unittest.TestCase):
    def test_reference_only_when_all_in_week(self):
        res = sc.analyze([trade("2026-11-10", UP)], DAYS, cfg())
        self.assertTrue(res["segments"]["호재 전체"]["reference_only"])
        self.assertIn("선정 구간 표본 없음", sc.render(res, "2026-11-10"))

    def test_oos_split_and_overfit(self):
        tr = [trade("2026-11-02", UP), trade("2026-11-03", UP)] + [trade(d, DOWN) for d in DAYS[2:]]
        res = sc.analyze(tr, DAYS, cfg())
        r = res["segments"]["호재 전체"]
        self.assertFalse(r["reference_only"])
        self.assertEqual(r["basis"]["n"], 2)              # 선정 = 최근 1주(11-04~11-10) 제외 → 2건만
        self.assertEqual(r["oos"]["n"], 5)
        self.assertTrue(r["overfit"])                     # 선정 구간은 상승, 검증 1주는 하락
        self.assertIn("과적합 의심", sc.render(res, DAYS[-1]))

    def test_range_expands_at_edge(self):
        tr = [trade("2026-11-02", UP), trade("2026-11-03", UP), trade("2026-11-10", UP)]
        res = sc.analyze(tr, DAYS, cfg())
        r = res["segments"]["호재 전체"]
        self.assertGreater(r["combo"][1], 10)             # 최적 익절이 +10% 끝 → 확장
        self.assertTrue(any("호재 전체" in n and "확장" in n for n in res["notes"]))
        self.assertEqual(res["n_combos"], 1134)           # 기본 그리드 조합 수는 그대로

    def test_segments(self):
        self.assertEqual(sc.segments_of({"grp": "scored", "sentiment": "호재", "final": 4, "basis": "공시,뉴스"}),
                         ["호재 전체", "4점↑", "공시", "뉴스"])
        self.assertEqual(sc.segments_of({"grp": "control", "sentiment": None, "final": None, "basis": "공시"}), ["비교 기준"])
        self.assertEqual(sc.segments_of({"grp": "scored", "sentiment": "악재", "final": None, "basis": "공시"}), ["악재"])

    def test_confidence_and_sample_warning(self):
        c = cfg()
        self.assertEqual([sc.confidence(n, c) for n in (29, 30, 99, 100)], ["낮음", "보통", "보통", "높음"])
        self.assertIn("표본 부족", sc._s(sc.stats([0.01] * 29), c))
        self.assertNotIn("표본 부족", sc._s(sc.stats([0.01] * 30), c))

    def test_stable_zone_connected_only(self):
        stops, takes = [-2, -3, -4, -5], [2, 3, 4, 5]
        good = {(-2, 2), (-2, 3), (-3, 2), (-5, 5)}          # (-5,5)는 좋지만 떨어져 있음
        table = {(s, t, "full"): {"n": 10, "mean": 0.02 if (s, t) in good else -0.01} for s in stops for t in takes}
        z = sc.stable_zone(table, stops, takes, (-2, 2, "full"), 0.5)
        self.assertEqual(z["cells"], 3)
        self.assertEqual(z["stop"], [-2, -3])
        self.assertEqual(z["take"], [2, 3])

    def test_changes_summary(self):
        segs = {"3점": {"entry": "krx_open", "combo": (-4, 6, "pct3"), "zone": {}, "reference_only": False}}
        prev = {"3점": {"entry": "krx_open", "stop": -5, "take": 6, "mode": "full", "zone": None}}
        ch = sc._changes(segs, prev)
        self.assertEqual(len(ch), 1)
        self.assertIn("손절 -5→-4%", ch[0])
        self.assertIn("절반 익절+트레일 3%", ch[0])


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ 개별 매매(trades)·재계산
class TradesTest(unittest.TestCase):
    def test_first_hit(self):
        self.assertEqual(sc.first_hit([b("0900", 100, 110, 99, 105), b("0901", 105, 106, 90, 92)]), ("high", None))
        self.assertEqual(sc.first_hit([b("0900", 100, 101, 90, 95), b("0901", 95, 120, 95, 118)]), ("low", None))
        fh, why = sc.first_hit([b("0900", 100, 120, 80, 100)])
        self.assertIsNone(fh)
        self.assertIn("같은 1분봉", why)
        self.assertEqual(sc.first_hit([]), (None, "분봉 없음"))

    def test_rows_and_recompute_same_result(self):
        c = cfg(cost_pct=0.25, stop_slippage_pct=0.2, nxt_entry_slippage_pct=0.3, krx_entry_slippage_pct=0.1)
        nxt = [b("0800", 10000, 10100, 9950, 10050, 5000), b("0849", 10050, 10080, 10000, 10050, 5000)]
        tr = [{**trade(d, bars, c=c), "code": f"00000{i}"} for i, (d, bars) in
              enumerate([("2026-11-02", UP), ("2026-11-03", DOWN), ("2026-11-10", UP)])]
        tr[0]["entries"] = sc.make_entries({"KRX": UP, "NXT": nxt, "prev_close": 10000.0}, c)
        res = sc.analyze(tr, DAYS, c)
        rows = sc.trade_rows(tr, res, c)
        r0 = next(r for r in rows if r["code"] == "000000" and r["entry"] == "krx_open")
        self.assertEqual((r0["open"], r0["high"], r0["low"], r0["close"]), (10000, 11600, 9990, 11500))
        self.assertAlmostEqual(r0["high_pct"], 16.0)
        self.assertEqual(r0["entry_type"], "KRX 09:00")
        self.assertEqual(r0["first_hit"], "low")
        self.assertIsNotNone(r0["exit"]["ret_pct"])
        self.assertTrue(any(r["entry"] == "nxt_early" for r in rows))
        # bars를 붙인 행만으로 다시 계산 → 같은 추천·같은 성과
        for r in rows:
            e = next(t for t in tr if t["code"] == r["code"])["entries"][r["entry"]]
            r["bars"] = e.get("bars")
        res2 = sc.analyze(sc.trades_from_rows(rows), DAYS, c)
        for seg in sc.REC_SEGMENTS:
            a, b2 = res["segments"][seg], res2["segments"][seg]
            self.assertEqual(a.get("combo"), b2.get("combo"), seg)
            self.assertEqual(a.get("entry"), b2.get("entry"), seg)
            if a.get("basis"):
                self.assertAlmostEqual(a["basis"]["mean"], b2["basis"]["mean"])
