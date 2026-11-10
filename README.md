# 복지트윈 (WelfareTwin)

2026 사회보장 데이터를 활용한 AI 혁신 아이디어 경진대회 출품작 구현 저장소.
사회보장제도를 실행 가능한 규칙(Rules-as-Code)으로 옮기고, Z3 형식검증으로 사각지대·급여 절벽·중복을 찾고,
합성 인구 + LLM 에이전트로 정책 변화를 미리 시뮬레이션한다.

작업 가이드는 `CLAUDE.md`, 진행 상황은 `docs/01_구현계획서.md`, 측정 결과는 `reports/`.

## 빠른 시작(데모)

```
uv sync
uv run streamlit run app/dashboard.py          # 사전 계산본(artifacts/demo)으로 오프라인 동작
```

화면: 1 복지 법전 → 코드 / 2 사각지대 / 3 급여 절벽 / 4 정책 시뮬레이터 + 레드팀 / 5 백테스트.
사이드바 '모드'에서 실시간 실행(Z3·평가기·LLM 캐시)으로 바꿀 수 있다.

## 전체 재현

`.env.example` 을 `.env` 로 복사해 키를 채운다(공공데이터포털, KOSIS, Anthropic).

```
uv run pytest -q                                                   # 테스트
uv run python -m welfaretwin.data.collect --source central --source local   # 복지서비스 원문(일일 한도 → 이어받기)
uv run python -m welfaretwin.data.stats                            # e-나라지표 수급 통계
uv run python -m welfaretwin.population.synth                      # 합성 인구 10만 가구(KOSIS 캐시 data/raw/kosis 사용)
uv run python -m welfaretwin.sim.static                            # 전 가구 평가 + 실적 대비 보정 리포트
uv run python -m welfaretwin.verify.run --query all                # Z3 사각지대·급여 절벽·중복/사문화
uv run python -m welfaretwin.rules.pipeline run WLF00005691 ...    # 지자체 사업 LLM 추출(A·B) → 교차검증 → review/
uv run python -m welfaretwin.rules.pipeline approve LOCAL_WLF00005691   # 사람 승인
uv run python -m welfaretwin.agents.behavior                       # 페르소나 행동 응답(LLM, 캐시)
uv run python -m welfaretwin.sim.scenario --preset demo            # 정책 시나리오(실수급 통계 보정)
uv run python -m welfaretwin.redteam.report --preset heating       # 사업안 레드팀 리포트
uv run python -m welfaretwin.backtest.run                          # 백테스트(B2 2024→2025)
uv run python -m welfaretwin.demo_bundle                           # 대시보드용 사전 계산본 묶음
python app/rehearsal.py --rounds 3 --shots artifacts/rehearsal     # 데모 리허설 자동 점검(대시보드 실행 중)
```

## 운영

- 원문 수집은 공공데이터포털 일일 한도 때문에 매일 이어받는다: `scripts/register_collect_task.cmd` 로 매일 01:30 예약
  (해제 `schtasks /Delete /TN WelfareTwin_Collect /F`, 로그 `.cache/collect_daily.log`, PC 가 켜져 있어야 실행).
- 발표 자료: 대본 `docs/12_발표_대본.md`, 질의응답 `docs/11_질의응답_대비표.md`, 수치 `reports/eval_*.md`,
  백업 영상 `python app/rehearsal.py --rounds 1 --video artifacts/rehearsal` → `artifacts/rehearsal/demo_backup.webm`.

## 원칙

- 판정은 코드가 한다. LLM 은 규칙 추출(이중 생성·Z3 교차검증·사람 승인)과 행동 추정(실제 통계로 보정)에만 쓴다.
- 모든 제도 수치는 `rules_db/params/*.yaml` 에 출처(사업안내 쪽수·고시·URL)와 함께. 단순화 가정은 각 규칙의 `assumptions` 에 명시.
- 실제 개인 데이터 미사용 — 공개 집계표로 만든 합성 가구만.
