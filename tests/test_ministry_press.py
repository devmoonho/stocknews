"""부처 보도자료 수집 — 부처별 목록 파서(실제 페이지 조각), 엠바고 파싱, 게시 시각 판정, 정책브리핑 중복 묶기, 근거 표시.

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

FIX = ROOT / "tests" / "fixtures" / "ministry"


class ListParserTest(unittest.TestCase):
    """2026-11-14에 받은 실제 목록 페이지 조각 — 부처마다 id·제목·날짜를 읽는지."""

    def check(self, key: str, n: int, first_title_part: str, first_date: str, dept: str | None = None):
        raw = (FIX / (f"{key}_list.json" if key == "nssc" else f"{key}_list.html")).read_text(encoding="utf-8")
        items = mp.MINISTRIES[key].parse_list(raw)
        self.assertEqual(len(items), n, key)
        self.assertIn(first_title_part, items[0]["title"])
        self.assertEqual(items[0]["date"], dt.date.fromisoformat(first_date))
        self.assertTrue(all(x["id"] and x["url"].startswith("http") and x["date"] for x in items), key)
        if dept is not None:
            self.assertEqual(items[0]["dept"], dept)
        return items

    def test_motir(self):
        items = self.check("motir", 10, "12차 최고가격", "2026-11-13", "석유산업과")
        self.assertIn("18시엠바고", items[0]["attachments"][0]["name"])        # 엠바고가 첨부명에

    def test_msit_js_dates(self):
        self.check("msit", 10, "구글사 지도", "2026-11-13")                    # 날짜는 JS로 채워짐

    def test_molit(self):
        self.check("molit", 10, "제설대책", "2026-11-13")

    def test_dapa(self):
        self.check("dapa", 10, "폴란드 군비청", "2026-11-12")

    def test_mfds(self):
        items = self.check("mfds", 10, "코코아분말", "2026-11-13", "수입유통안전과")
        self.assertTrue(items[0]["attachments"])

    def test_mohw_robots_scope(self):
        items = self.check("mohw", 15, "의료데이터", "2026-11-13", "의료인공지능데이터정책과")
        # robots.txt는 이 게시판 경로만 허용 — 상세 URL이 허용 접두어로 시작해야 한다
        self.assertTrue(all(x["url"].startswith("https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027") for x in items))

    def test_mof(self):
        items = self.check("mof", 10, "돔류", "2026-11-13", "수산정책과")
        self.assertFalse(items[0]["attachments"][0]["name"].startswith("pdf파일"))

    def test_mcee(self):
        self.check("mcee", 10, "원자력공동조정위원회", "2026-11-13")

    def test_mofe(self):
        self.check("mofe", 10, "최근 경제동향", "2026-11-13", "경제분석과")

    def test_fsc(self):
        items = self.check("fsc", 10, "씨티그룹", "2026-11-13", "글로벌금융과")
        self.assertTrue(all(a["url"].startswith("https://www.fsc.go.kr/comm/getFile") for a in items[0]["attachments"]))

    def test_nssc_json_has_time(self):
        items = self.check("nssc", 4, "한빛 원전", "2026-11-03", "원자력안전과")
        self.assertEqual(items[0]["posted_at"], dt.datetime(2026, 11, 3, 14, 10, 23))
        self.assertTrue(items[0]["body"])


class EmbargoTest(unittest.TestCase):
    REF = dt.date(2026, 11, 13)

    def p(self, *texts):
        return mp.parse_embargo(list(texts), self.REF)

    def test_table_header(self):                 # 과기정통부 .md 머리 표
        t, at = self.p("| 보도 시점 | 2026. 11. 13.(금) 13:00 (2026. 11. 14.(토) 조간) | 배포 | 2026. 11. 13.(금) 9:00 |")
        self.assertEqual(at, dt.datetime(2026, 11, 13, 13, 0))

    def test_day_hour_embargo(self):             # 산업부 첨부명
        self.assertEqual(self.p("(참고자료, 13(금) 18시엠바고)석유산업과")[1], dt.datetime(2026, 11, 13, 18))

    def test_month_rollover(self):
        t, at = mp.parse_embargo(["보도시점 : 1.2.(금) 06:00"], dt.date(2026, 12, 31))
        self.assertEqual(at, dt.datetime(2027, 1, 2, 6, 0))

    def test_after_phrase(self):
        self.assertEqual(self.p("11. 15.(일) 12:00 이후 보도 바랍니다")[1], dt.datetime(2026, 11, 15, 12))

    def test_text_only(self):
        self.assertEqual(self.p("261113 즉시 (보도참고) 구글"), ("즉시", None))
        self.assertEqual(self.p("| 보도 시점 | 배포 시점 | 배포 | 2026. 11. 13.(금) |"), ("즉시", None))
        self.assertEqual(self.p("11.14.(토) 조간"), ("11.14.(토) 조간", None))
        self.assertEqual(self.p("[26.11.13.(금) 보도참고자료] 식약처"), (None, None))

    def test_title_first_wins_over_body_time(self):
        # 명시 시각이 있는 표기가 '조간' 단독 표기보다 우선
        t, at = self.p("기업과 대학 (조간)", "보도시점 11.14.(토) 06:00")
        self.assertEqual(at, dt.datetime(2026, 11, 14, 6))


class TimingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.m = mp.MINISTRIES["motir"]
        self.evening = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 13, 19, 30))
        self.morning = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 16, 7, 0))   # 금 15:30 ~ 월 07:00
        self.now = mock.patch.object(mp, "now_kst", return_value=dt.datetime(2026, 11, 13, 19, 30))
        self.now.start()

    def tearDown(self):
        self.now.stop()
        self.conn.close()
        self.tmp.cleanup()

    def it(self, d="2026-11-13", **kw):
        return {"id": kw.pop("id", "1"), "date": dt.date.fromisoformat(d), **kw}

    def snap(self, ids):
        self.conn.execute("INSERT INTO press_snapshots VALUES ('2026-11-13', '2026-11-13 15:30:05', '{}')")
        for i in ids:
            self.conn.execute("INSERT INTO press_seen VALUES ('motir', ?, '2026-11-13', 't', '2026-11-13 15:30:05')", (i,))

    def test_page_time(self):
        pub, basis = mp.timing(self.conn, self.m, self.it(posted_at=dt.datetime(2026, 11, 13, 14, 4)), self.evening, set())
        self.assertEqual((pub, basis), (dt.datetime(2026, 11, 13, 14, 4), "page"))
        self.assertFalse(self.evening.contains(pub))                 # 장중 게시 → 시간대 밖

    def test_snapshot_after_and_before(self):
        self.snap(["old"])
        snaps = mp.snapshot_dates(self.conn)
        pub, basis = mp.timing(self.conn, self.m, self.it(id="new"), self.evening, snaps)
        self.assertEqual(basis, "snapshot")
        self.assertTrue(self.evening.contains(pub))
        pub, basis = mp.timing(self.conn, self.m, self.it(id="old"), self.evening, snaps)
        self.assertEqual(basis, "before")
        self.assertFalse(self.evening.contains(pub))

    def test_unknown_without_snapshot(self):
        pub, basis = mp.timing(self.conn, self.m, self.it(), self.evening, set())
        self.assertEqual(basis, "unknown")
        self.assertFalse(self.evening.contains(pub))

    def test_embargo_fallback(self):
        it = self.it(embargo_at=dt.datetime(2026, 11, 13, 18, 0))
        self.assertEqual(mp.timing(self.conn, self.m, it, self.evening, set()), (dt.datetime(2026, 11, 13, 18), "embargo"))

    def test_weekend_dates_in_monday_window(self):
        with mock.patch.object(mp, "now_kst", return_value=dt.datetime(2026, 11, 16, 7, 0)):
            pub, basis = mp.timing(self.conn, self.m, self.it("2026-11-15"), self.morning, set())
        self.assertEqual(basis, "date")
        self.assertTrue(self.morning.contains(pub))
        pub, basis = mp.timing(self.conn, self.m, self.it("2026-11-12"), self.morning, set())
        self.assertFalse(self.morning.contains(pub))

    def test_backfill_end_date_is_unknown(self):
        # 지난 시간대를 다시 받을 때 끝 날짜 글은 시간대 끝 뒤에 올라왔을 수 있다
        w = Window(dt.datetime(2026, 11, 12, 15, 30), dt.datetime(2026, 11, 13, 7, 0))
        self.assertEqual(mp.timing(self.conn, self.m, self.it("2026-11-13"), w, set())[1], "unknown")


class DedupeTest(unittest.TestCase):
    BODY = ("국토교통부는 13일 제3차 주택공급 정례브리핑을 열고 서울 아파트 가격 상승폭이 둔화되고 매물이 늘고 있으며 "
            "수도권에는 내년 1분기까지 4만 호 이상이 입주할 예정이라고 밝혔다. 정부는 공공주택 공급을 차질 없이 추진한다.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def art(self, source, office_id, aid, office, title, pub, body="", basis=None):
        self.conn.execute("INSERT INTO articles (source, office_id, article_id, office_name, title, summary, body, published_at, url, "
                          "collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (source, office_id, aid, office, title, "", body, pub, f"https://x/{aid}", pub))
        rid = self.conn.execute("SELECT id FROM articles WHERE office_id=? AND article_id=?", (office_id, aid)).fetchone()[0]
        if source == mp.SOURCE:
            self.conn.execute("INSERT INTO press_meta (article_id, ministry, ministry_name, item_id, list_date, time_basis) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (rid, office_id, office, aid, pub[:10], basis))
        return rid

    def test_groups_and_keeps_earliest_time(self):
        k = self.art("korea_policy", "korea.kr", "1", "관계부처 합동", "서울 주택가격 상승폭 둔화…수도권 내년 1분기까지 4만 호 입주",
                     "2026-11-13 17:10:00", self.BODY)
        m = self.art(mp.SOURCE, "molit", "9", "국토부", "서울 가격상승폭 둔화·매물 증가…수도권 내년 1분기까지 4만 호 이상 입주",
                     "2026-11-13 11:10:00", "", "page")
        other = self.art("korea_policy", "korea.kr", "2", "고용노동부", "한파 취약사업장 3만여 곳 집중 관리", "2026-11-13 15:50:00")
        pairs = mp.dedupe(self.conn, ["2026-11-13"], log=lambda *_: None)
        self.assertEqual(len(pairs), 1)
        dup = self.conn.execute("SELECT * FROM policy_dups").fetchall()
        self.assertEqual([(r["article_id"], r["canonical_id"]) for r in dup], [(k, m)])       # 대표 = 부처 원문
        self.assertEqual(self.conn.execute("SELECT published_at FROM articles WHERE id=?", (m,)).fetchone()[0],
                         "2026-11-13 11:10:00")                                                # 더 이른 시각
        merged = json.loads(self.conn.execute("SELECT merged_from FROM press_meta WHERE article_id=?", (m,)).fetchone()[0])
        self.assertEqual(merged[0]["id"], k)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM policy_dups WHERE article_id=?", (other,)).fetchone())

    def test_korea_earlier_moves_canonical_time(self):
        # 날짜만 아는 부처 글(15:30 스냅샷 뒤)보다 정책브리핑 시각이 확실하면 그 시각을 남긴다
        self.art("korea_policy", "korea.kr", "1", "산업통상부", "12차 석유 최고가격 11차 수준으로 동결", "2026-11-13 16:05:00", self.BODY)
        m = self.art(mp.SOURCE, "motir", "5", "산업부", "(참고자료)12차 최고가격, 11차 수준으로 동결", "2026-11-13 15:30:00",
                     self.BODY, "snapshot")
        mp.dedupe(self.conn, ["2026-11-13"], log=lambda *_: None)
        self.assertEqual(self.conn.execute("SELECT published_at FROM articles WHERE id=?", (m,)).fetchone()[0],
                         "2026-11-13 16:05:00")

    def test_joint_release_two_ministries(self):
        a = self.art(mp.SOURCE, "msit", "1", "과기정통부", "[관계부처 합동] 제27차 한·불 원자력공동조정위원회 개최",
                     "2026-11-13 16:00:00", self.BODY, "snapshot")
        b = self.art(mp.SOURCE, "mcee", "2", "기후에너지환경부", "제27차 한·불 원자력공동조정위원회 개최",
                     "2026-11-13 16:20:00", self.BODY, "page")
        mp.dedupe(self.conn, ["2026-11-13"], log=lambda *_: None)
        rows = self.conn.execute("SELECT article_id, canonical_id FROM policy_dups").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(a, b)])    # 시각을 정확히 아는 쪽이 대표

    def test_korea_articles_not_merged_together(self):
        self.art("korea_policy", "korea.kr", "1", "정책브리핑", "반도체 지원 확대", "2026-11-13 16:00:00", self.BODY)
        self.art("korea_policy", "korea.kr", "2", "정책브리핑", "반도체 지원 확대", "2026-11-13 17:00:00", self.BODY)
        self.assertEqual(mp.dedupe(self.conn, ["2026-11-13"], log=lambda *_: None), [])

    def test_similarity_threshold(self):
        s, _ = mp.similarity({"title": "[보도자료] 원전 수출 지원 강화"}, {"title": "중소기업 정책자금 1조 공급"})
        self.assertLess(s, mp.DUP_THRESHOLD)
        s, _ = mp.similarity({"title": "(참고자료)12차 최고가격, 11차 수준으로 동결"}, {"title": "12차 최고가격 11차 수준으로 동결"})
        self.assertGreaterEqual(s, mp.DUP_THRESHOLD)


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        for i, (src, off) in enumerate([("korea_policy", "산업통상부"), (mp.SOURCE, "국토부"), ("naver_focus_401", "한국경제")], 1):
            self.conn.execute("INSERT INTO articles (id, source, office_id, article_id, office_name, title, published_at, collected_at) "
                              "VALUES (?, ?, ?, ?, ?, 't', '2026-11-13 16:00:00', 'x')", (i, src, f"o{i}", str(i), off))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_basis_labels(self):
        from stock_news.report import basis_labels, basis_tags, policy_ministries
        it = {"id": "P-1:000001", "sources": ["article:1", "article:2", "article:3"]}
        self.assertEqual(basis_tags(self.conn, it), ["뉴스", "정책"])                     # 분류(집계용)는 그대로
        self.assertEqual(policy_ministries(self.conn, it), ["산업부", "국토부"])           # 정책브리핑 표기도 통일
        self.assertEqual(basis_labels(self.conn, it), ["뉴스", "정책:산업부", "정책:국토부"])

    def test_policy_label(self):
        self.assertEqual(mp.policy_label("재정경제부"), "정책:기획재정부")
        self.assertEqual(mp.policy_label("고용노동부"), "정책:고용노동부")


class RssTest(unittest.TestCase):
    """2026-11-14 실제 RSS 형식 — 시각 출처 우선순위."""

    def test_time_formats(self):
        f = mp.parse_rss_time
        self.assertEqual(f("Fri, 13 Nov 2026 06:57:39 GMT"), (dt.datetime(2026, 11, 13, 15, 57, 39), True))   # 식약처(UTC)
        self.assertEqual(f("Fri, 13 Nov 2026 19:08:38 +0900"), (dt.datetime(2026, 11, 13, 19, 8, 38), True))  # 국토부
        self.assertEqual(f("Fri Nov 13 07:00:00 KST 2026"), (dt.datetime(2026, 11, 13, 7, 0), True))          # 기후부
        self.assertEqual(f("2026-11-13 00:00:00.0"), (dt.datetime(2026, 11, 13), False))                       # 기재부 날짜만
        self.assertEqual(f("<![CDATA[2026.11.13]]>"), (dt.datetime(2026, 11, 13), False))                     # 과기정통부
        self.assertEqual(f(""), (None, False))

    def test_parse_and_apply(self):
        xml = ("<rss><channel><item><title><![CDATA[[보도참고] 코코아분말 회수 조치]]></title>"
               "<link>https://www.mfds.go.kr/brd/m_99/view.do?seq=50493</link><pubDate>Fri, 13 Nov 2026 06:57:39 GMT</pubDate></item>"
               "<item><title>다른 글</title><link>https://x/view.do?seq=1</link><pubDate>Thu, 12 Nov 2026 01:00:00 GMT</pubDate></item>"
               "</channel></rss>")
        items = mp.parse_rss(xml, mp.MINISTRIES["mfds"])
        self.assertEqual(items[0]["id"], "50493")
        by_id = {x["id"]: x["posted_at"] for x in items}
        lst = [{"id": "50493", "title": "x", "date": dt.date(2026, 11, 13)},
               {"id": "1", "title": "다른 글", "date": dt.date(2026, 11, 13)},     # RSS 날짜(11-12 KST 10:00)와 다름 → 안 씀
               {"id": "9", "title": "[보도참고] 코코아분말 회수 조치", "date": dt.date(2026, 11, 13)}]   # 제목으로 맞춤
        n = mp.apply_rss(lst, by_id, {mp.norm_title(x["title"]): x["posted_at"] for x in items})
        self.assertEqual(n, 2)
        self.assertEqual((lst[0]["posted_at"], lst[0]["time_source"]), (dt.datetime(2026, 11, 13, 15, 57, 39), "rss"))
        self.assertNotIn("posted_at", lst[1])

    def test_rss_beats_snapshot_in_timing(self):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(Path(tmp.name) / "t.db")
        w = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 13, 19, 30))
        it = {"id": "1", "date": dt.date(2026, 11, 13), "posted_at": dt.datetime(2026, 11, 13, 16, 0, 34), "time_source": "rss"}
        self.assertEqual(mp.timing(conn, mp.MINISTRIES["mof"], it, w, {"2026-11-13"}), (dt.datetime(2026, 11, 13, 16, 0, 34), "rss"))
        self.assertEqual(mp.TIME_SOURCE["before"], "snapshot")
        conn.close()
        tmp.cleanup()

    def test_rss_registry(self):
        exact = sorted(k for k, m in mp.MINISTRIES.items() if m.rss_exact)
        self.assertEqual(exact, ["mcee", "mfds", "mof", "mohw", "molit"])


class IntradayPolicyTest(unittest.TestCase):
    def test_posted_session(self):
        ps = mp.posted_session
        self.assertEqual(ps("2026-11-13 10:13:23", "rss"), "장중")
        self.assertEqual(ps("2026-11-13 07:00:00", "rss"), "장 전")         # 07:00~08:00 장 전
        self.assertEqual(ps("2026-11-13 07:59:59", "rss"), "장 전")
        self.assertEqual(ps("2026-11-13 08:00:00", "rss"), "장중")
        self.assertEqual(ps("2026-11-13 15:30:00", "snapshot"), "장 마감 후")
        self.assertEqual(ps("2026-11-13 06:59:00", "page"), "장 마감 후")
        self.assertEqual(ps("2026-11-13 00:00:00", "before"), "장중")          # 15:30 스냅샷에 이미 있던 글
        self.assertEqual(ps("2026-11-14 11:00:00", "page"), "장 마감 후")      # 토요일
        self.assertEqual(ps("2026-11-16 00:00:00", "date"), "장 마감 후")

    def test_intraday_start(self):
        w = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 16, 7, 0))
        self.assertEqual(mp.intraday_start(w), dt.datetime(2026, 11, 13, 7, 0))

    def test_candidates_keep_intraday_policy_not_disclosure_news(self):
        from stock_news.analysis import candidates
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(Path(tmp.name) / "t.db")
        rows = [("korea_policy", "korea.kr", "1", "중소벤처기업부", "상생 재원 2조 원 조성", "2026-11-13 13:35:00"),
                ("korea_policy", "korea.kr", "2", "고용노동부", "한파 취약사업장 관리", "2026-11-13 15:50:00"),
                ("naver_focus_401", "n", "3", "한국경제", "장중 시황 기사", "2026-11-13 13:00:00"),
                ("korea_policy", "korea.kr", "4", "정책브리핑", "아침 6시 기사", "2026-11-13 06:00:00")]
        for r in rows:
            conn.execute("INSERT INTO articles (source, office_id, article_id, office_name, title, published_at, collected_at) "
                         "VALUES (?,?,?,?,?,?,'x')", r)
        conn.execute("INSERT INTO articles (source, office_id, article_id, office_name, title, published_at, collected_at) "
                     "VALUES ('ministry_press','motir','5','산업부','날짜만 아는 장중 글','2026-11-13 00:00:00','x')")
        conn.execute("INSERT INTO press_meta (article_id, ministry, ministry_name, item_id, list_date, time_basis) "
                     "VALUES ((SELECT id FROM articles WHERE article_id='5'), 'motir', '산업부', '5', '2026-11-13', 'before')")
        conn.commit()
        w = Window(dt.datetime(2026, 11, 13, 15, 30), dt.datetime(2026, 11, 13, 19, 30))
        gov = candidates.build(conn, w)["gov_policy"]
        got = [(g["title"], g["intraday"]) for g in gov]
        self.assertEqual(got[0], ("한파 취약사업장 관리", False))            # 장 마감 후 먼저
        self.assertIn(("상생 재원 2조 원 조성", True), got)
        self.assertIn(("날짜만 아는 장중 글", True), got)
        self.assertNotIn("아침 6시 기사", [t for t, _ in got])              # 07:00 전 = 아침 리포트 시간대
        conn.close()
        tmp.cleanup()

    def test_label_and_review_split(self):
        from stock_news.analysis import review
        from stock_news.report import basis_labels, policy_session
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(Path(tmp.name) / "t.db")
        conn.execute("INSERT INTO articles (id, source, office_id, article_id, office_name, title, published_at, collected_at) "
                     "VALUES (1,'korea_policy','korea.kr','1','국토교통부','t','2026-11-13 10:13:00','x'),"
                     "(2,'korea_policy','korea.kr','2','산업통상부','t','2026-11-13 18:00:00','x')")
        a, b = {"id": "P-1:1", "sources": ["article:1"]}, {"id": "P-2:2", "sources": ["article:2"]}
        self.assertEqual(policy_session(conn, a), "장중")
        self.assertEqual(basis_labels(conn, a), ["정책:국토부", "장중 게시"])
        self.assertEqual(basis_labels(conn, b), ["정책:산업부"])
        rows = [{"grp": "scored", "sentiment": "호재", "final": 3, "basis": "정책", "oc_excess": 0.02, "policy_session": "장중",
                 "ministries": ["국토부"]},
                {"grp": "scored", "sentiment": "호재", "final": 3, "basis": "정책", "oc_excess": -0.01,
                 "policy_session": "장 마감 후", "ministries": ["산업부"]}]
        sm = review.summarize(rows, "oc_excess")
        self.assertEqual(sm["by_policy_session"]["장중"]["n"], 1)
        self.assertAlmostEqual(sm["by_policy_session"]["장 마감 후"]["mean"], -0.01)
        conn.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
