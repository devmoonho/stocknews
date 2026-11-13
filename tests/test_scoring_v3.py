"""채점표 v3 회귀 테스트 — 11-13 정부 'K-온디바이스 AI반도체 로드맵' (오픈엣지테크놀로지·가온칩스).

    python3 -m unittest discover tests

실제 반응(11-13): 오픈엣지테크놀로지 +29%(상한가), 가온칩스 +26%, 같은 테마 다른 종목 +10~15% 이하.
1건은 방향 확인용이지 검증이 아니다. 기대와 다른 결과는 숫자를 맞추려고 고치지 않고 expectedFailure로 남긴다.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_news import db  # noqa: E402
from stock_news.analysis import leader as L  # noqa: E402
from stock_news.analysis import review  # noqa: E402
from stock_news.analysis.matching import ThemeMatcher  # noqa: E402
from stock_news.analysis.scoring import final_score  # noqa: E402
from stock_news.tracking import _base_price, limit_up  # noqa: E402

FX = Path(__file__).parent / "fixtures"
POLICY = json.loads((FX / "policy_ai_chip.json").read_text(encoding="utf-8"))
MEMBERS = json.loads((FX / "theme_ondevice_ai_members.json").read_text(encoding="utf-8"))
DF = json.loads((FX / "reason_df.json").read_text(encoding="utf-8"))


def ranked() -> dict[str, dict]:
    rel = L.Relevance.from_df(DF["n"], DF["df"])
    out = L.rank_members(rel, f"{POLICY['title']}\n{POLICY['summary']}", POLICY["body"], MEMBERS)
    return {m["name"]: m for m in out}


class AliasTest(unittest.TestCase):
    def test_policy_title_hits_ondevice_theme(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO themes (source, theme_code, name) VALUES ('naver', '1', '온디바이스 AI')")
        hits = {h.term for h in ThemeMatcher.from_db(conn).find(POLICY["title"])}
        self.assertIn("AI칩", hits)        # 별칭(config/theme_keyword_aliases.txt) — 테마 이름만으로는 못 잡던 표현


class LeaderTest(unittest.TestCase):
    def test_gaonchips_top2(self):
        self.assertLessEqual(ranked()["가온칩스"]["rank"], 2)

    @unittest.expectedFailure
    def test_openedges_top2(self):
        """알려진 한계(2026-11-13): 오픈엣지의 핵심어 'NPU·엣지'가 정책 본문에 없고 'IP'는 흔한 단어라 7위 안팎."""
        self.assertLessEqual(ranked()["오픈엣지테크놀로지"]["rank"], 2)

    def test_scores(self):
        self.assertEqual([L.leader_score(r) for r in (1, 2, 3, None)], [5, 4, 3, None])
        self.assertEqual([L.cluster_score(p) for p in (9, 7, 3, 2, 1)], [5, 4, 3, 2, 1])
        self.assertEqual(L.cluster_points(gov=1, title_mentions=0, offices=1, material_members=4), 7)


class FinalScoreTest(unittest.TestCase):
    """채점표 v3로 매긴 값(2026-11-13 재채점). 선반영은 4로 가정(실제 값은 score.py가 일봉으로 계산)."""
    OPENEDGE = {"scale": 3, "exclusivity": 2, "novelty": 3, "policy": 4, "diffusion": 2,
                "preprice": 4, "theme_cluster": 4, "leader": 3}
    GAON = {"scale": 2, "exclusivity": 2, "novelty": 3, "policy": 3, "diffusion": 2,
            "preprice": 4, "theme_cluster": 4, "leader": 5}

    def test_at_least_3(self):
        for f in (self.OPENEDGE, self.GAON):
            self.assertGreaterEqual(final_score({k: {"score": v} for k, v in f.items()})[1], 3)


class PriceBasisTest(unittest.TestCase):
    def test_base_price_from_pred_pre(self):
        # 금호건설 11-13: 통합 종가 11,210, 전일 대비 +2,580 → KRX 기준가 8,630 (11-12 통합 종가 9,150은 NXT 애프터마켓 반영)
        self.assertEqual(_base_price({"cur_prc": "11210", "pred_pre": "+2580"}), 8630)
        self.assertEqual(_base_price({"cur_prc": "9150", "pred_pre": "-340"}), 9490)
        self.assertEqual(limit_up(8630), 11210)


class ZeroRuleTest(unittest.TestCase):
    def corr(self, rho, n=30):
        return {k: {"n": n, "rho": 0.1} for k in review.FACTORS} | {"preprice": {"n": n, "rho": rho}}

    def test_zero_after_three_weeks(self):
        z, _ = review.zero_rules(self.corr(-0.2), {"preprice": [-0.1, -0.3]}, set())
        self.assertIn("preprice", z)
        z, _ = review.zero_rules(self.corr(-0.2), {"preprice": [0.1, -0.3]}, set())
        self.assertNotIn("preprice", z)

    def test_restore_after_two_weeks(self):
        z, _ = review.zero_rules(self.corr(0.2), {"preprice": [0.1]}, {"preprice"})
        self.assertNotIn("preprice", z)
        z, _ = review.zero_rules(self.corr(0.2), {"preprice": [-0.1]}, {"preprice"})
        self.assertIn("preprice", z)

    def test_propose_gives_zero_weight_and_sums_to_one(self):
        cur = {k: 1 / len(review.FACTORS) for k in review.FACTORS}
        new, notes, z = review.propose(cur, self.corr(-0.2), 100, {"preprice": [-0.1, -0.3]}, set())
        self.assertEqual(new["preprice"], 0.0)
        self.assertAlmostEqual(sum(new.values()), 1.0, places=3)
        self.assertTrue(all(v >= review.MIN_W - 1e-9 for k, v in new.items() if k != "preprice"))


if __name__ == "__main__":
    unittest.main()
