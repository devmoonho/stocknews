"""장 전 추가(07:45)와 게시·접수 구간 3분류(장 마감 후 / 장 전 / 장중).

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

from stock_news import db  # noqa: E402
from stock_news.collectors import ministry_press as mp  # noqa: E402
from stock_news.timewindow import Window  # noqa: E402


class SessionOfTest(unittest.TestCase):
    def test_buckets(self):
        f = mp.session_of
        self.assertEqual(f("2026-11-12 18:00:00"), "장 마감 후")
        self.assertEqual(f("2026-11-13 06:59:59"), "장 마감 후")
        self.assertEqual(f("2026-11-13 07:00:00"), "장 전")
        self.assertEqual(f("2026-11-13 07:31:12"), "장 전")
        self.assertEqual(f("2026-11-13 08:00:00"), "장중")
        self.assertEqual(f("2026-11-13 15:29:59"), "장중")
        self.assertEqual(f("2026-11-13 15:30:00"), "장 마감 후")
        self.assertEqual(f("2026-11-14 07:30:00"), "장 마감 후")      # 토요일


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class DisclosureSessionTest(Base):
    def test_first_received(self):
        from stock_news.report import disclosure_session
        for no, t in (("1", "2026-11-13 07:31:00"), ("2", "2026-11-13 09:10:00"), ("3", None)):
            self.conn.execute("INSERT INTO disclosures (rcept_no, rcept_dt, received_at, corp_name, report_nm, collected_at) "
                              "VALUES (?, '20261113', ?, 'x', 'r', 'x')", (no, t))
        self.assertEqual(disclosure_session(self.conn, {"sources": ["rcept:2", "rcept:1"]}), "장 전")
        self.assertEqual(disclosure_session(self.conn, {"sources": ["rcept:2"]}), "장중")
        self.assertEqual(disclosure_session(self.conn, {"sources": ["rcept:3"]}), "장 마감 후")     # 시각 없음
        self.assertIsNone(disclosure_session(self.conn, {"sources": ["article:9"]}))


class PreopenTimingTest(Base):
    """07:45 실행: 07:00 아침 실행 때 목록에 없던 날짜만 아는 글 = 장 전 게시."""

    def test_first_seen_rule(self):
        m = mp.MINISTRIES["fsc"]
        self.conn.execute("INSERT INTO press_seen VALUES ('fsc', 'old', '2026-11-16', 't', '2026-11-16 07:02:10')")
        w = Window(dt.datetime(2026, 11, 16, 7, 3), dt.datetime(2026, 11, 16, 7, 45))
        with mock.patch.object(mp, "now_kst", return_value=dt.datetime(2026, 11, 16, 7, 45)):
            new = mp.timing(self.conn, m, {"id": "new", "date": dt.date(2026, 11, 16)}, w, set())
            old = mp.timing(self.conn, m, {"id": "old", "date": dt.date(2026, 11, 16)}, w, set())
            other = mp.timing(self.conn, mp.MINISTRIES["dapa"], {"id": "x", "date": dt.date(2026, 11, 16)}, w, set())
        self.assertEqual(new, (w.start, "snapshot"))
        self.assertEqual(mp.posted_session(f"{new[0]:%Y-%m-%d %H:%M:%S}", "snapshot"), "장 전")
        self.assertEqual(old[1], "unknown")             # 아침에 이미 있던 글(아침 리포트 몫)
        self.assertEqual(other[1], "unknown")           # 아침에 이 부처 목록을 못 봤으면 판단 불가


class PreopenLoadTest(Base):
    def test_status_and_text(self):
        from stock_news import preopen
        work = Path(self.tmp.name) / "2026-11-16_preopen"
        work.mkdir()
        with mock.patch.object(preopen, "session_dir", return_value=work):
            self.assertEqual(preopen.load(self.conn, "2026-11-16")["status"], "실행 전")
            (work / "status.json").write_text(json.dumps({"status": "없음", "window": {
                "start": "2026-11-16 07:03:00", "end": "2026-11-16 07:45:00"}, "counts": {"disclosures": 7, "policies": 2}}))
            d = preopen.load(self.conn, "2026-11-16")
            self.assertEqual((d["status"], d["items"]), ("없음", []))
            self.assertIn("장 전 추가 없음", preopen.telegram_text(d, "2026-11-16"))
            self.conn.execute("INSERT INTO disclosures (rcept_no, rcept_dt, received_at, corp_name, report_nm, collected_at) "
                              "VALUES ('9', '20261116', '2026-11-16 07:31:12', 'x', '단일판매ㆍ공급계약체결', 'x')")
            (work / "status.json").write_text(json.dumps({"status": "있음", "window": {
                "start": "2026-11-16 07:03:00", "end": "2026-11-16 07:45:00"}, "counts": {"disclosures": 7, "policies": 2}}))
            (work / "scored.json").write_text(json.dumps({"items": [
                {"id": "S-1", "code": "000001", "name": "가", "sentiment": "호재", "final": 4, "final_raw": 3.8,
                 "material": "공급계약 120억", "sources": ["rcept:9"]}]}))
            d = preopen.load(self.conn, "2026-11-16")
            it = d["items"][0]
            self.assertEqual((it["posted_at"], it["basis_labels"], it["final"]), ("2026-11-16 07:31:12", ["공시"], 4))
            t = preopen.telegram_text(d, "2026-11-16")
            self.assertIn("🌅 장 전 추가 11-16 (07:03~07:45)", t)
            self.assertIn("가(000001) 4점 [공시] 07:31", t)


class CandidatesSkipPreopenTest(Base):
    def test_policy_scored_preopen_not_repeated(self):
        from stock_news.analysis import candidates
        rows = [("korea_policy", "korea.kr", "1", "산업통상부", "장 전에 채점한 정책", "2026-11-13 07:20:00"),
                ("korea_policy", "korea.kr", "2", "국토교통부", "장중 정책", "2026-11-13 10:00:00")]
        for r in rows:
            self.conn.execute("INSERT INTO articles (source, office_id, article_id, office_name, title, published_at, "
                              "collected_at) VALUES (?,?,?,?,?,?,'x')", r)
        self.conn.commit()
        aid = self.conn.execute("SELECT id FROM articles WHERE article_id='1'").fetchone()[0]
        w = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 13, 19, 30))
        with mock.patch.object(candidates, "_preopen_gov_ids", return_value={aid}):
            gov = candidates.build(self.conn, w)["gov_policy"]
        self.assertEqual([(g["title"], g["session"]) for g in gov], [("장중 정책", "장중")])
        with mock.patch.object(candidates, "_preopen_gov_ids", return_value=set()):
            gov = candidates.build(self.conn, w)["gov_policy"]
        self.assertIn(("장 전에 채점한 정책", "장 전"), [(g["title"], g["session"]) for g in gov])


class RecheckMergeTest(Base):
    def test_main_and_recheck_merged(self):
        from stock_news import preopen
        work = Path(self.tmp.name) / "d_preopen"
        (work / "recheck").mkdir(parents=True)
        win = lambda a, b: {"start": f"2026-11-16 {a}:00", "end": f"2026-11-16 {b}:00"}  # noqa: E731
        (work / "status.json").write_text(json.dumps({"status": "없음", "window": win("07:03", "07:50"),
                                                      "counts": {"disclosures": 3, "policies": 0}}))
        (work / "recheck" / "status.json").write_text(json.dumps({"status": "있음", "window": win("07:50", "08:00"),
                                                                  "counts": {"disclosures": 2, "policies": 1}}))
        (work / "recheck" / "scored.json").write_text(json.dumps({"items": [
            {"id": "S-1", "code": "000810", "name": "삼성화재", "sentiment": "호재", "final": 3, "final_raw": 2.9,
             "material": "잠정실적", "sources": []}]}))
        with mock.patch.object(preopen, "session_dir", return_value=work):
            d = preopen.load(self.conn, "2026-11-16")
        self.assertEqual(d["status"], "있음")
        self.assertEqual(d["window"], win("07:03", "08:00"))
        self.assertEqual(d["counts"], {"disclosures": 5, "policies": 1})
        self.assertEqual((d["items"][0]["code"], d["items"][0]["recheck"]), ("000810", True))
        self.assertEqual(d["recheck"]["items"], 1)


if __name__ == "__main__":
    unittest.main()
