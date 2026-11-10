# 04. Rules-as-Code 설계 (L1)

## 1. 목표
복지서비스 선정기준을 **사람이 읽을 수 있고, 기계가 실행할 수 있으며, Z3로 번역 가능한** 규칙으로 표현한다.
같은 규칙 파일을 ①평가기(engine) ②형식검증(encoder) ③LLM 추출 결과 비교에 공통으로 쓴다.

## 2. DSL (YAML)

### 2.1 파라미터 파일 `rules_db/params/2026.yaml`
```yaml
year: 2026
median_income:            # 기준중위소득(월, 원) — 가구원수별
  values: {1: null, 2: null, 3: null, 4: null, 5: null, 6: null}   # 고시문 값으로 채울 것
  source: "보건복지부 고시 제2025-○○호 (2026년 기준중위소득)"
  verified: false
ratios:
  livelihood: {value: 0.32, source: "e-나라지표 국민기초생활보장 수급 현황 지표설명", verified: true}
  medical:    {value: 0.40, source: "동일", verified: true}
  housing:    {value: 0.48, source: "동일", verified: true}
  education:  {value: 0.50, source: "동일", verified: true}
income_recognition:      # 소득인정액 산정 파라미터(단순화)
  earned_income_deduction_rate: {value: null, source: "2026 국민기초생활보장사업안내 p.○", verified: false}
  property_conversion_rate_monthly:
    general: {value: null, source: "...", verified: false}
    financial: {value: null, source: "...", verified: false}
    car: {value: null, source: "...", verified: false}
  basic_property_deduction_by_region: {...}
```
`null`인 값은 M0에서 원문을 보고 채운다. 채우기 전에는 평가기가 오류를 낸다(조용히 0으로 처리하지 않음).

### 2.2 규칙 파일 `rules_db/approved/NBLS_LIVELIHOOD.yaml`
```yaml
id: NBLS_LIVELIHOOD
name: 생계급여
level: central            # central | local
region: null              # 지자체 사업이면 "대구광역시" 등
unit: household
source_refs:
  - {doc: "2026 국민기초생활보장사업안내", page: null, clause: "생계급여 선정기준"}
derived:                  # 이 규칙에서 쓰는 파생 변수
  sel_line: "median_income(hh.size) * ratios.livelihood"
eligibility:
  all:
    - {expr: "income_recognized(hh) <= sel_line", ref: 0}
benefit:
  monthly: "max(0, sel_line - income_recognized(hh))"
exclusions: []            # 다른 제도와의 중복 제한: [{program: X, rule: "..."}]
status: approved          # draft | review | approved
tests: tests/rules/NBLS_LIVELIHOOD.yaml
```

### 2.3 표현식 언어 (제한된 부분집합)
- 허용: 정수/실수 상수, `+ - * /`(상수 곱셈·나눗셈만), 비교, `and/or/not`, `min/max`, `if(c, a, b)`, 테이블 조회(`median_income(n)`), 가구 집계 함수(`count(members, age>=65)` 등)
- 금지: 임의 함수 호출, 변수×변수 곱셈(Z3 비선형 방지 — 필요 시 경우 분할)
- 파서: Python `ast`로 파싱 후 화이트리스트 노드만 허용 → 같은 AST를 ①Python 평가 ②Z3 식 생성에 사용

## 3. 평가기 (`rules/engine.py`)
```python
def evaluate(hh: Household, programs: list[Program], params: Params) -> list[Decision]: ...
```
- `income_recognized(hh)` = 소득평가액 + 재산의 소득환산액 (`rules/income.py`, 단순화 가정 목록을 docstring과 화면에 표시)
- `exclusions` 처리: 1차로 각 제도 독립 판정 → 2차로 중복 제한 적용
- 모든 조건 평가값을 `trace`에 기록

## 4. LLM 추출 파이프라인 (`rules/extract.py`)
```
Service 원문 ─► [프롬프트 A: 구조화 추출] ─► 규칙 JSON 초안 A
             └► [프롬프트 B: 독립 재추출] ─► 규칙 JSON 초안 B
                         │
                 crosscheck.py: A·B 정규화 후 비교
                   ├─ 일치 → testgen.py로 경계값 테스트 생성·실행 → 통과 시 review/ (사람 1클릭 승인 → approved/)
                   └─ 불일치 → review/ (차이 하이라이트)
```
- 출력은 반드시 §2 스키마의 JSON(도구 호출/구조화 출력 사용). 스키마 위반 시 1회 재시도 후 review.
- 프롬프트 필수 지시: 원문에 없는 수치를 만들지 말 것, 불확실 조건은 `uncertain: true`와 원문 인용(`quote`)을 남길 것, "기준중위소득 N% 이하"는 `ratios`/`median_income` 참조로 표현할 것.
- 원문 인용(`quote`)은 원문 부분 문자열인지 코드로 검사(환각 탐지).
- 캐시 키: (serv_id, 원문 해시, 프롬프트 버전, 모델명).

## 5. 테스트
- 수작업 규칙: `tests/rules/*.yaml`에 가구 입력 → 기대 자격·급여. 사업안내 예시를 우선 사용.
- 자동 테스트: 선정선 −1원/정확히 선정선/+1원 경계 가구 자동 생성.
- 속성 테스트(hypothesis): 소득이 증가하면 소득인정액은 감소하지 않는다 등 단조성.

## 6. MVP 대상 제도
| ID | 제도 | 작성 방식 |
|---|---|---|
| NBLS_LIVELIHOOD | 생계급여 | 수작업 |
| NBLS_MEDICAL | 의료급여 | 수작업 |
| NBLS_HOUSING | 주거급여(임차 기준 단순화) | 수작업 |
| NBLS_EDUCATION | 교육급여 | 수작업 |
| BASIC_PENSION | 기초연금(선정기준액 비교 단순화) | 수작업 |
| ENERGY_VOUCHER | 에너지바우처(수급+세대원 특성) | 수작업 |
| EITC | 근로장려금(연간, 단순화) | 수작업 |
| LOCAL_* | 대구·경북 지자체 사업 5~20건 | LLM 추출 + 검수 |
