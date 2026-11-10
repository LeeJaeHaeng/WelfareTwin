"""정적 웹(Vercel) — 배포 전에 스크립트 문법·데이터 파일을 확인한다(배포 후 빈 화면 방지)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"


def test_inline_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert scripts, "인라인 스크립트가 없음"
    js = tmp_path / "page.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_data_files_have_required_keys():
    b = json.loads((WEB / "data" / "bundle.json").read_text(encoding="utf-8"))
    for key in ("rules", "blindspot", "blindspot_scale", "cliff_z3", "scenario", "redteam", "backtest", "nbls_series"):
        assert b.get(key), key
    s = json.loads((WEB / "data" / "sweeps.json").read_text(encoding="utf-8"))
    assert s and all(len(v["earned"]) == len(v["own_income"]) for v in s.values())


def test_no_secrets_in_published_files():
    pat = re.compile(r"sk-ant-|VERCEL_OIDC|apiKey|serviceKey=")
    for f in WEB.rglob("*"):
        if f.is_file() and not f.name.startswith(".env") and ".vercel" not in f.parts:
            assert not pat.search(f.read_text(encoding="utf-8", errors="ignore")), f
