"""실시간 계산 API 를 Hugging Face Space(Docker)로 내보내기.

    uv run python scripts/build_space.py                                   # dist/space/ 만 만들기
    uv run --with huggingface_hub python scripts/build_space.py --deploy                 # <로그인 계정>/welfaretwin-api

허용 목록(INCLUDE)에 있는 파일만 복사한다 — .env·원문 수집본·캐시는 절대 들어가지 않는다.
합성 인구 parquet·수급 통계 CSV 는 git 에 없으므로(재생성 가능) 여기서 함께 올린다. 둘 다 개인정보 없음.
배포 전: https://huggingface.co/settings/tokens 에서 쓰기(Write) 토큰을 만들어 .env 에 HF_TOKEN=... 로 넣는다.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "space"
INCLUDE = [
    "pyproject.toml", "uv.lock", ".python-version",
    "src/welfaretwin",
    "rules_db/approved", "rules_db/params",
    "artifacts/sim/behavior.parquet",
    "data/processed/households.parquet", "data/processed/persons.parquet", "data/processed/population_meta.json",
    "data/processed/stats/nbls_recipients.csv", "data/processed/stats/nbls_households_by_type.csv",
]
FORBIDDEN = (".env", "sk-ant-", "VERCEL_OIDC")


def build() -> Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for rel in INCLUDE:
        src = ROOT / rel
        if not src.exists():
            raise SystemExit(f"없음: {rel} — 합성 인구·통계를 먼저 만드세요(README '전체 재현')")
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    for name in ("Dockerfile", "README.md"):
        shutil.copy2(ROOT / "deploy" / "space" / name, OUT / name)
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    with (OUT / "Dockerfile").open("a", encoding="utf-8") as f:
        f.write(f"\nENV WT_VERSION={rev or 'dev'}\n")
    for f in OUT.rglob("*"):   # 마지막 안전장치: 비밀처럼 보이는 파일·문자열이 있으면 중단
        if f.is_file():
            if f.name.startswith(".env"):
                raise SystemExit(f"비밀 파일 포함: {f}")
            if f.suffix in {".py", ".yaml", ".toml", ".json", ".md", ".csv", ""} and \
                    any(t in f.read_text(encoding="utf-8", errors="ignore") for t in FORBIDDEN[1:]):
                raise SystemExit(f"비밀 문자열 의심: {f}")
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{OUT} — 파일 {sum(1 for f in OUT.rglob('*') if f.is_file())}개, {size / 1e6:.1f} MB")
    return OUT


def deploy(repo: str) -> None:
    import os

    from dotenv import load_dotenv
    from huggingface_hub import HfApi
    load_dotenv(ROOT / ".env")          # HF_TOKEN(쓰기 권한)은 .env 에만 — 채팅·커밋 금지
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    if repo == "auto":
        repo = f"{api.whoami()['name']}/welfaretwin-api"
    api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)
    api.upload_folder(folder_path=str(OUT), repo_id=repo, repo_type="space", delete_patterns=["*"],
                      commit_message="복지트윈 API 배포")
    owner, name = repo.split("/")
    print(f"https://huggingface.co/spaces/{repo}")
    print(f"API: https://{owner.lower()}-{name.lower().replace('_', '-').replace('.', '-')}.hf.space")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", metavar="OWNER/SPACE", nargs="?", const="auto",
                    help="값 없이 쓰면 <로그인 계정>/welfaretwin-api")
    args = ap.parse_args()
    build()
    if args.deploy:
        deploy(args.deploy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
