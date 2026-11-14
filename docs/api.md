# 장전 리포트 읽기 전용 API (v1)

미니PC(`192.168.0.12`)의 `scripts/serve_api.py`가 제공한다. 조회(GET)만 있고 주문·수정 기능은 없다.

- 기본 주소: `http://192.168.0.12:8770`
- 인증: 모든 요청에 헤더 `Authorization: Bearer <API_TOKEN>` (또는 `X-API-Token: <API_TOKEN>`)
  - 토큰은 미니PC `~/stock_news/.env`의 `API_TOKEN` 값. 클라이언트 쪽 `.env`에 넣고 코드·로그에 남기지 않는다.
- 접속 허용: 내부망(사설 IP)만. 그 밖에서는 403.
- 응답: JSON(UTF-8). 시각은 KST `YYYY-MM-DD HH:MM:SS`. 비율은 소수(0.035 = 3.5%).
- 오류: `{"error": "메시지"}` + 상태 코드 400(잘못된 인자) / 401(토큰) / 403(네트워크) / 404(없음) / 405(GET 외) / 500
- 데이터 갱신 시각: 평일 07:00~07:15 아침 리포트(뉴스·공시·채점), 19:30~19:45 저녁 종가매매 리포트, 20:10 주가 반응. 그 외 시간엔 데이터가 바뀌지 않는다
  → 클라이언트는 수 분 간격 이상으로 조회하면 충분하다.

## 엔드포인트

### 아침 / 저녁 리포트 (`session`)
`/api/dates`, `/api/report/...`, `/api/report/.../markdown`은 `session` 인자를 받는다.
- `session=morning`(기본): 아침 **장전 재료 리포트** — 전 거래일 15:30 ~ 당일 07:00 재료, 평일 07:15 전후 갱신
- `session=evening`: 저녁 **종가매매 리포트** — 당일 15:30 ~ 19:30 재료 + 시간외(NXT) 시세, 평일 19:35~19:45 갱신.
  NXT 애프터마켓(~20:00)과 다음날 NXT 프리마켓(08:00~) 전에 볼 용도
- 잘못된 값이면 400

### `GET /api/health`
(추가 필드) `latest_theme_day`(마지막 15:35 테마 집계일), `latest_theme_outlook`(테마 전망이 있는 마지막 대상 거래일), `latest_scenario`(마지막 손익 시나리오 날짜, 평일 16:10 생성).
연결 확인. `{"ok": true, "api_version": 1, "now": "...", "latest_article": "...", "latest_report": "2026-11-13",
"latest_evening_report": "2026-11-13"}` (저녁 리포트가 아직 없으면 null)

### `GET /api/dates?limit=60[&session=evening]`
리포트(채점)가 있는 날짜, 최신순.
```json
{"items": [{"date": "2026-11-13", "bullish": 25, "bearish": 11,
            "top": [{"code": "002990", "name": "금호건설", "final": 4}]}]}
```

### `GET /api/report/{YYYY-MM-DD | latest}[?session=evening]`
그날 아침 리포트의 채점 결과.
```json
{
  "date": "2026-11-13",
  "window": {"start": "2026-11-12 15:30:00", "end": "2026-11-13 07:03:37"},
  "weights_version": 1,
  "warnings": ["dart: ... 수집 경고 (있으면 화면 상단에 표시)"],
  "bullish": [{
    "rank": 1, "code": "002990", "name": "금호건설", "sentiment": "호재",
    "final": 4, "final_raw": 3.5,
    "material": "연세대학교 의료원과 3,720억 원 공사 수주(매출 대비 18.43%)",
    "category": "contract", "category_name": "공급계약·수주",
    "basis": ["공시"],                       // 공시 / 뉴스 / 정책 중 해당하는 것 (분류용)
    "basis_labels": ["공시"],                // 표시용: 정책은 부처마다 "정책:산업부", 장중(07:00~15:30) 게시 정책이면 "장중 게시"도 (2026-11-14 추가)
    "caution": "공사 기간 동안 매출 분산 인식",   // 없으면 null
    "factors": {                              // 항목 점수 1~5와 근거 한 줄. 해당 없음은 null
      "scale": {"score": 3, "reason": "..."}, "exclusivity": {...}, "novelty": {...},
      "policy": null, "diffusion": null
    },
    "sources": [{"label": "[공시] 단일판매ㆍ공급계약체결 (11-12 17:36)", "url": "https://dart.fss.or.kr/..."}],
    "item_id": "S-002990"
  }],
  "bearish": [ /* 같은 형식, rank·final은 null — 주의 종목 */ ],
  "reviewed_policies": [{"id": "P-1", "title": "...", "picked": ["394280"], "note": "..."}],
  "report_md_url": "/api/report/2026-11-13/markdown"
}
```
- `final`은 호재 강도 1~5 (매수 추천이 아님). `bullish`는 이미 순위순.
- 공통 추가 필드: `"session": "morning" | "evening"`, `"fallback": false`, `"fallback_reason": null`

**저녁(`session=evening`)에서 달라지는 점**
- `fallback: true`면 Claude 채점이 19:42:30까지 끝나지 않아 **객관 지표만으로 만든 간이 리포트**다
  (`fallback_reason`에 이유). 이때 `final`은 공시 규모 점수이거나 null(점수 없음), 화면에 "간이" 표시 필요
- 각 종목에 `evening_quote`(아침 리포트에서는 null):
```json
"evening_quote": {
  "as_of": "19:31",            // 시세 기준 시각(HH:MM)
  "krx_close": 8630,           // 오늘 KRX 종가(15:30)
  "price": 9150,               // 리포트 시각 NXT 가격
  "after_hours_pct": 6.0,      // 시간외 등락 % = price / krx_close − 1  (단위 %, 소수 아님)
  "since_disclosure_pct": 5.3, // 공시 접수 직전 가격 대비 % (공시 없는 종목은 null)
  "after_value": 6327391880,   // 15:30 이후 거래대금(원)
  "nxt_enabled": true,         // NXT 거래 가능(마스터 표시 또는 18시 이후 체결) — false면 애프터마켓 매수 불가
  "nxt_traded": true           // 18시 이후 실제 체결 있음
}
```
  ※ `evening_quote`의 %는 **이미 % 단위**(6.0 = 6%). 다른 엔드포인트의 비율(0.035 = 3.5%)과 다르니 주의
- factors 키: scale 규모 / exclusivity 최초·독점 / novelty 신규성 / policy 정책 수혜 / diffusion 확산도 /
  quality 재료의 질 / preprice 선반영(높을수록 재료 전에 덜 오름) / theme_cluster 테마 결집도 / leader 테마 대장(테마·정책 종목만).
  v1(11-12·13 리포트)은 앞의 5개만, v2는 7개까지 있다. 없는 키는 빈칸으로 처리.

### `GET /api/report/{YYYY-MM-DD}/markdown[?session=evening]`
리포트 원문 마크다운. `{"date": "...", "markdown": "# 2026-11-13 장전 재료 리포트 ..."}`

### `GET /api/news`
수집한 뉴스 목록(본문 제외), 최신순.

| 인자 | 설명 |
|---|---|
| `source` | `all`(기본) / `naver`(네이버 시황·전망) / `policy`(정책브리핑) / `ministry`(부처 보도자료 11곳, `office`=부처명) |
| `from`, `to` | `YYYY-MM-DD` 또는 `YYYY-MM-DD HH:MM`. 아무 조건도 없으면 최근 2일 |
| `q` | 제목·요약·본문 검색어 |
| `stock` | 종목코드 6자리 — 종목명이 들어간 기사 |
| `limit`, `offset` | 기본 100, 최대 500 |

```json
{"total": 259, "limit": 100, "offset": 0, "count": 100,
 "items": [{"id": 247, "source": "naver", "source_name": "네이버 시황·전망", "office": "한국경제",
            "title": "...", "summary": "...", "published_at": "2026-11-13 07:02:16", "url": "https://n.news.naver.com/..."}]}
```

### `GET /api/news/{id}`
기사 하나 + 본문(`body`, 평문) + 이 기사를 근거로 채점된 종목(`scored_stocks`: date, code, name, sentiment, final).

### `GET /api/disclosures`
DART 공시 목록, 최신순.

| 인자 | 설명 |
|---|---|
| `date` | 접수일 `YYYY-MM-DD` |
| `from`, `to` | 접수 시각 범위. 아무 조건도 없으면 최근 2일 |
| `stock` | 종목코드 |
| `q` | 공시명·회사명 검색 |
| `major` | `1`이면 본문을 저장한 주요 공시만 |
| `listed_only` | 기본 `1`(상장사만). `0`이면 비상장·펀드 포함 |
| `limit`, `offset` | 기본 100, 최대 500 |

항목: `rcept_no, received_at(시각 모르면 null), rcept_date, corp_name, stock_code, market, report_nm, filer,
signal(buy/sell/neutral, 제목 키워드 규칙), confirmed(확정형 재료), has_body, url`

### `GET /api/disclosures/{접수번호 14자리}`
공시 하나 + 본문(`body`, 주요 공시만 있음) + `scored_stocks`.

### `GET /api/stocks?q=검색어`
종목 검색(검색창 자동완성용). 종목명 일부·종목코드 일부 모두 가능, 영문 대소문자 무시, 공백 무시.
정렬: 정확히 일치 → 앞부분 일치 → 포함, 같은 순위면 이름이 짧은 순.

| 인자 | 설명 |
|---|---|
| `q` | 필수. 예: `금호`, `삼성전자`, `0059`, `sk` |
| `limit` | 기본 20, 최대 100 |
| `include_excluded` | `1`이면 ETF/ETN·거래정지·관리종목·상장폐지 추정 종목도 포함(기본 제외) |

```json
{"count": 2, "items": [
  {"code": "002990", "name": "금호건설", "market": "KOSPI", "sector": "...", "is_excluded": false,
   "exclude_reason": null, "last_score": {"date": "2026-11-13", "sentiment": "호재", "final": 4}},
  {"code": "001210", "name": "금호전기", "market": "KOSPI", "sector": "...", "is_excluded": false,
   "exclude_reason": null, "last_score": null}]}
```
`last_score` = 가장 최근에 채점된 날의 결과(없으면 null). 고른 종목은 `/api/stock/{code}`, `/api/news?stock={code}`,
`/api/disclosures?stock={code}`로 이어서 조회한다.

> 뉴스 검색 범위 참고: 저장된 뉴스는 2026-11-11부터, 매일 전일 15:30 ~ 당일 07:00 구간의 네이버 '시황·전망'과
> 정부 정책브리핑 기사다. 장중 기사·종목 개별 뉴스 전체가 있는 것은 아니다. 일주일 검색 예:
> `/api/news?stock=005930&from=2026-11-06`

### `GET /api/stock/{종목코드}`
종목 한 화면용 묶음.
```json
{"stock": {"code": "002990", "name": "금호건설", "market": "KOSPI", "sector": "...", "audit_info": "...",
           "state": "...", "is_listed": true, "is_excluded": false, "exclude_reason": null},
 "themes": [{"source": "naver", "theme": "건설 대표주", "reason": "편입 사유"}],
 "scores": [{"date": "2026-11-13", "sentiment": "호재", "final": 4, "final_raw": 3.5, "material": "...",
             "category_name": "공급계약·수주",
             "reaction": {"gap_pct": 0.02, "oc_pct": 0.01, "oc_excess": 0.005,
                          "ret_1d": null, "ret_3d": null, "ret_5d": null, "complete": 0}}],
 "disclosures": [ /* 최근 30건, /api/disclosures 항목 형식 */ ],
 "news": [ /* 종목명이 들어간 최근 기사 30건, /api/news 항목 형식 */ ]}
```
`reaction`은 그날 20:10 이후에 채워진다(전에는 null).

추가 필드 `evening_scores` — 그 종목이 저녁 종가매매 리포트에 나온 이력(최근 30건):
```json
"evening_scores": [{"date": "2026-11-13", "sentiment": "호재", "final": 4, "final_raw": 4.32, "material": "...",
  "category_name": "공급계약·수주", "fallback": false,
  "quote": { /* evening_quote와 같은 형식 */ },
  "next_day": {"open_ret": 0.031, "high_ret": 0.22, "close_ret": 0.18}}]   // 진입가(리포트 시각 NXT 가격) 대비, 소수.
                                                                            // 다음 거래일 20:10 이후 채워짐(전에는 null)
```

### 리포트 항목의 `after` (이후 주가) — `/api/report/{날짜}` 아침·저녁 공통
`bullish`·`bearish` 각 항목에 `after`가 붙는다. 종목마다 `/api/stock`을 따로 부를 필요가 없다. 응답 맨 위 `after_d0` = 기준일(D0):
아침 리포트는 그날, 저녁 종가매매 리포트는 다음 거래일.
```json
"after": {"d0": "2026-11-12", "status": "집계 중", "close_basis": "KRX 종가",
          "open": 10230.0, "high": 10340.0, "low": 9040.0, "close": 9170.0,
          "gap_pct": 1.59, "oc_pct": -10.36, "excess_pct": -9.56, "ret_1d_pct": -7.43, "ret_3d_pct": null, "ret_5d_pct": null}
```
- `status`: `집계 전`(D0 봉이 아직 없음 — 모든 값 null) / `집계 중`(일부 값만, 나머지 null) / `완료`(5일 수익률까지).
  D0 봉은 그날 20:10 주가 반응 기록 뒤에 생긴다.
- `gap_pct` 시초가 갭(전일 KRX 종가 대비) · `oc_pct` 시가→종가 · `excess_pct` 시가→종가에서 같은 시장 지수의 시가→종가를 뺀 값 ·
  `ret_Nd_pct` D+N 종가 / D0 시가 − 1. 모두 퍼센트 단위.
- `close`는 KRX 종가(다음 거래일 기준가로 확정). 아직이면 통합 종가(NXT 애프터마켓 포함)이고 `close_basis`에 그렇게 적힌다.

### `GET /api/theme-outlook/{YYYY-MM-DD | latest}[?phase=evening|morning]`
**예측 대상 거래일** 기준 테마 전망(리포트에 실린 것 그대로) + 그날 15:35 채점 결과. 하루에 두 번 만들어진다:
- `evening` **1차** — 전 거래일 19:30 저녁 종가매매 리포트에 실림(그날 15:35 집계 + 15:30~19:30 재료). 예: 11-13 저녁 → `date` 11-16
- `morning` **최종** — 그날 07:00 아침 리포트에 실림(밤사이 재료 전부 반영)

`phase`를 빼면 최종이 있으면 최종, 없으면 1차. `latest` = 가장 최근 대상일. 없으면 404.
아침 리포트 `/api/report/{날짜}` 응답의 `theme_outlook` = 그날 최종, 저녁 리포트 `/api/report/{날짜}?session=evening`의
`theme_outlook` = 그 저녁에 만든 다음 거래일 1차(없으면 null).
추가 필드: `phase`, `made_at`(만든 시각), `available_phases`(그 대상일에 있는 단계 목록).
```json
{"date": "2026-11-16", "phase": "evening", "made_at": "2026-11-13 19:31", "available_phases": ["evening"],
 "prev_day": "2026-11-13", "approx": false,
 "leaders_yday": [{"group_id": "naver:545", "name": "뉴로모픽 반도체", "median_excess": 4.4, "avg_excess": 9.1,
                   "wavg_excess": 8.0, "rise_ratio": 1.0, "up5": 3, "limit_ups": 1, "stage": "태동", "streak": 1,
                   "tv_ratio20": 0.7, "leader_changed": 0, "related": ["창투사"], "approx": 0,
                   "leader":    [{"code": "394280", "name": "오픈엣지테크놀로지", "change_pct": 29.9, "limit_hit": 1,
                                  "closed_at_limit": 1, "first_hit": "09:02", "nxt": true, "label": "대장"}],
                   "seconds":   [ /* 2등주, 같은 형식 */ ],
                   "followers": [ /* 후발주 최대 3, 같은 형식 */ ]}],
 "small_yday": [{"name": "건설_국내주택", "median_excess": 5.3, "n": 4}],
 "hedge": {"down_days": 11, "display_only": false,
           "items": [{"name": "제지_기타", "cum_excess": 31.5, "cum_pct": 1.6, "days": 11, "leader_name": "에스엠국일"}]},
 "forecast": [{"group_id": "naver:9", "name": "퓨리오사AI", "score": 0.665, "confidence": "상",
               "components": {"stage": 0.75, "breadth": 0.91, "trade_value": 1.0, "material": 0.0},
               "basis": ["태동(1일)", "전일 중앙값 초과 +4.2%p·상승 82%", "거래대금 3.3배"],
               "stage": "태동", "leader_code": "...", "leader_name": "DSC인베스트먼트",
               "seconds": ["..."], "second_names": ["포바이포", "TS인베스트먼트"],
               "nxt": {"종목코드": false}, "related": ["창투사"]}],
 "baseline": [ /* 기준선(어제 상위 유지) 5개, forecast와 같은 형식(점수=전일 중앙값 초과, basis 없음) */ ],
 "track": {"forecast": {"days": 12, "avg_excess": 0.8, "top10_ratio": 0.46, "leader_ratio": 0.3},
           "baseline": {"days": 12, "avg_excess": 0.5, "top10_ratio": 0.38, "leader_ratio": 0.25}},
 "results": [{"kind": "forecast", "dedupe": 1, "n": 5, "avg_excess": 2.1, "top10_hits": 3, "top10_ratio": 0.6,
              "leader_hits": 2, "leader_n": 5, "second_hits": 3, "second_n": 9}]}
```
- `forecast`·`baseline`·`leaders_yday`는 **중복 회피 적용**(대장 같거나 상승 종목이 겹치는 묶음은 `related`로) — 아침 리포트와 같은 목록.
- `results`는 대상일 15:35 이후에 생긴다(그 전엔 빈 배열, 단계별로 따로 채점). `track`은 같은 단계의 그 전날까지 누적 성적(최근 20일).
- 저녁 1차의 `leaders_yday`는 "오늘(장 마감) 주도 테마", 아침 최종에서는 "어제 주도 테마" — 같은 집계일(`prev_day`)이다.
- `nxt`가 false = 종목 마스터상 NXT 거래 불가(실제로 거래되는 경우가 있어 "NXT✕?"로 표시 권장). null = 정보 없음.
- `%` 값은 퍼센트 단위, `rise_ratio`·`top10_ratio`·`leader_ratio`·`components`는 0~1.

### `GET /api/scenario/{YYYY-MM-DD | latest}`
일일 손익 시나리오(평일 16:10). 그날까지의 장전 리포트 종목(호재·악재·비교 기준 후보)을 1분봉으로 시뮬레이션한 손절·익절 추천.
`latest` = 마지막 생성일. 없으면 404. 리포트 `reports/scenario/날짜.md`와 같은 내용이다.
```json
{"date": "2026-11-20", "n_trades": 380, "n_good": 160, "n_combos": 1134,
 "sel_days": ["2026-11-12", "2026-11-13"],          // 선정 구간(최근 1주 제외) — 비어 있으면 추천 없이 참고만
 "week_days": ["2026-11-16", "...", "2026-11-20"],   // 검증 구간(최근 1주)
 "segments": {
   "호재 전체": {"entry": "krx_open", "combo": [-4, 6, "pct3"],   // [손절%, 익절%, 방식] 방식: full | pctX | tickN
                "reference_only": false, "confidence": "보통", "basis_n": 54, "sel_mean": 0.0081,
                "basis": {"n": 54, "mean": 0.0081, "win": 0.56, "max_loss": -0.047, "pf": 1.4, "amb": 2},
                "month": null, "use_month": false,
                "oos": {"n": 31, "mean": 0.0032, "win": 0.52, "max_loss": -0.046, "pf": 1.1, "amb": 0}, "overfit": false,
                "zone": {"stop": [-3, -5], "take": [5, 8], "cells": 9},
                "single": {"entry": "nxt_early", "combo": [-2, 9, "full"], "stats": {"n": 20, "mean": 0.015}},
                "entries": {"nxt_early": {"combo": [...], "score": 0.004, "basis": {...}, "zone": {...}}, "nxt_late": {}, "krx_open": {}},
                "periods": {"week": {...}, "month": {...}, "all": {...}},
                "status_counts": {"nxt_early": {"ok": 30, "illiquid": 12, "na": 8}, "krx_open": {"ok": 49, "limit": 1}}},
   "2점": {}, "3점": {}, "4점↑": {}, "공시": {}, "뉴스": {}, "정책": {}, "악재": {}, "비교 기준": {}},
 "changes": ["3점: 손절 -5→-4%, 방식 전량 익절→절반 익절+트레일 3%"],
 "notes": ["4점↑: 최적값이 범위 끝에 걸려 손절 -2~-9% · 익절 +2~+10%로 확장"],
 "assumptions": {"cost_pct": 0.25, "stop_slippage_pct": 0.2, "nxt_entry_slippage_pct": 0.3,
                 "krx_entry_slippage_pct": 0.1, "nxt_min_amount": 10000000, "min_n": 30}}
```
- 수익률·승률은 소수(0.0081 = +0.81%). `entry`: `nxt_early` NXT 08:00 시가 · `nxt_late` NXT 08:40 시가 · `krx_open` KRX 시가.
- `reference_only: true`면 선정 구간 표본이 없어 최근 1주 결과를 참고로만 보인 것(추천 아님). `악재`·`비교 기준`은 `호재 전체` 조합을 적용한 참고값.
- `status_counts`: `ok` 시뮬레이션 · `illiquid` NXT 진입 봉 거래대금 부족(NXT만) · `limit` 시가 상한가(매수 불가) · `na` 봉 없음.

**개별 매매 `trades`** (종목 × 진입 시점마다 한 행, `na`는 제외). 옵션: `?trade_date=YYYY-MM-DD`·`?code=종목코드`로 거르기,
`?trades=0`이면 빼고(약 16KB), `?bars=1`이면 각 행에 `bars`(진입 봉 ~ 15:30 1분봉 `[hhmm, 시, 고, 저, 종, 거래량]`)를 붙인다 —
이 `bars`가 붙은 `trades`만으로 손절·익절 시뮬레이션을 그대로 다시 계산할 수 있다(`scenario.trades_from_rows` → `analyze`, 같은 결과).
```json
{"date": "2026-11-12", "code": "093370", "name": "후성", "score": 3, "basis": ["공시"], "sentiment": "호재",
 "group": "호재", "segments": ["호재 전체", "3점", "공시"], "entry": "krx_open", "entry_type": "KRX 09:00", "status": "ok",
 "entry_price": 10240.23, "prev_close": 10070.0, "open": 10230.0, "high": 10340.0, "low": 9040.0, "close": 9170.0,
 "high_pct": 1.08, "low_pct": -11.63, "close_pct": -10.36, "entry_amount": 1517677593,
 "first_hit": "high", "first_hit_reason": null, "bars_n": 380,
 "exit": {"combo": [-1, 5, "pct3"], "label": "손절 -1% · 익절 +5% · 절반 익절+트레일 3%", "reason": "손절",
          "ret_pct": -1.45, "ambiguous": false}}
```
- 가격은 원 단위 원값. `open`은 진입 봉 시가(슬리피지 전), `entry_price`는 슬리피지를 더한 진입가, `close`는 15:30 KRX 종가.
  `high`·`low`는 진입 봉부터 15:30까지. `*_pct`는 `open` 대비 %(퍼센트 단위).
- `first_hit`: 그 구간 최고가와 최저가 중 먼저 나온 쪽(`high`/`low`). 모르면 null이고 `first_hit_reason`에 이유(분봉 없음, 같은 1분봉에서 둘 다 발생).
- `exit`: `호재 전체` 추천 조합을 이 매매에 적용한 청산(사유 손절/익절/익절+트레일/종가, `ret_pct`는 비용·슬리피지 뺀 %).
  `status`가 `limit`(시가 상한가, 매수 불가)이면 null. `group`: 호재 / 악재 / 비교 기준.

### `GET /api/themes/{YYYY-MM-DD | latest}[?limit=30]`
그날 15:35 테마 집계(묶음 단위, 중앙값 초과 순위)와 그날 아침 예측·채점.
```json
{"date": "2026-11-16", "approx": false, "count": 30,
 "items": [{"group_id": "naver:123", "group_name": "온디바이스 AI (+유사 1개)", "rank": 1, "n": 24,
            "median_pct": 6.1, "avg_pct": 8.3, "wavg_pct": 9.0, "median_excess": 5.2, "avg_excess": 7.4, "wavg_excess": 8.1,
            "rise_ratio": 0.83, "up5": 9, "limit_ups": 2, "trade_value": 1.2e12, "tv_ratio20": 2.4,
            "concentration": 0.31, "strong": 1, "streak": 2, "cum_excess": 9.1, "stage": "확산",
            "leader_code": "394280", "leader_changed": 0, "small": 0, "approx": 0,
            "members": [{"code": "394280", "name": "오픈엣지테크놀로지", "label": "대장", "change_pct": 29.9,
                         "limit_hit": 1, "first_hit": "09:02", "trade_value": 3.1e11}]}],
 "forecasts": [{"kind": "forecast", "rank": 1, "group_name": "...", "score": 0.71, "confidence": "상",
                "components": {"stage": 1.0, "breadth": 0.8, "trade_value": 0.7, "material": 0.25},
                "leader_pick": "394280", "second_picks": ["399720"], "related": ["HBM"]}],
 "results": [{"kind": "forecast", "dedupe": 1, "n": 5, "avg_excess": 2.1, "top10_hits": 3, "top10_ratio": 0.6,
              "leader_hits": 2, "leader_n": 5, "second_hits": 3, "second_n": 9}]}
```
- `%` 값은 이미 퍼센트 단위(5.2 = 5.2%), `rise_ratio`·`top10_ratio`는 0~1. `trade_value`는 원.
- `members`는 라벨(대장/2등주/후발주)이 있는 상위 8종목. `forecasts.kind` = forecast(예측) / baseline(어제 상위 유지).
- `approx: true`는 통합 일봉으로 근사 재구성한 날(시간외 포함).

## 예시 (Python 표준 라이브러리)
```python
import json, os, urllib.request
BASE = os.environ.get("STOCK_NEWS_API", "http://192.168.0.12:8770")
TOKEN = os.environ["STOCK_NEWS_API_TOKEN"]

def api(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

rep = api("/api/report/latest")
for it in rep["bullish"][:7]:
    print(it["rank"], it["name"], it["final"], it["basis"], it["material"])
```

## 운영 (미니PC)
- 실행: `python3 scripts/serve_api.py` (포트 `.env` API_PORT, 기본 8770). 로그 `data/logs/api.log`
- 자동 시작: crontab의 `@reboot`와 10분 감시 줄(`--quiet-if-running`: 이미 떠 있으면 바로 종료)
- 토큰 교체: `.env`의 API_TOKEN을 바꾸고 서버 재시작 → 클라이언트 `.env`도 같이 변경
- 허용 네트워크 변경: `.env` `API_ALLOW_NETS=192.168.0.0/24` (쉼표로 여러 개)
