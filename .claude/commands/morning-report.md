---
description: 전일 15:30 이후 뉴스·공시 수집 → 호재 채점 → 아침 리포트 작성
---

CLAUDE.md의 "아침 절차"를 순서대로 실행한다. 사용자에게는 한국어로 짧게 진행 상황을 알린다.

$ARGUMENTS 에 날짜/시간대가 있으면(예: `--since "2026-11-11 15:30" --until "2026-11-12 07:00"`) 2·3단계 스크립트에 그대로 넘긴다.

1. `python3 scripts/update_master.py --skip-if-fresh` (오래 걸리면 백그라운드로 돌리고 끝날 때까지 기다린다)
2. `python3 scripts/collect.py $ARGUMENTS`
   - 경고가 있으면 메모해 두고 리포트 상단에 넣는다. 수집 0건이면 멈추고 사용자에게 알린다.
3. `python3 scripts/match.py $ARGUMENTS`
4. `config/scoring_rubric.md`를 읽고, `data/work/<날짜>/candidates_digest.md`를 읽어 채점한다.
   다이제스트 E(정부 정책뉴스)는 하나씩 모두 검토해 수혜 종목(P-번호:코드)을 고르거나, 고르지 않은 이유를 reviewed_policies에 적는다.
   다이제스트 G(후속 이벤트)도 확인해 재채점할 것은 F-번호:코드로 넣는다.
   contract·mna·buyback은 `quality`(재료의 질) 필수. `preprice`·`theme_cluster`·`leader`는 쓰지 않는다(score.py가 계산).
   정책 재료 규모는 채점표 v3 기준(범주 명시 + 예산). 다이제스트 D·E의 대장 후보 순위를 참고한다.
   필요한 근거는 candidates.json·SQLite에서 확인한다. 결과를 `data/work/<날짜>/scores.json`에 쓴다.
5. `python3 scripts/score.py --date <날짜>` — 오류가 없을 때까지 scores.json을 고쳐 다시 실행한다.
6. `python3 scripts/report.py --date <날짜>` → `reports/<날짜>.md`
7. `.env`에 MAIL_SMTP_PASSWORD가 있으면 `python3 scripts/send_report.py --date <날짜>`로 메일 발송. 없으면 건너뛰고 알린다.
8. `python3 scripts/send_telegram.py --date <날짜>` — 텔레그램 요약(설정 없으면 건너뜀).

끝나면 점수 높은 순 상위 종목(점수·재료 한 줄)과 경고, 리포트 경로를 알려준다. 매수 추천 표현은 쓰지 않는다.
