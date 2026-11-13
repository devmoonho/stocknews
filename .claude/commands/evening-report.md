---
description: 당일 15:30 이후 공시·뉴스 → 저녁 종가매매(NXT 애프터마켓) 리포트
---
오늘 저녁 종가매매 리포트를 만든다. 사용자에게는 한국어로 답한다. 최종 매매 판단은 사용자가 한다 — 주문하지 않는다.
시간이 중요하다(20:00 NXT 마감). 단계마다 오래 머물지 않는다.

1. `python3 scripts/collect.py` — 경고는 리포트 상단에 그대로.
2. `python3 scripts/match.py --session evening` — 공시·제목 기사 종목의 시간외 시세(1분봉)까지 붙는다.
3. `config/scoring_rubric.md`와 `data/work/<날짜>_evening/candidates_digest.md`를 읽고 채점표 v3대로
   `data/work/<날짜>_evening/scores.json`을 쓴다. A(공시)·B(제목 기사)·E(정책)·D(테마) 순으로 중요한 것부터, C는 건너뛴다.
   contract·mna·buyback은 quality 필수, preprice·theme_cluster·leader는 쓰지 않는다.
4. `python3 scripts/score.py --session evening` — 오류가 없을 때까지 고친다.
5. `python3 scripts/report.py --session evening` → `reports/evening/<날짜>.md`
6. 메일 `python3 scripts/send_report.py --session evening`, 텔레그램 `python3 scripts/send_telegram.py --session evening`.

끝나면 상위 종목(점수·시간외 등락·NXT 가능 여부·재료 한 줄)과 경고를 알려준다. 매수 추천 표현은 쓰지 않는다.
