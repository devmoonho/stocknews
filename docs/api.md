# 장전 리포트 읽기 전용 API (v1)

미니PC(`192.168.0.12`)의 `scripts/serve_api.py`가 제공한다. 조회(GET)만 있고 주문·수정 기능은 없다.

- 기본 주소: `http://192.168.0.12:8770`
- 인증: 모든 요청에 헤더 `Authorization: Bearer <API_TOKEN>` (또는 `X-API-Token: <API_TOKEN>`)
  - 토큰은 미니PC `~/stock_news/.env`의 `API_TOKEN` 값. 클라이언트 쪽 `.env`에 넣고 코드·로그에 남기지 않는다.
- 접속 허용: 내부망(사설 IP)만. 그 밖에서는 403.
- 응답: JSON(UTF-8). 시각은 KST `YYYY-MM-DD HH:MM:SS`. 비율은 소수(0.035 = 3.5%).
- 오류: `{"error": "메시지"}` + 상태 코드 400(잘못된 인자) / 401(토큰) / 403(네트워크) / 404(없음) / 405(GET 외) / 500
- 데이터 갱신 시각: 평일 07:00~07:15 아침 리포트(뉴스·공시·채점), 16:10 주가 반응. 그 외 시간엔 데이터가 바뀌지 않는다
  → 클라이언트는 수 분 간격 이상으로 조회하면 충분하다.

## 엔드포인트

### `GET /api/health`
연결 확인. `{"ok": true, "api_version": 1, "now": "...", "latest_article": "...", "latest_report": "2026-11-13"}`

### `GET /api/dates?limit=60`
리포트(채점)가 있는 날짜, 최신순.
```json
{"items": [{"date": "2026-11-13", "bullish": 25, "bearish": 11,
            "top": [{"code": "002990", "name": "금호건설", "final": 4}]}]}
```

### `GET /api/report/{YYYY-MM-DD | latest}`
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
    "basis": ["공시"],                       // 공시 / 뉴스 / 정책 중 해당하는 것
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
- factors 키: scale 규모 / exclusivity 독점성 / novelty 새로움 / policy 정책 연관 / diffusion 확산도

### `GET /api/report/{YYYY-MM-DD}/markdown`
리포트 원문 마크다운. `{"date": "...", "markdown": "# 2026-11-13 장전 재료 리포트 ..."}`

### `GET /api/news`
수집한 뉴스 목록(본문 제외), 최신순.

| 인자 | 설명 |
|---|---|
| `source` | `all`(기본) / `naver`(네이버 시황·전망) / `policy`(정책브리핑) |
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
`reaction`은 그날 16:10 이후에 채워진다(전에는 null).

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
