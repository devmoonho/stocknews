# 전일 재료 기반 매매 준비 시스템

매일 아침(07시경) 사용자가 "오늘 아침 리포트 만들어줘" 또는 `/morning-report`를 실행하면,
직전 거래일 15:30 이후 ~ 실행 시점의 뉴스·공시를 수집해 호재 강도(1~5점)를 매기고 리포트를 만든다.
**최종 매매 판단은 사용자가 한다.** 자동매매·주문은 범위 밖 — 주문 API를 호출하지 않는다.

- 사용자에게는 항상 한국어로 답한다.
- Python 3.12 표준 라이브러리만 사용한다(추가 설치 없음). 스크립트는 프로젝트 루트에서 `python3 scripts/...`로 실행.
- 비밀값은 `.env`(DART_API_KEY, KIWOOM_APP_KEY/SECRET_KEY, KIWOOM_MOCK, KIWOOM_ACCOUNT_NO). 값을 출력·커밋하지 않는다.

## 아침 절차 (순서 고정)

1. **종목 마스터** — `python3 scripts/update_master.py --skip-if-fresh`
   - 키움: 전 종목 + 제외 플래그(ETF/ETN·거래정지·관리종목·정리매매·목록에서 사라진 종목), 키움 테마
   - 네이버: 테마 265개 + 테마 설명 + 종목별 편입 사유 — **주 1회**(그 주 첫 실행, 보통 월요일 아침)
   - 키움은 오늘, 네이버 테마는 이번 주에 이미 갱신했으면 건너뜀. 네이버 테마를 바로 받으려면 `--only naver_themes`
2. **수집** — `python3 scripts/collect.py`
   - 네이버 뉴스 포커스 '시황·전망'(sid=401) 목록+본문
   - 정책브리핑(korea.kr) 정책뉴스: 목록을 페이지로 넘기며 날짜 범위 기사 → 본문의 발행 시각으로 시간대 필터
   - DART 공시(목록·접수 시각·주요 공시 본문) → 모두 SQLite
   - 종료 코드 2 = 경고 있음. **경고는 리포트 상단에 그대로 옮긴다.** 0건·형식 변경 경고면 사용자에게 먼저 알린다.
3. **매칭** — `python3 scripts/match.py`
   - `data/work/YYYY-MM-DD/candidates.json`(전체 근거)과 `candidates_digest.md`(압축본) 생성
4. **채점 (Claude)** — 아래 "채점 규칙"대로 `data/work/YYYY-MM-DD/scores.json` 작성
5. **검증** — `python3 scripts/score.py` → 오류가 나오면 scores.json을 고쳐 통과할 때까지 반복 → `scored.json`, DB `scores`
6. **리포트** — `python3 scripts/report.py` → `reports/YYYY-MM-DD.md`
   - 요약·수집 경고 / 장 시작 전 체크리스트(지수·장초반 매수세·외국인·기관·개인 수급, 직접 기입) /
     호재 순위표(종목별 시초가·매수세·메모 칸) / 종목별 항목 점수·근거·원문 링크 / 주의 종목 / 정책뉴스 검토 / 수집 현황
7. **메일 발송** — `python3 scripts/send_report.py` (`.env`의 MAIL_SMTP_PASSWORD가 비어 있으면 건너뛰고 사용자에게 알린다)
   - 메일용 HTML(인라인 스타일·표 레이아웃) + 텍스트 대체본. 미리보기만: `--dry-run` → data/work/날짜/report_email.html
8. **텔레그램 요약** — `python3 scripts/send_telegram.py` (토큰·채팅 ID가 없으면 건너뜀)
   - 호재 상위 7종목·주의 종목·수혜 정책을 4096자 안의 짧은 글로. 확인만: `--dry-run`, 토큰 확인: `--check`
   - 실거래를 제어하는 봇(AutoInvest QuoVestor)의 토큰은 절대 쓰지 않는다.
   - 리포트 파일은 생성 후 손으로 고치지 않는다(다시 만들면 덮어씀). 수정은 scores.json → score.py → report.py 순서로.

## 저녁 종가매매 리포트 (평일 19:45 발송)
"오늘 저녁 리포트 만들어줘" 또는 `/evening-report` — 당일 15:30 ~ 지금의 공시·뉴스로 NXT 애프터마켓(20:00 마감) 매매 판단용 리포트.
- 절차: `collect.py` → `match.py --session evening`(시간외 시세 포함) → 채점(아침과 같은 채점표, `data/work/날짜_evening/scores.json`)
  → `score.py --session evening` → `report.py --session evening`(`reports/evening/날짜.md`) → `send_report.py --session evening`
  → `send_telegram.py --session evening`
- 저녁 점수는 DB `evening_scores`(아침 `scores`와 별도). 선반영은 오늘 KRX 종가까지 상승률 + 지금 시간외 등락.
- 자동: 19:10 `evening_auto.py --prefetch`, 19:30 `evening_auto.py` — Claude 채점은 19:42:30까지만 기다리고
  못 끝내면 객관 지표 간이 리포트로 대체해 19:45 전에 보낸다.

## 자동 실행 (미니PC cron)
- 평일 07:00 `scripts/morning_auto.py` — 1~8단계 전체(채점은 `claude -p`). 실패 시 텔레그램 알림. 로그 `data/logs/`
- 평일 19:10 `scripts/evening_auto.py --prefetch`, 19:30 `scripts/evening_auto.py` — 저녁 종가매매 리포트
- 평일 20:10 `scripts/track_prices.py` — 채점 종목·비교 기준 종목의 주가 반응 기록(`price_reactions`),
  장중 상한가 도달·해제·재도달(`intraday_patterns`, 1분봉)
- 토요일 08:00 `scripts/weekly_review.py` — 점수별·근거별 실제 반응 집계, **재료 유형·근거별 영향 지속성(D0~D+10 경로, 정점·반감일)**,
  가중치 재귀 조절(`config/weights.json`,
  `weight_history`), 주간 리포트 `reports/weekly/날짜.md` + 메일·텔레그램
- 가중치는 주간 리뷰만 바꾼다. Claude가 채점 중에 `config/weights.json`을 고치지 않는다.

## 채점 규칙
- 기준은 `config/scoring_rubric.md` **하나뿐**이다. 매번 읽고 그대로 적용한다. 날마다 기준을 바꾸지 않는다.
- 입력은 `candidates_digest.md`를 먼저 읽고, 더 필요한 근거(기사 전문, 공시 본문, 편입 사유)는 `candidates.json`이나 SQLite(`data/stock_news.db`)에서 찾아본다.
- 확산도(`diffusion`)와 공시 수치가 있는 규모(`scale`)는 **다이제스트의 객관 점수를 그대로** 쓴다. score.py가 대조한다.
- 최종 점수는 Claude가 정하지 않는다 — score.py가 가중치로 계산한다.
- 각 항목 근거는 한 줄, 숫자와 출처를 넣는다. 기사·공시에 없는 사실을 지어내지 않는다. 추측은 "추정"이라고 쓴다.
- 시황 기사 속 등락 나열(다이제스트 C)은 재료가 아니다. 단순 시세 기사도 재료가 아니다.
- 테마 신호(다이제스트 D)는 구성 종목 중 기사 내용과 편입 사유가 직접 맞는 종목만 고른다(테마 전체를 넣지 않는다).
  id는 `T-번호:종목코드`.
- 정부 정책뉴스(다이제스트 E)는 **하나씩 모두 읽고** 산업·예산과 직결되는 정책만 수혜 종목을 고른다.
  id는 `P-번호:종목코드`, `policy` 점수 필수. 정책 본문은 DB `articles`(source='korea_policy')에 있다.
  종목을 고르지 않은 정책도 리포트에 "검토한 정책" 한 줄로 남길 수 있게 scores.json의 `reviewed_policies`에 적는다.
- **채점표 v2(2026-11-13)**: 항목 7개. `quality`(재료의 질)는 contract·mna·buyback에 필수 — 다이제스트 A의
  발주처 유형·인수자 재무·조달방법·보호예수·신탁/직접·소각 수치를 근거로. `preprice`(선반영)는 **쓰지 않는다**(score.py가 계산).
  규모는 매출(연환산)·시총 대비 중 높은 쪽, 제3자배정 유증은 회사 유입액 합계 — 다이제스트 객관 점수 그대로.
- **채점표 v3**: `theme_cluster`(테마 결집도)·`leader`(테마 대장, T-·P-만)도 **쓰지 않는다**(score.py가 다이제스트 값으로 채움).
  정책 재료의 규모는 회사 범주가 본문에 명시될 때 예산 기준(1조↑ 4 · 3천억~1조 3 · 미만 2), 명시 없으면 −1.
  다이제스트 D·E의 "대장 후보"(편입 사유 연관도 순)를 참고해 고르되, 순위가 낮아도 본문과 직접 맞으면 고른다.
- 가격: 키움 일봉 종가는 KRX+NXT 통합(애프터마켓 20:00까지). 갭·상한가는 KRX 기준가(`price_daily.base_price`) 기준,
  저녁 공시 뒤 시간외 반응은 선반영으로 본다.
- 후속 이벤트(다이제스트 G): 이전 채점 재료의 유증 납입·거래종결 일정과 새 공시. 재채점은 id `F-번호:종목코드`.
  완료=확정, 정정·연기=신규성 낮게, 철회·미이행=악재. 일정은 score.py가 `followups`에 자동 등록.
- 악재는 `sentiment: "악재"`로 따로 기록(점수 없음) — 리포트의 주의 종목.
- 항목마다 재료 유형 `category`를 적는다(contract 공급계약·수주 / earnings 실적 / buyback 자사주·주주환원 / policy 정부 정책 /
  tech 기술·특허·허가 / mna M&A·지분·경영권 / investment 시설투자·증설 / partnership 협력·MOU / other). 주간 지속성 분석 단위다.
- scores.json 형식은 `src/stock_news/analysis/scoring.py` 맨 위 docstring 참조.

## 데이터 위치
- `data/stock_news.db` — articles, disclosures, stocks, themes, theme_stocks, scores, collect_runs,
  price_daily, index_daily, price_reactions, price_paths, intraday_patterns, followups, weight_history
  - 호재 여부와 상관없이 수집한 것은 전부 저장(백테스트용). 지우지 않는다.
- `data/work/YYYY-MM-DD/` — 그날의 덤프·후보·채점 파일
- `reports/YYYY-MM-DD.md` — 최종 리포트
- `ref/ras/`, `ref/telegram/` — 참고용 원본(수정하지 않음). 실제 사용본은 `src/stock_news/collectors/dart_client.py`, `src/stock_news/telegram_notify.py`

## 오탐 조정
- 종목명 오탐 → `config/name_stopwords.txt`에 추가, 별칭 → `config/name_aliases.txt`
- 테마 키워드 오탐(예: 백신 테마의 'AI'=조류인플루엔자) → `config/theme_keyword_block.txt`
- 정책 기사 키워드 → `config/policy_keywords.txt`
- 정정공시는 바뀐 내용(본문 '정정사유')을 확인하고, 단순 오기·일정 변경이면 재료로 채점하지 않는다
- 휴장일 → `config/krx_holidays.txt` (연초에 KRX 공지 보고 갱신)

## 알려진 특성
- 네이버 포커스 API: 요청 날짜 기사가 끝나면 전날 1페이지를 반복 → 날짜별로 요청하고 응답 date가 바뀌면 중단(구현됨).
- DART list.json은 시각이 없음 → 오늘의공시 페이지(dsac001)에서 시각을 붙인다. 지분·펀드 공시 일부는 시각 없음(정상).
- 키움 키는 현재 **모의투자** 키(KIWOOM_MOCK=1). 종목·테마 조회는 모의 서버에서도 동작.
- korea.kr 목록은 날짜만, 본문 meta `article:published_time`에 시각이 있다('Z'가 붙어 있지만 KST). robots.txt 허용.
- 네이버 사이트 robots.txt는 일반 봇을 막는다 — 뉴스 목록은 하루 2회(아침 07:00, 저녁 19:10·19:30), 기사 본문은 기사당 1회만,
  테마(약 270회 요청)는 주 1회
  (저녁에 받은 기사는 아침에 다시 받지 않음), 요청 간격 유지. 이보다 빈도를 늘리지 않는다(2026-11-13 사용자 결정).
- 키움 일봉 종가는 KRX+NXT 통합(20:00까지) → 그날 봉은 20:05 전에 저장하지 않는다(`tracking._partial_day`).
  저녁 시세(오늘 KRX 종가·지금 NXT 가격)는 1분봉(ka10080)에서 계산한다.
