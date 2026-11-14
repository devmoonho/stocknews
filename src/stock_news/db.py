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
    base_price REAL,                            -- KRX 기준가 = 전일 KRX 종가 (close는 KRX+NXT 통합 종가)
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
    open_limit    INTEGER,                  -- 시가가 상한가(매수 불가) → 시가→종가 목표에서 제외
    after_hours_pct REAL,                   -- 전일 NXT 애프터마켓 반응: 통합 종가 / KRX 종가 − 1
    krx_close_excess REAL,                  -- KRX 기준가 대비 종가 수익률 − 지수 (보조 목표)
    krx_prev_close REAL,                    -- 전일 KRX 종가 (= D0 기준가)
    theme         TEXT,                     -- theme_peer: 어느 테마의 비교 종목인지
    leader_rank   INTEGER,                  -- 테마 안 연관도 순위(대장 후보 1·2)
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

CREATE TABLE IF NOT EXISTS evening_scores (     -- 저녁 종가매매 리포트 채점(아침 scores와 별도) + 다음날 반응
    date          TEXT NOT NULL,            -- 저녁 리포트 날짜(그날 15:30 이후 재료)
    stock_code    TEXT NOT NULL,
    stock_name    TEXT, sentiment TEXT, final INTEGER, final_raw REAL, material TEXT,
    factors       TEXT, sources TEXT, category TEXT, weights_version INTEGER, rubric_version INTEGER,
    fallback      INTEGER DEFAULT 0,        -- Claude 채점이 시간 안에 안 끝나 객관 지표로만 만든 항목
    quote_time    TEXT,                     -- 시세 기준 시각 HH:MM
    krx_close     REAL,                     -- 그날 KRX 종가(15:30)
    entry_price   REAL,                     -- 리포트 시각 NXT 가격(애프터마켓 매수 가정가)
    after_hours_pct REAL, since_disclosure_pct REAL, after_value REAL,
    nxt_enabled   INTEGER,                  -- 종목 마스터 nxtEnable
    next_open REAL, next_high REAL, next_close REAL,   -- 다음 거래일 (통합 봉)
    next_open_ret REAL, next_close_ret REAL, next_high_ret REAL,   -- 진입가 대비
    updated_at    TEXT,
    PRIMARY KEY (date, stock_code)
);
CREATE TABLE IF NOT EXISTS factor_corr_history (  -- 주간 리뷰의 항목별 상관 — 가중치 0/복귀 규칙(연속 주 판단)
    week_end TEXT NOT NULL, target TEXT NOT NULL, factor TEXT NOT NULL, n INTEGER, rho REAL,
    PRIMARY KEY (week_end, target, factor)
);
-- ---- 테마 선행 예측 (scripts/theme_daily.py 15:35 · analysis/theme_forecast.py 07:00)
CREATE TABLE IF NOT EXISTS theme_groups (        -- 구성이 사실상 같은 테마 묶음(대표 중심 묶기, 사슬 연결 없음)
    group_id      TEXT NOT NULL,                 -- 대표 테마 'source:theme_code' (날마다 같은 ID)
    source        TEXT NOT NULL, theme_code TEXT NOT NULL,
    is_rep        INTEGER,                       -- 1 = 대표 테마
    group_name    TEXT,                          -- '대표 테마명 (+유사 N개)'
    member_count  INTEGER,                       -- 묶음 구성 종목 수
    theme_count   INTEGER,                       -- 묶음에 든 테마 수
    members       TEXT,                          -- JSON 종목코드 목록(묶음 구성)
    updated_at    TEXT,
    PRIMARY KEY (source, theme_code)
);
CREATE TABLE IF NOT EXISTS market_daily (        -- 전 종목 KRX 장 마감 스냅샷 (ka10032, 15:31~15:59)
    dt TEXT NOT NULL, code TEXT NOT NULL,
    close REAL, change_pct REAL,                 -- KRX 종가, 기준가 대비 %
    trade_value REAL,                            -- 거래대금(원)
    volume REAL,
    approx INTEGER DEFAULT 0,                    -- 1 = 통합 일봉으로 근사 재구성(--rebuild)
    PRIMARY KEY (dt, code)
);
CREATE TABLE IF NOT EXISTS theme_daily (         -- 테마 묶음별 하루 지표
    dt TEXT NOT NULL, group_id TEXT NOT NULL,
    group_name TEXT, n INTEGER,                  -- 시세가 있는 구성 종목 수
    avg_pct REAL, median_pct REAL, wavg_pct REAL,          -- 단순 평균 · 중앙값 · 거래대금 가중
    avg_excess REAL, median_excess REAL, wavg_excess REAL, -- 지수(종목 시장) 대비 초과
    rise_ratio REAL, up5 INTEGER, limit_ups INTEGER,
    trade_value REAL, tv_ratio20 REAL,           -- 거래대금 합계, 직전 20거래일 평균 대비
    concentration REAL,                          -- 상위 2종목이 테마 상승분에서 차지하는 비율(폭 지표)
    strong INTEGER, streak INTEGER, cum_excess REAL,
    stage TEXT,                                  -- 태동/확산/과열/소멸/NULL
    leader_code TEXT, leader_changed INTEGER,
    rank INTEGER,                                -- 중앙값 초과 순위(5종목 이상 묶음만)
    small INTEGER,                               -- 1 = 5종목 미만(순위·예측 제외)
    approx INTEGER DEFAULT 0,
    PRIMARY KEY (dt, group_id)
);
CREATE TABLE IF NOT EXISTS theme_members_daily ( -- 묶음 안 종목 서열
    dt TEXT NOT NULL, group_id TEXT NOT NULL, code TEXT NOT NULL,
    change_pct REAL, excess REAL, trade_value REAL,
    limit_hit INTEGER, first_hit TEXT, closed_at_limit INTEGER,
    streak INTEGER, pos INTEGER, label TEXT,     -- 대장 / 2등주 / 후발주 / NULL
    after_leader_pct REAL,                       -- 대장 상한가 도달 뒤 이 종목의 추가 상승(2등주, D0)
    next_change_pct REAL, next_excess REAL,      -- D+1 (다음 날 집계 때 채움)
    PRIMARY KEY (dt, group_id, code)
);
CREATE TABLE IF NOT EXISTS theme_forecasts (     -- 테마 예측(kind=forecast)과 기준선(kind=baseline)
    date TEXT NOT NULL,                          -- 예측 대상 거래일
    phase TEXT NOT NULL DEFAULT 'morning',       -- evening = 전날 19:30 1차(종가매매 리포트) · morning = 그날 07:00 최종
    kind TEXT NOT NULL, rank INTEGER NOT NULL,
    group_id TEXT, group_name TEXT, score REAL, components TEXT, confidence TEXT,
    leader_pick TEXT, second_picks TEXT,         -- 종목코드, JSON 목록
    related TEXT,                                -- 중복 회피로 건너뛴 관련 묶음 이름 JSON
    dedupe INTEGER NOT NULL DEFAULT 1,           -- 1 = 중복 회피 적용 목록, 0 = 미적용(비교용)
    created_at TEXT,
    PRIMARY KEY (date, phase, kind, dedupe, rank)
);
CREATE TABLE IF NOT EXISTS theme_forecast_results (  -- 예측 적중 판정 (그날 15:35 집계 뒤)
    date TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'morning', kind TEXT NOT NULL, dedupe INTEGER NOT NULL,
    n INTEGER, avg_excess REAL, top10_hits INTEGER, top10_ratio REAL,
    leader_hits INTEGER, leader_n INTEGER, second_hits INTEGER, second_n INTEGER,
    detail TEXT, updated_at TEXT,
    PRIMARY KEY (date, phase, kind, dedupe)
);
CREATE TABLE IF NOT EXISTS theme_runs (          -- 15:35/15:50 집계 실행 기록(재시도 판단)
    dt TEXT NOT NULL, started_at TEXT, finished_at TEXT, ok INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS intraday_disclosures (  -- 장중 공시(아침·저녁 수집 시간대 밖, 15:30 전) — 다음 날 주가 반응 비교용
    rcept_no TEXT PRIMARY KEY, rcept_dt TEXT NOT NULL, rcept_time TEXT, received_at TEXT,
    corp_name TEXT, corp_code TEXT, stock_code TEXT, corp_cls TEXT, report_nm TEXT NOT NULL, flr_nm TEXT,
    signal TEXT, confirmed INTEGER, url TEXT, collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intraday_disc_dt ON intraday_disclosures(rcept_dt, stock_code);
CREATE TABLE IF NOT EXISTS minute_bars (           -- 손익 시나리오용 1분봉 (KRX 09:00~15:30 · NXT 08:00~08:50)
    date TEXT NOT NULL, code TEXT NOT NULL, venue TEXT NOT NULL,   -- venue: KRX | NXT
    prev_close REAL,                          -- 전일 KRX 종가(전일 15:30 봉) — 상한가 판정 기준
    bars TEXT,                                -- JSON [[hhmm, open, high, low, close, volume], ...] 시간순
    n INTEGER, updated_at TEXT,
    PRIMARY KEY (date, code, venue)
);
CREATE TABLE IF NOT EXISTS scenario_recs (         -- 일일 손익 시나리오 추천(분류별) — 전일 대비 변화 비교
    date TEXT NOT NULL, segment TEXT NOT NULL,
    entry TEXT, mode TEXT, stop REAL, take REAL,
    zone TEXT,                                -- JSON {stop:[lo,hi], take:[lo,hi]}
    metrics TEXT,                             -- JSON 선정 구간·검증 성과·신뢰도
    created_at TEXT,
    PRIMARY KEY (date, segment)
);

CREATE TABLE IF NOT EXISTS weight_history (    -- 주간 가중치 조절 기록
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT, target TEXT, sample_n INTEGER,
    weights       TEXT,                     -- JSON
    prev_weights  TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS preopen_scores (        -- 장 전 추가(07:50·08:00) 채점: 07:00 이후 새 공시·정책 (아침 scores와 별도)
    date TEXT NOT NULL, stock_code TEXT NOT NULL, stock_name TEXT, sentiment TEXT, final INTEGER, final_raw REAL,
    material TEXT, factors TEXT, sources TEXT, weights_version INTEGER, category TEXT, rubric_version INTEGER,
    fallback INTEGER DEFAULT 0, created_at TEXT,
    PRIMARY KEY (date, stock_code)
);
CREATE TABLE IF NOT EXISTS press_meta (            -- 부처 보도자료(articles.source='ministry_press') 부가 정보
    article_id    INTEGER PRIMARY KEY,      -- articles.id
    ministry      TEXT NOT NULL,            -- motir, msit, molit … (collectors/ministry_press.MINISTRIES)
    ministry_name TEXT NOT NULL,            -- 리포트 표시명 — 근거 표시 [정책:부처명]
    item_id       TEXT NOT NULL,            -- 누리집 글 번호
    dept          TEXT, category TEXT,
    list_date     TEXT NOT NULL,            -- 목록의 등록일 YYYY-MM-DD
    posted_at     TEXT,                     -- 누리집이 보여 준 게시 시각(있을 때만: 국토부·복지부·원안위)
    time_basis    TEXT,                     -- rss | page | snapshot(15:30 스냅샷 뒤) | date(시작일 뒤 날짜) | embargo | before(장중)
    time_source   TEXT,                     -- 시각 출처: rss | page(누리집 표시) | snapshot(15:30 스냅샷) | date | embargo
    embargo_at    TEXT,                     -- 엠바고(보도 시점) 시각 'YYYY-MM-DD HH:MM:SS'
    embargo_text  TEXT,                     -- 엠바고 표기 원문(조간·석간·즉시 포함)
    attachments   TEXT,                     -- JSON [{name, url}]
    merged_from   TEXT,                     -- 같은 보도로 묶인 다른 글 JSON [{id, source, office, published_at}]
    first_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS press_seen (            -- 부처 목록에서 처음 본 시각(15:30 스냅샷 = 장중 게시 판정 기준)
    ministry TEXT NOT NULL, item_id TEXT NOT NULL, list_date TEXT, title TEXT, first_seen_at TEXT NOT NULL,
    PRIMARY KEY (ministry, item_id)
);
CREATE TABLE IF NOT EXISTS press_snapshots (       -- 15:30 스냅샷 실행 기록 (날짜만 있는 부처의 게시 시각 판정용)
    snap_date TEXT PRIMARY KEY, taken_at TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS policy_dups (           -- 같은 보도자료 묶음: 정책브리핑 기사·다른 부처 합동 보도 → 대표 글
    article_id   INTEGER PRIMARY KEY,       -- 묶여서 빠지는 글
    canonical_id INTEGER NOT NULL,          -- 대표 글(부처 원문 우선). 대표의 published_at = 묶음에서 가장 먼저 게시된 시각
    similarity REAL, reason TEXT, merged_at TEXT
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
    warnings      TEXT,                     -- JSON 배열
    detail        TEXT                      -- 소스별 세부(부처 보도자료: 부처별 건수·실패 JSON)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)      # 다른 작업(일봉 백필 등)이 쓰는 중이면 기다림
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """테마 테이블에 source 컬럼이 없던 초기 버전 → 다시 만든다(매일 새로 받는 파생 데이터라 버려도 됨)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(press_meta)")}
    if cols and "time_source" not in cols:          # 시각 출처(RSS / 페이지 표시 / 15:30 스냅샷)
        conn.execute("ALTER TABLE press_meta ADD COLUMN time_source TEXT")
        conn.execute("UPDATE press_meta SET time_source = CASE time_basis WHEN 'before' THEN 'snapshot' ELSE time_basis END")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collect_runs)")}
    if cols and "detail" not in cols:               # 부처 보도자료: 부처별 건수·실패
        conn.execute("ALTER TABLE collect_runs ADD COLUMN detail TEXT")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(disclosures)")}
    if cols and "body_attempts" not in cols:        # 본문 조회 실패 횟수 — 일시 실패는 다음 실행 때 재시도(3회까지)
        conn.execute("ALTER TABLE disclosures ADD COLUMN body_attempts INTEGER DEFAULT 0")
        conn.commit()
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(price_daily)")}
    if cols and "base_price" not in cols:           # KRX 기준가(전일 KRX 종가) — 종가는 KRX+NXT 통합
        conn.execute("ALTER TABLE price_daily ADD COLUMN base_price REAL")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(price_reactions)")}
    for col, typ in (("open_limit", "INTEGER"), ("after_hours_pct", "REAL"), ("krx_close_excess", "REAL"),
                     ("theme", "TEXT"), ("leader_rank", "INTEGER"), ("krx_prev_close", "REAL")):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE price_reactions ADD COLUMN {col} {typ}")
    conn.commit()
    for t in ("theme_forecasts", "theme_forecast_results"):   # 저녁 1차/아침 최종 구분(phase) 전 버전 — 키가 바뀌어 다시 만든다
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        if cols and "phase" not in cols:
            if conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
                conn.execute(f"ALTER TABLE {t} RENAME TO {t}_old")
            else:
                conn.execute(f"DROP TABLE {t}")
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
