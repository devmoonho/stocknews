"""SQLite 저장소. 호재 여부와 무관하게 전부 저장한다(백테스트용)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,            -- naver_focus_401 등
    office_id     TEXT NOT NULL,
    article_id    TEXT NOT NULL,
    office_name   TEXT,
    title         TEXT NOT NULL,
    summary       TEXT,                     -- 목록의 앞부분 요약
    body          TEXT,                     -- 기사 본문(평문)
    published_at  TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS' KST
    url           TEXT,
    collected_at  TEXT NOT NULL,
    body_fetched_at TEXT,
    UNIQUE (office_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);

CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no      TEXT PRIMARY KEY,         -- 접수번호 14자리
    rcept_dt      TEXT NOT NULL,            -- YYYYMMDD
    rcept_time    TEXT,                     -- HH:MM (DART 오늘의공시 페이지)
    received_at   TEXT,                     -- 'YYYY-MM-DD HH:MM:00' (시각 모르면 NULL)
    corp_name     TEXT,
    corp_code     TEXT,
    stock_code    TEXT,
    corp_cls      TEXT,                     -- Y 유가 · K 코스닥 · N 코넥스 · E 기타
    report_nm     TEXT NOT NULL,
    flr_nm        TEXT,
    rm            TEXT,
    signal        TEXT,                     -- buy / sell / neutral (제목 키워드 규칙)
    confirmed     INTEGER,                  -- 확정형 재료 여부
    url           TEXT,
    body          TEXT,                     -- 주요 공시만 본문 저장
    collected_at  TEXT NOT NULL,
    body_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_disclosures_received ON disclosures(received_at);

CREATE TABLE IF NOT EXISTS stocks (
    code          TEXT PRIMARY KEY,         -- 종목코드
    name          TEXT NOT NULL,
    market        TEXT,                     -- KOSPI / KOSDAQ
    sector        TEXT,                     -- 업종명
    audit_info    TEXT,                     -- 감리구분(정상/투자주의/투자경고/투자위험 등)
    state         TEXT,                     -- 종목상태(증거금·거래정지·관리종목 등 '|' 구분)
    order_warning TEXT,                     -- 투자유의 코드(0 없음, 2 정리매매, 3 단기과열, 4 위험, 5 경고)
    is_listed     INTEGER NOT NULL DEFAULT 1,   -- 오늘 목록에 없으면 0 (상장폐지 추정)
    is_excluded   INTEGER NOT NULL DEFAULT 0,   -- 후보 제외 여부
    exclude_reason TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    raw           TEXT                      -- 원본 응답 JSON
);
CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(name);

CREATE TABLE IF NOT EXISTS themes (
    source        TEXT NOT NULL,            -- kiwoom / naver
    theme_code    TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,                     -- 테마 설명(네이버)
    stock_count   INTEGER,
    updated_at    TEXT,
    PRIMARY KEY (source, theme_code)
);

CREATE TABLE IF NOT EXISTS theme_stocks (
    source        TEXT NOT NULL,
    theme_code    TEXT NOT NULL,
    stock_code    TEXT NOT NULL,
    stock_name    TEXT,
    reason        TEXT,                     -- 편입 사유(네이버 themeItemInfoMap)
    updated_at    TEXT,
    PRIMARY KEY (source, theme_code, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_stock ON theme_stocks(stock_code);

CREATE TABLE IF NOT EXISTS scores (           -- 날짜별 채점 결과 (백테스트용)
    date          TEXT NOT NULL,            -- 리포트 날짜(장 날짜)
    stock_code    TEXT NOT NULL,
    stock_name    TEXT,
    sentiment     TEXT,                     -- 호재 / 악재
    final         INTEGER,                  -- 1~5 (악재는 NULL)
    final_raw     REAL,
    material      TEXT,
    factors       TEXT,                     -- JSON
    sources       TEXT,                     -- JSON
    weights_version INTEGER,                -- 계산에 쓴 가중치 버전 (config/weights.json)
    category      TEXT,                     -- 재료 유형 (analysis/categories.py)
    PRIMARY KEY (date, stock_code)
);

CREATE TABLE IF NOT EXISTS price_daily (       -- 일봉 캐시 (키움 ka10081, 수정주가)
    code TEXT NOT NULL, dt TEXT NOT NULL,       -- dt: YYYY-MM-DD
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (code, dt)
);
CREATE TABLE IF NOT EXISTS index_daily (       -- 지수 일봉 (키움 ka20006) 001 코스피 · 101 코스닥
    code TEXT NOT NULL, dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (code, dt)
);
CREATE TABLE IF NOT EXISTS price_reactions (   -- 리포트 날짜(D0)의 주가 반응 — 가중치 조절 근거
    date          TEXT NOT NULL,            -- 리포트 날짜 = 재료 반영 첫 거래일(D0)
    stock_code    TEXT NOT NULL,
    grp           TEXT NOT NULL,            -- scored(채점 종목) / control(후보였지만 채점 안 함 — 비교 기준)
    sentiment     TEXT,                     -- 호재 / 악재 / NULL(control)
    final         INTEGER,
    final_raw     REAL,
    basis         TEXT,                     -- 공시,뉴스,정책
    market        TEXT,
    prev_close    REAL, open REAL, high REAL, low REAL, close REAL, volume REAL,
    vol_ratio20   REAL,                     -- 거래량 / 직전 20일 평균
    gap_pct       REAL,                     -- 시가 / 전일 종가 - 1
    oh_pct        REAL,                     -- 고가 / 시가 - 1 (장중 최대 상승 여지)
    oc_pct        REAL,                     -- 종가 / 시가 - 1 (시가 매수 → 종가)
    close_pct     REAL,                     -- 종가 / 전일 종가 - 1
    idx_oc_pct    REAL, idx_close_pct REAL, -- 같은 시장 지수
    oc_excess     REAL,                     -- oc_pct - idx_oc_pct (기본 목표 지표)
    close_excess  REAL,
    ret_1d REAL, ret_3d REAL, ret_5d REAL,  -- D0 시가 매수 → D+N 종가
    complete      INTEGER DEFAULT 0,        -- ret_5d까지 채워졌으면 1
    updated_at    TEXT,
    PRIMARY KEY (date, stock_code)
);
CREATE TABLE IF NOT EXISTS price_paths (       -- D0~D+10 날짜별 누적 경로 — 재료 영향 지속성 분석
    date          TEXT NOT NULL,            -- 리포트 날짜 D0
    stock_code    TEXT NOT NULL,
    n             INTEGER NOT NULL,         -- 0 = D0, 1 = 다음 거래일 ...
    dt            TEXT,                     -- 그 거래일
    cum_ret       REAL,                     -- 종가(D+n) / 시가(D0) - 1
    idx_cum_ret   REAL,                     -- 같은 시장 지수의 같은 구간
    cum_excess    REAL,                     -- cum_ret - idx_cum_ret
    max_high_ret  REAL,                     -- D0~D+n 최고가 / 시가(D0) - 1
    PRIMARY KEY (date, stock_code, n)
);

CREATE TABLE IF NOT EXISTS followups (         -- 채점한 재료의 후속 일정(유증 납입·경영권 거래종결) 추적
    origin_rcept  TEXT NOT NULL,            -- 일정을 만든 공시 접수번호
    kind          TEXT NOT NULL,            -- 유증납입 / 거래종결
    stock_code    TEXT NOT NULL,
    stock_name    TEXT,
    origin_date   TEXT,                     -- 처음 채점한 리포트 날짜
    due_date      TEXT,                     -- 예정일 YYYY-MM-DD
    detail        TEXT,                     -- 금액·대상자 등 한 줄
    status        TEXT NOT NULL DEFAULT '대기',   -- 대기 / 완료 / 정정 / 철회 / 기한경과
    resolved_rcept TEXT,                    -- 상태를 바꾼 공시
    updated_at    TEXT,
    PRIMARY KEY (origin_rcept, kind)
);
CREATE TABLE IF NOT EXISTS intraday_patterns ( -- D0 장중 상한가 패턴 (1분봉, 사후 검증용)
    date          TEXT NOT NULL,            -- 리포트 날짜 D0
    stock_code    TEXT NOT NULL,
    prev_close    REAL, limit_price REAL,   -- 전일 종가, 상한가(×1.3 호가단위 내림)
    high          REAL, close REAL,
    hit           INTEGER,                  -- 상한가 도달 여부
    first_hit     TEXT,                     -- 처음 도달 시각 HH:MM
    releases      INTEGER,                  -- 도달 후 상한가 아래로 내려온 횟수(해제)
    rehit         INTEGER,                  -- 해제 뒤 다시 도달했나
    closed_at_limit INTEGER,                -- 종가 상한가
    minutes_at_limit INTEGER,               -- 상한가에 머문 분(1분봉 종가 기준)
    bars          INTEGER,                  -- 사용한 1분봉 수
    updated_at    TEXT,
    PRIMARY KEY (date, stock_code)
);

CREATE TABLE IF NOT EXISTS weight_history (    -- 주간 가중치 조절 기록
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT, target TEXT, sample_n INTEGER,
    weights       TEXT,                     -- JSON
    prev_weights  TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS collect_runs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    window_start  TEXT,
    window_end    TEXT,
    fetched       INTEGER,                  -- 시간대 안에서 받은 건수
    inserted      INTEGER,                  -- 새로 저장된 건수
    warnings      TEXT                      -- JSON 배열
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """테마 테이블에 source 컬럼이 없던 초기 버전 → 다시 만든다(매일 새로 받는 파생 데이터라 버려도 됨)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(themes)")}
    if cols and "source" not in cols:
        conn.execute("DROP TABLE themes")
        conn.execute("DROP TABLE IF EXISTS theme_stocks")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scores)")}
    if cols and "weights_version" not in cols:      # 어떤 가중치로 계산한 점수인지
        conn.execute("ALTER TABLE scores ADD COLUMN weights_version INTEGER")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scores)")}
    if cols and "category" not in cols:             # 재료 유형
        conn.execute("ALTER TABLE scores ADD COLUMN category TEXT")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scores)")}
    if cols and "rubric_version" not in cols:       # 어떤 채점표 버전으로 채점했나 (v2: 재료의 질·선반영 추가)
        conn.execute("ALTER TABLE scores ADD COLUMN rubric_version INTEGER")
        conn.commit()


def insert_article(conn: sqlite3.Connection, a: dict) -> bool:
    """새 기사면 True. 이미 있으면 요약만 비어 있을 때 채운다."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO articles
           (source, office_id, article_id, office_name, title, summary, published_at, url, collected_at)
           VALUES (:source, :office_id, :article_id, :office_name, :title, :summary, :published_at, :url, :collected_at)""",
        a)
    return cur.rowcount == 1


def set_article_body(conn: sqlite3.Connection, office_id: str, article_id: str, body: str, fetched_at: str) -> None:
    conn.execute("UPDATE articles SET body=?, body_fetched_at=? WHERE office_id=? AND article_id=?",
                 (body, fetched_at, office_id, article_id))


def upsert_disclosure(conn: sqlite3.Connection, d: dict) -> bool:
    """새 공시면 True. 기존 공시는 시각이 비어 있으면 채운다."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO disclosures
           (rcept_no, rcept_dt, rcept_time, received_at, corp_name, corp_code, stock_code, corp_cls,
            report_nm, flr_nm, rm, signal, confirmed, url, collected_at)
           VALUES (:rcept_no, :rcept_dt, :rcept_time, :received_at, :corp_name, :corp_code, :stock_code, :corp_cls,
                   :report_nm, :flr_nm, :rm, :signal, :confirmed, :url, :collected_at)""", d)
    if cur.rowcount == 1:
        return True
    if d.get("rcept_time"):
        conn.execute("UPDATE disclosures SET rcept_time=?, received_at=? WHERE rcept_no=? AND rcept_time IS NULL",
                     (d["rcept_time"], d["received_at"], d["rcept_no"]))
    return False


def set_disclosure_body(conn: sqlite3.Connection, rcept_no: str, body: str, fetched_at: str) -> None:
    conn.execute("UPDATE disclosures SET body=?, body_fetched_at=? WHERE rcept_no=?", (body, fetched_at, rcept_no))


def log_run(conn: sqlite3.Connection, source: str, started_at: str, finished_at: str, window,
            fetched: int, inserted: int, warnings: list[str]) -> None:
    conn.execute(
        """INSERT INTO collect_runs (source, started_at, finished_at, window_start, window_end, fetched, inserted, warnings)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, started_at, finished_at, f"{window.start:%Y-%m-%d %H:%M:%S}", f"{window.end:%Y-%m-%d %H:%M:%S}",
         fetched, inserted, json.dumps(warnings, ensure_ascii=False)))


def recent_fetched_counts(conn: sqlite3.Connection, source: str, limit: int = 10) -> list[int]:
    rows = conn.execute("SELECT fetched FROM collect_runs WHERE source=? AND fetched > 0 ORDER BY id DESC LIMIT ?",
                        (source, limit)).fetchall()
    return [r[0] for r in rows]
