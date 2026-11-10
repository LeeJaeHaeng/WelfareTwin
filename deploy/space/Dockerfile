# 복지트윈 실시간 계산 API — Hugging Face Spaces(Docker). scripts/build_space.py 가 이 폴더 구성을 만든다.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    WT_WORKERS=2 \
    WT_SCENARIO_SAMPLE=10000
WORKDIR /home/user/app

COPY --chown=user pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=user . .
RUN uv sync --frozen --no-dev

EXPOSE 7860
CMD ["uvicorn", "welfaretwin.api.main:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers", "--forwarded-allow-ips", "*"]
