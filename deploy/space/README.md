---
title: WelfareTwin API
colorFrom: yellow
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: 복지트윈 실시간 계산 API (Rules-as-Code · Z3 · 합성 인구)
---

# 복지트윈 실시간 계산 API

[복지트윈 웹 데모](https://welfaretwin.vercel.app)가 부르는 계산 서버입니다. 소스: https://github.com/LeeJaeHaeng/WelfareTwin

- `GET /health` · `GET /programs` · `GET /regions`
- `POST /evaluate` 가구 → 제도별 자격·급여·탈락 사유
- `POST /sweep` 가구 → 근로소득 0~350만원 구간별 제도 급여(급여 절벽)
- `POST /verify/blindspot` · `POST /verify/cliff` Z3 형식검증
- `POST /scenario` 생계급여 선정기준·신청 방식 변경 시뮬레이션(합성 인구 1만 가구 표본)

판정은 승인된 규칙(YAML)과 같은 엔진으로 합니다. 실제 개인 데이터는 없고 공개 집계표로 만든 합성 가구만 있습니다.
공개 서버라 LLM 은 부르지 않습니다. 무거운 계산은 한 번에 하나, IP 당 분당 6회까지입니다. API 문서: `/docs`
