"""실시간 계산 API 배포 묶음 만들기·올리기.

    uv run python scripts/deploy_api.py                    # dist/welfaretwin-api/ (Vercel Python 함수) 만들기
    uv run python scripts/deploy_api.py --deploy           # + Vercel 프로덕션 배포(npx vercel, CLI 로그인 필요) → /health 확인
    uv run python scripts/deploy_api.py --target space     # dist/space/ (Hugging Face Docker Space)
    uv run --with huggingface_hub python scripts/deploy_api.py --target space --deploy   # .env 의 HF_TOKEN

기본은 Vercel: 정적 웹(welfaretwin)과 별도 프로젝트 welfaretwin-api 로 올려, 서버가 잘못돼도 웹은 그대로다.
Hugging Face 는 2026-11 기준 Docker Space 를 무료 CPU 에 올리려면 PRO 구독이 필요하다(402).
허용 목록(DATA)에 있는 파일만 복사한다 — .env·원문 수집본·캐시는 절대 들어가지 않는다.
합성 인구 parquet·수급 통계 CSV 는 git 에 없으므로(재생성 가능) 여기서 함께 올린다. 둘 다 개인정보 없음.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = [
    "src/welfaretwin",
    "rules_db/approved", "rules_db/params",
    "artifacts/sim/behavior.parquet",
    "data/processed/households.parquet", "data/processed/persons.parquet", "data/processed/population_meta.json",
    "data/processed/stats/nbls_recipients.csv", "data/processed/stats/nbls_households_by_type.csv",
]
# API 가 실제로 import 하는 외부 패키지만(LLM·대시보드 패키지 제외) — 로컬 uv.lock 과 같은 버전으로 고정
VERCEL_REQS = ["fastapi", "pydantic", "pandas", "numpy", "pyarrow", "PyYAML", "z3-solver"]
VERCEL_PROJECT = "welfaretwin-api"
FORBIDDEN = ("sk-ant-", "VERCEL_OIDC", "hf_")


def _rev() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() or "dev"


def _clear(out: Path) -> None:
    """폴더 자체와 Vercel 연결 정보(.vercel)는 두고 내용만 비움(Windows 에서 열린 폴더는 지울 수 없음)."""
    out.mkdir(parents=True, exist_ok=True)
    for p in out.iterdir():
        if p.name == ".vercel":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()


def _copy(out: Path, items: list[str]) -> None:
    for rel in items:
        src = ROOT / rel
        if not src.exists():
            raise SystemExit(f"없음: {rel} — 합성 인구·통계를 먼저 만드세요(README '전체 재현')")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)


def _write(path: Path, text: str) -> None:
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def _scan(out: Path) -> None:
    """마지막 안전장치: 비밀처럼 보이는 파일·문자열이 있으면 중단."""
    for f in out.rglob("*"):
        if not f.is_file() or ".vercel" in f.parts:
            continue
        if f.name.startswith(".env"):
            raise SystemExit(f"비밀 파일 포함: {f}")
        if f.suffix in {".py", ".yaml", ".toml", ".json", ".md", ".csv", ".txt", ""}:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if any(t in text for t in FORBIDDEN[:2]) or re.search(r"\bhf_[A-Za-z0-9]{20,}", text):
                raise SystemExit(f"비밀 문자열 의심: {f}")
    files = [f for f in out.rglob("*") if f.is_file() and ".vercel" not in f.parts]
    print(f"{out} — 파일 {len(files)}개, {sum(f.stat().st_size for f in files) / 1e6:.1f} MB")


# ------------------------------------------------------------------ Vercel

def build_vercel() -> Path:
    out = ROOT / "dist" / VERCEL_PROJECT
    _clear(out)
    _copy(out, DATA)
    _write(out / "app.py", f'''"""Vercel 진입점 — scripts/deploy_api.py 가 만든다(직접 고치지 말 것)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
os.environ.setdefault("WT_VERSION", "{_rev()}")

from welfaretwin.api.main import app  # noqa: E402,F401
''')
    _write(out / "requirements.txt", "".join(f"{p}=={version(p)}\n" for p in VERCEL_REQS))
    _write(out / ".python-version", "3.12\n")
    _baseline(out)
    _write(out / "vercel.json", json.dumps({
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "regions": ["icn1"],                              # 서울 — 발표장 왕복 지연 최소
        "functions": {"app.py": {"maxDuration": 120}},    # 시나리오(1만 가구, 1 vCPU) 약 20~40초
    }, ensure_ascii=False, indent=2) + "\n")
    _scan(out)
    return out


def _baseline(out: Path) -> None:
    """시나리오 기준선(현행 규칙·S0, 서버와 같은 표본)의 가구 표를 미리 계산해 묶음에 넣는다.

    사용자 입력과 무관한 고정값이라 매 요청 계산할 이유가 없다 → 1 vCPU 서버의 첫 시나리오가 변경안 계산만 하면 된다.
    서버는 표본 가구가 같을 때만 쓴다(api.main._preload_baseline).
    """
    import pandas as pd

    from welfaretwin.api.main import SCENARIO_SAMPLE
    from welfaretwin.sim.scenario import PolicyChange, compare, table_key
    cache: dict = {}
    t0 = time.time()
    compare([(PolicyChange("현행"), "S0")], sample=SCENARIO_SAMPLE, workers=4, cache=cache)
    key = table_key(PolicyChange("현행"), SCENARIO_SAMPLE)
    dst = out / "artifacts" / "api" / "scenario_base.pkl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle({"key": key, "table": cache[key]}, dst)
    print(f"기준선 {SCENARIO_SAMPLE:,}가구 표 → {dst.relative_to(out)} ({time.time() - t0:.1f}s)")


def deploy_vercel(out: Path) -> str:
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit("npx 가 없습니다(Node.js 필요).")
    # 저장소 안(dist/)에서 올리면 CLI 가 git 커밋 정보를 붙이고, Hobby 요금제는 커밋 작성자 검사로 배포를 막는다
    # ("commit author doesn't have permission") → 저장소 밖 임시 폴더에 복사(연결 정보 .vercel 포함)해서 올린다.
    import tempfile
    stage = Path(tempfile.mkdtemp(prefix="wt-api-")) / VERCEL_PROJECT
    shutil.copytree(out, stage)
    print(f"vercel deploy --prod … ({stage})", flush=True)
    r = subprocess.run([npx, "--no-install", "vercel", "deploy", "--prod", "--yes"], cwd=stage,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if (stage / ".vercel").exists():          # 처음 연결했으면 연결 정보를 dist 로 되가져온다
        shutil.copytree(stage / ".vercel", out / ".vercel", dirs_exist_ok=True)
    out = stage
    log = (r.stdout or "") + (r.stderr or "")
    print("\n".join(l for l in log.splitlines() if l.strip())[-3000:])
    if r.returncode != 0:
        raise SystemExit("Vercel 배포 실패 — 위 로그 확인")
    # 처음 연결할 때 CLI 가 GitHub 저장소를 자동 연결한다 → 푸시마다 저장소 루트(정적 웹)를 이 프로젝트로 빌드하게 됨.
    # API 는 이 스크립트로만 배포하므로 연결을 끊는다(이미 끊겨 있으면 아무 일 없음).
    subprocess.run([npx, "--no-install", "vercel", "git", "disconnect", "--yes"], cwd=out,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    shutil.rmtree(stage.parent, ignore_errors=True)        # 임시 복사본 정리
    m = re.search(r"Aliased\s+(https://\S+\.vercel\.app)", log)
    url = m.group(1) if m else f"https://{VERCEL_PROJECT}.vercel.app"
    wait_health(url)
    return url


def wait_health(url: str, timeout: int = 180) -> None:
    import httpx
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"{url}/health", timeout=60)
            if r.status_code == 200:
                print(f"API: {url}  /health {r.json()}  ({time.time() - t0:.1f}s)")
                return
            print(f"  /health {r.status_code}", flush=True)
        except httpx.HTTPError as e:
            print(f"  /health {type(e).__name__}", flush=True)
        time.sleep(5)
    raise SystemExit(f"{timeout}초 안에 {url}/health 가 응답하지 않음")


# ------------------------------------------------------------------ Hugging Face Space

def build_space() -> Path:
    out = ROOT / "dist" / "space"
    _clear(out)
    _copy(out, ["pyproject.toml", "uv.lock", ".python-version", *DATA])
    for name in ("Dockerfile", "README.md"):   # Windows 체크아웃(CRLF)이어도 Space 에는 LF 로
        _write(out / name, (ROOT / "deploy" / "space" / name).read_text(encoding="utf-8"))
    with (out / "Dockerfile").open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"\nENV WT_VERSION={_rev()}\n")
    _scan(out)
    return out


def space_url(repo: str) -> str:
    sub = repo.lower().replace("/", "-").replace("_", "-").replace(".", "-")
    return f"https://{sub}.hf.space"


def deploy_space(out: Path, repo: str, wait: bool) -> str:
    import os

    from dotenv import load_dotenv
    from huggingface_hub import HfApi
    load_dotenv(ROOT / ".env")          # HF_TOKEN(쓰기 권한)은 .env 에만 — 채팅·커밋 금지
    api = HfApi(token=os.environ.get("HF_TOKEN") or None)   # 없으면 `hf auth login` 로그인 정보
    if repo == "auto":
        repo = f"{api.whoami()['name']}/welfaretwin-api"
    api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)
    api.upload_folder(folder_path=str(out), repo_id=repo, repo_type="space", delete_patterns=["*"],
                      commit_message="복지트윈 API 배포")
    url = space_url(repo)
    print(f"https://huggingface.co/spaces/{repo}\nAPI: {url}")
    if wait:
        wait_running(api, repo, url)
    return url


def wait_running(api, repo: str, url: str, timeout: int = 1200) -> None:
    """빌드가 끝나 RUNNING 이 되고 /health 가 답할 때까지 기다린다(실패 단계면 바로 중단)."""
    import httpx
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        st = api.get_space_runtime(repo).stage
        stage = str(getattr(st, "value", st))
        if stage != last:
            print(f"  {int(time.time() - t0):4d}s  {stage}", flush=True)
            last = stage
        if stage.endswith("ERROR"):
            raise SystemExit(f"Space 빌드/실행 실패({stage}) — https://huggingface.co/spaces/{repo} 의 Logs 확인")
        if stage.endswith("RUNNING"):
            try:
                r = httpx.get(f"{url}/health", timeout=30)
                if r.status_code == 200:
                    print(f"  /health {r.json()}")
                    return
            except httpx.HTTPError:
                pass
        time.sleep(10)
    raise SystemExit(f"{timeout}초 안에 RUNNING 이 안 됨 — Space 페이지에서 상태 확인")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["vercel", "space"], default="vercel")
    ap.add_argument("--deploy", metavar="OWNER/SPACE", nargs="?", const="auto",
                    help="배포까지. space 대상에서 값 없이 쓰면 <로그인 계정>/welfaretwin-api")
    ap.add_argument("--no-wait", action="store_true", help="space: 업로드만 하고 빌드 완료를 기다리지 않음")
    a = ap.parse_args()
    if a.target == "vercel":
        out = build_vercel()
        if a.deploy:
            deploy_vercel(out)
    else:
        out = build_space()
        if a.deploy:
            deploy_space(out, a.deploy, wait=not a.no_wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
