"""테마 선행 예측 1차 — 묶기(사슬 금지·하위 테마 보호·ID 유지)·중복 회피·지표·서열·단계·16:00 마감·예측 채점.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from stock_news import db  # noqa: E402
from stock_news.analysis import theme_forecast, theme_groups as tg, themes_daily as td  # noqa: E402

FX = Path(__file__).parent / "fixtures"
CFG = tg.load_config()
LCFG = td.load_lifecycle()


def codes(a: int, b: int) -> set[str]:
    return {f"{i:06d}" for i in range(a, b + 1)}


class GroupingTest(unittest.TestCase):
    def test_no_chaining(self):
        # A~B 겹침 0.6, B~C 겹침 0.6, A~C 0.2 → A·B 한 묶음, C는 따로 (B를 거쳐 C가 들어오지 않음)
        themes = {("naver", "a"): codes(1, 10), ("naver", "b"): codes(5, 14), ("naver", "c"): codes(9, 18)}
        gs = tg.build_groups(themes, {k: k[1].upper() for k in themes}, CFG)
        by = {g["group_id"]: {t[1] for t in g["themes"]} for g in gs}
        self.assertEqual(by["naver:a"], {"a", "b"})
        self.assertEqual(by["naver:c"], {"c"})

    def test_subtheme_not_swallowed(self):
        # 큰 일반 테마(40종목)가 그 안에 다 들어가는 세부 테마(10종목)를 삼키지 않음 — 크기 비율 0.25 < 0.5
        themes = {("naver", "semi"): codes(0, 39), ("naver", "hbm"): codes(0, 9)}
        gs = tg.build_groups(themes, {}, CFG)
        self.assertEqual(len(gs), 2)

    def test_union_cap(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["groups"]["overlap"] = 0.1
        cfg["groups"]["size_ratio"] = 0.0
        cfg["groups"]["union_cap"] = 1.5
        themes = {("naver", "a"): codes(1, 10), ("naver", "b"): codes(10, 19)}   # 합집합 19 > 10×1.5
        gs = tg.build_groups(themes, {}, cfg)
        self.assertEqual(len(gs[0]["members"]), 10)

    def test_id_kept_when_rep_changes(self):
        # 어제 대표 a(묶음 ID naver:a) → 오늘 더 큰 d가 생겨 대표가 바뀌어도 a가 정확히 하나 들어 있으면 ID 유지
        themes = {("naver", "a"): codes(1, 10), ("naver", "d"): codes(1, 12)}
        gs = tg.build_groups(themes, {}, CFG, prev_reps={"naver:a": "naver:a"})
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["group_id"], "naver:a")
        # 같은 구성으로 다시 묶으면 같은 ID
        gs2 = tg.build_groups(themes, {}, CFG, prev_reps={"naver:d": "naver:a"})
        self.assertEqual(gs2[0]["group_id"], "naver:a")

    def test_name(self):
        themes = {("naver", "a"): codes(1, 10), ("kiwoom", "x"): codes(1, 10)}
        gs = tg.build_groups(themes, {("naver", "a"): "반도체"}, CFG)
        self.assertEqual(gs[0]["name"], "반도체 (+유사 1개)")
        self.assertEqual(gs[0]["group_id"], "naver:a")          # 같은 크기면 네이버가 대표


class PickTopTest(unittest.TestCase):
    ranked = [{"group_id": "g1", "name": "반도체 장비", "leader_code": "A"},
              {"group_id": "g2", "name": "HBM", "leader_code": "A"},          # 대장 같음 → 건너뜀
              {"group_id": "g3", "name": "시스템반도체", "leader_code": "C"},  # 상승 종목 겹침 → 건너뜀
              {"group_id": "g4", "name": "조선", "leader_code": "D"}]
    rising = {"g1": {"A", "B", "X"}, "g3": {"A", "B", "Y"}, "g4": {"D", "E"}}

    def test_dedupe(self):
        top = tg.pick_top(self.ranked, 5, self.rising, CFG, dedupe=True)
        self.assertEqual([t["group_id"] for t in top], ["g1", "g4"])
        self.assertEqual(top[0]["related"], ["HBM", "시스템반도체"])

    def test_no_dedupe(self):
        top = tg.pick_top(self.ranked, 3, self.rising, CFG, dedupe=False)
        self.assertEqual([t["group_id"] for t in top], ["g1", "g2", "g3"])

    def test_rising_sets(self):
        r = tg.rising_sets({"g": [{"code": "a", "change_pct": 7}, {"code": "b", "change_pct": 1},
                                  {"code": "c", "change_pct": -1}]}, 5)
        self.assertEqual(r["g"], {"a", "b"})          # +5% 종목이 2개 미만 → 상승 종목 전체


class MetricsTest(unittest.TestCase):
    def test_median_and_weighted(self):
        ms = [{"code": "a", "change_pct": 30.0, "excess": 29.0, "trade_value": 100},
              {"code": "b", "change_pct": 1.0, "excess": 0.0, "trade_value": 300},
              {"code": "c", "change_pct": 0.0, "excess": -1.0, "trade_value": 100},
              {"code": "d", "change_pct": -2.0, "excess": -3.0, "trade_value": 0}]
        m = td.group_metrics(ms, LCFG)
        self.assertAlmostEqual(m["avg_pct"], 7.25)
        self.assertAlmostEqual(m["median_pct"], 0.5)       # 한 종목 급등에 덜 휘둘림
        self.assertAlmostEqual(m["wavg_pct"], (30 * 100 + 300 + 0) / 500)
        self.assertEqual(m["up5"], 1)
        self.assertAlmostEqual(m["rise_ratio"], 0.5)
        self.assertAlmostEqual(m["concentration"], 1.0)

    def test_rank_labels(self):
        ms = [{"code": "a", "change_pct": 12.0, "trade_value": 9},
              {"code": "b", "change_pct": 29.9, "limit_hit": 1, "first_hit": "10:10", "trade_value": 1},
              {"code": "c", "change_pct": 29.9, "limit_hit": 1, "first_hit": "09:05", "trade_value": 1},
              {"code": "d", "change_pct": 3.0}, {"code": "e", "change_pct": 1.0}, {"code": "f", "change_pct": -1.0}]
        r = td.rank_members(ms)
        self.assertEqual([(x["code"], x["label"]) for x in r],
                         [("c", "대장"), ("b", "2등주"), ("a", "2등주"), ("d", "후발주"), ("e", "후발주"), ("f", None)])

    def test_stages(self):
        def day(med, rise, up5, conc=0.3):
            return {"median_excess": med, "rise_ratio": rise, "up5": up5, "concentration": conc}
        prev = None
        seq = []
        for t in (day(3, .8, 3), day(4, .9, 5), day(3, .7, 2, .8), day(2.5, .7, 2), day(-1, .3, 0)):
            strong, streak, cum, stage = td.stage_of(t, prev, LCFG)
            seq.append(stage)
            prev = {**t, "streak": streak, "cum_excess": cum}
        self.assertEqual(seq, ["태동", "확산", "과열", "과열", "소멸"])

    def test_small_groups_unranked(self):
        groups = [{"group_id": "big", "name": "큰", "members": [f"{i}" for i in range(6)]},
                  {"group_id": "small", "name": "작은", "members": ["x", "y"]}]
        snap = {**{f"{i}": {"change_pct": 3.0, "trade_value": 1} for i in range(6)},
                "x": {"change_pct": 20.0, "trade_value": 1}, "y": {"change_pct": 25.0, "trade_value": 1}}
        rows, _ = td.compute("2026-11-13", groups, snap, {"KOSPI": 1.0, "KOSDAQ": 1.0}, {}, {}, {}, {}, {}, LCFG, 5)
        by = {r["group_id"]: r for r in rows}
        self.assertEqual(by["big"]["rank"], 1)
        self.assertIsNone(by["small"]["rank"])
        self.assertEqual(by["small"]["small"], 1)
        self.assertIsNone(by["small"]["stage"])

    def test_streaks(self):
        self.assertEqual(td.stock_streaks({"a": [1, 2, -1, 3], "b": [-1, 2], "c": [None]}), {"a": 2, "b": 0, "c": 0})

    def test_after_leader(self):
        bars = json.loads((FX / "kumho_minute_20261112.json").read_text(encoding="utf-8"))
        v = td.after_leader("09:30", bars, "2026-11-12")
        self.assertIsNotNone(v)


class CutoffTest(unittest.TestCase):
    def test_snapshot_after_1559_rejected(self):
        import theme_daily
        client = mock.Mock()
        client.call_all.return_value = [{"stk_cd": f"{i:06d}", "cur_prc": "+1000", "flu_rt": "+1.0", "trde_prica": "5"}
                                        for i in range(2500)]
        tz = dt.timezone(dt.timedelta(hours=9))
        with mock.patch.object(theme_daily, "now_kst", return_value=dt.datetime(2026, 11, 16, 15, 59, 40, tzinfo=tz)):
            with self.assertRaises(RuntimeError):
                theme_daily.fetch_snapshot(client, "2026-11-16")
        with mock.patch.object(theme_daily, "now_kst", return_value=dt.datetime(2026, 11, 16, 15, 36, 10, tzinfo=tz)):
            snap = theme_daily.fetch_snapshot(client, "2026-11-16")
        self.assertEqual(len(snap), 2500)
        self.assertEqual(snap["000001"]["trade_value"], 5e6)


class VerifyTest(unittest.TestCase):
    def test_verify_counts_hits(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            day = "2026-11-16"
            for i, (gid, med, leader) in enumerate((("g1", 5.0, "A"), ("g2", 4.0, "B"), ("g3", -1.0, "C")), 1):
                conn.execute("INSERT INTO theme_daily (dt, group_id, group_name, median_excess, rank, small, leader_code) "
                             "VALUES (?,?,?,?,?,0,?)", (day, gid, gid, med, i, leader))
                conn.execute("INSERT INTO theme_members_daily (dt, group_id, code, change_pct, pos, label) "
                             "VALUES (?,?,?,?,1,'대장')", (day, gid, leader, med + 1))
            conn.execute("INSERT INTO theme_members_daily (dt, group_id, code, change_pct, pos, label) "
                         "VALUES (?,?,?,?,2,'2등주')", (day, "g1", "A2", 3))
            # 아침 최종: g1 적중 · 저녁 1차: g3(순위 3, 상위10 안이지만 대장 C가 아닌 X를 찍음) · 기준선: 없는 묶음
            for phase, kind, gid, lp, sec in (("morning", "forecast", "g1", "A", ["A2"]), ("morning", "baseline", "g9", "Z", []),
                                              ("evening", "forecast", "g3", "X", [])):
                conn.execute("INSERT INTO theme_forecasts (date, phase, kind, rank, group_id, group_name, leader_pick, "
                             "second_picks, dedupe) VALUES (?,?,?,1,?,?,?,?,1)", (day, phase, kind, gid, gid, lp, json.dumps(sec)))
            conn.commit()
            res = {(r["phase"], r["kind"]): r for r in theme_forecast.verify(conn, day)}
            self.assertEqual(res[("morning", "forecast")]["top10_hits"], 1)
            self.assertEqual(res[("morning", "forecast")]["leader_hits"], 1)
            self.assertEqual(res[("morning", "forecast")]["second_hits"], 1)
            self.assertEqual(res[("morning", "baseline")]["top10_hits"], 0)
            self.assertEqual(res[("evening", "forecast")]["top10_hits"], 1)
            self.assertEqual(res[("evening", "forecast")]["leader_hits"], 0)
            # 성적 누적은 단계별로 따로
            self.assertEqual(theme_forecast.track_record(conn, "2026-11-17", "evening")["forecast"]["days"], 1)
            self.assertEqual(theme_forecast.track_record(conn, "2026-11-17", "morning")["forecast"]["days"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
