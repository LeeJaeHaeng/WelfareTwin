"""데모 리허설 자동 점검 — 문서 01 M6 '데모 시나리오 3회 연속 무오류 실행'.

대시보드가 떠 있는 상태에서(예: streamlit run app/dashboard.py --server.port 8765)
    python app/rehearsal.py --url http://localhost:8765 --rounds 3 --shots artifacts/rehearsal

화면 1~5 를 문서 10 의 발표 순서대로 열고 주요 버튼을 누른 뒤, Streamlit 예외가 없는지·핵심 문구가 보이는지·
각 화면이 30초 안에 뜨는지 확인한다. 결과는 rehearsal_report.json, 스크린샷은 --shots 폴더.
(playwright 필요: pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

STEPS = [
    (1, "법전 → 코드", None, ["A·B 판정 동치", "추출된 규칙"]),
    (2, "사각지대", "사각지대 찾기", ["사각지대 유형 #1", "탈락"]),
    (3, "급여 절벽", None, ["가처분소득", "Z3 가 찾은 가장 큰 절벽"]),
    (4, "레드팀", "레드팀 점검 실행", ["정책 사전 점검 리포트", "배제 위험"]),
    (5, "백테스트", None, ["오차율", "한계"]),
]


def wait_idle(page, timeout_s: float = 60) -> None:
    """Streamlit 실행 표시가 사라질 때까지."""
    t0 = time.time()
    page.wait_for_timeout(800)
    while time.time() - t0 < timeout_s:
        if page.locator('[data-testid="stStatusWidget"]').count() == 0:
            return
        page.wait_for_timeout(300)


def run_round(pw, url: str, shots: Path | None, k: int, video: Path | None = None) -> list[dict]:
    browser = pw.chromium.launch()
    ctx_opts = {"viewport": {"width": 1920, "height": 1080}}
    if video:   # 발표 백업 영상(문서 10 §2) — 화면 전환 사이에 잠시 머문다
        video.mkdir(parents=True, exist_ok=True)
        ctx_opts.update(record_video_dir=str(video), record_video_size={"width": 1920, "height": 1080})
    context = browser.new_context(**ctx_opts)
    page = context.new_page()
    out = []
    for num, name, button, expects in STEPS:
        t0 = time.time()
        rec = {"round": k, "page": num, "name": name, "ok": False}
        try:
            page.goto(f"{url}/?page={num}", wait_until="networkidle")
            wait_idle(page)
            if num == 4:
                page.get_by_role("tab", name="사업안 레드팀(30초)").click()
                wait_idle(page)
            if button:
                page.get_by_role("button", name=button).click()
                wait_idle(page)
            body = page.inner_text("body")
            errors = page.locator('[data-testid="stException"]').count()
            missing = [e for e in expects if e not in body]
            rec.update(ok=errors == 0 and not missing, exceptions=errors, missing=missing,
                       seconds=round(time.time() - t0, 1))
            rec["within_30s"] = rec["seconds"] <= 30
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f"r{k}_p{num}.png"), full_page=True)
            if video:
                page.mouse.wheel(0, 600)
                page.wait_for_timeout(2500)
                page.mouse.wheel(0, -600)
                page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001 — 리허설은 어떤 실패든 기록
            rec.update(error=str(e)[:300], seconds=round(time.time() - t0, 1))
        out.append(rec)
    context.close()
    if video:
        vid = page.video.path() if page.video else None
        if vid:
            target = video / "demo_backup.webm"
            if target.exists():
                target.unlink()
            Path(vid).rename(target)
    browser.close()
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--shots", type=Path)
    ap.add_argument("--video", type=Path, help="1회차를 녹화해 <폴더>/demo_backup.webm 으로 저장")
    ap.add_argument("--out", type=Path, default=Path("artifacts/rehearsal/rehearsal_report.json"))
    args = ap.parse_args()
    results = []
    with sync_playwright() as pw:
        for k in range(1, args.rounds + 1):
            r = run_round(pw, args.url, args.shots if k == 1 else None, k, args.video if k == 1 else None)
            results += r
            print(f"[{k}회차] " + " | ".join(f"{x['page']}:{'OK' if x['ok'] else 'FAIL'}({x['seconds']}s)" for x in r))
    all_ok = all(x["ok"] for x in results)
    consecutive = all_ok and len({x["round"] for x in results}) == args.rounds
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"all_ok": all_ok, "rounds": args.rounds, "consecutive_clean_rounds": consecutive,
                                    "max_seconds": max(x["seconds"] for x in results), "results": results},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{'통과' if all_ok else '실패'} — {args.rounds}회 연속 무오류={consecutive}, 최대 {max(x['seconds'] for x in results)}초 → {args.out}")
    for x in results:
        if not x["ok"]:
            print("  실패:", x)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
