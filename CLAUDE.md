# CLAUDE.md — 복지트윈(WelfareTwin) 구현 가이드

이 폴더는 「2026 사회보장 데이터를 활용한 AI 혁신 아이디어 경진대회」 출품작 **복지트윈**의 구현 저장소다.
작업을 시작하기 전에 이 파일과 `docs/00_README.md`를 먼저 읽고, 해당 작업의 설계서(`docs/0x_*.md`)를 따른다.

## 프로젝트 한 줄 요약
사회보장제도를 AI로 **실행 가능한 코드(Rules-as-Code)** 로 바꾸고, **형식검증(Z3)** 으로 사각지대·급여 절벽·중복을 수학적으로 찾아내며, **합성 인구 + LLM 에이전트**로 정책 변화를 미리 시뮬레이션하는 정책 디지털 트윈.

## 일정 (절대 기준)
| 날짜 | 이벤트 | 구현 의미 |
|---|---|---|
| 2026-11-20(금) | 서류 접수 마감 | 제안서는 이미 완료. 구현과 무관 |
| 2026-11-23(월) | 서류심사 | — |
| 2026-11-25(수) | 본선 진출 발표 | 이때까지 MVP 핵심(M1~M3) 동작 |
| 2026-12-03(목) | 본선 발표 (5분 발표 + 5분 질의) | 데모 완성·리허설 완료 |

## 작업 원칙
1. **데모가 우선**: 12/3에 "심사위원 앞에서 30초 안에 결과가 나오는 장면"이 최종 목표다. 범위 확장보다 데모 안정성을 우선한다.
2. **정답을 지어내지 않는다**: 기준중위소득, 선정기준 비율, 재산 환산율 등 모든 제도 수치는 `rules_db/params/*.yaml`에 **출처(문서명·쪽수·URL)와 함께** 저장하고, 코드에 하드코딩하지 않는다. 출처를 확인하지 못한 값은 `verified: false`로 표시한다.
3. **LLM 결과는 항상 검증 대상**: LLM이 만든 규칙은 테스트케이스와 교차검증을 통과해야 `rules_db/approved/`로 승격된다.
4. **개인정보 0**: 실제 개인 단위 데이터는 사용하지 않는다. 합성 인구만 쓴다.
5. **비용 통제**: LLM 호출은 캐시(`.cache/llm/`)를 거친다. 에이전트 시뮬레이션은 가구 10만 개에 직접 호출하지 않고 페르소나 클러스터(약 200개) 단위로 호출한다.
6. 작업이 끝날 때마다 `docs/01_구현계획서.md`의 체크리스트를 갱신한다.

## 기술 스택
- Python 3.12, 패키지 관리 `uv`
- 규칙 엔진: 자체 DSL(YAML) → Python 평가기 + Z3 인코더 (`z3-solver`)
- 데이터: `pandas`/`polars`, `httpx`(공공데이터포털 API)
- LLM: Anthropic Claude API (`anthropic` SDK), 모델명은 환경변수 `WT_LLM_MODEL`
- API 서버: FastAPI / 대시보드: Streamlit (+ Plotly)
- 테스트: `pytest`, `hypothesis`(속성 기반 테스트)

## 환경 변수 (`.env`, 커밋 금지)
```
DATA_GO_KR_API_KEY=   # 공공데이터포털 일반 인증키(Decoding)
ANTHROPIC_API_KEY=
WT_LLM_MODEL=         # 예: 사용 가능한 최신 Claude 모델
```

## 자주 쓰는 명령
```
uv sync
uv run pytest -q
uv run python -m welfaretwin.data.collect --source central --source local
uv run python -m welfaretwin.rules.extract --service-id <ID>
uv run python -m welfaretwin.verify.run --query blindspot
uv run streamlit run app/dashboard.py
```

## 문서 지도
- `docs/00_README.md` 전체 개요·문서 목록
- `docs/01_구현계획서.md` 마일스톤·WBS·체크리스트 ← **진행 상황의 단일 기준**
- `docs/02_시스템_아키텍처.md` 구성요소·디렉터리 구조
- `docs/03_데이터_설계.md` 데이터 소스·수집·스키마
- `docs/04_RulesAsCode_설계.md` 규칙 DSL·LLM 추출·교차검증
- `docs/05_형식검증_설계.md` Z3 인코딩·질의
- `docs/06_합성인구_에이전트_설계.md` 합성 인구·LLM 에이전트·백테스트
- `docs/07_레드팀_설계.md` 정책 레드팀
- `docs/08_API_대시보드_설계.md` API·화면
- `docs/09_테스트_검증계획.md`
- `docs/10_데모_발표_시나리오.md`
