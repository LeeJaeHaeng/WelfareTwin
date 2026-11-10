# 복지트윈 (WelfareTwin)

2026 사회보장 데이터를 활용한 AI 혁신 아이디어 경진대회 출품작 구현 저장소.
작업 가이드는 `CLAUDE.md`, 진행 상황은 `docs/01_구현계획서.md` 참고.

```
uv sync
cp .env.example .env   # 키 입력
uv run pytest -q
uv run python -m welfaretwin.data.collect --source central --source local
```
