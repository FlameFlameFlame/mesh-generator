from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest
import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path("/Users/timur/Documents/src/LoraMeshPlanner")
REPO = ROOT / "mesh-generator"
DEFAULT_OUTPUT_DIR = ROOT / "projects"
RUN_ID = time.strftime("%Y%m%d-%H%M%S")
PROJECT_NAME = f"playwright-smoke-{RUN_ID}"
PROJECT_DIR = DEFAULT_OUTPUT_DIR / PROJECT_NAME
ARTIFACT_DIR = REPO / "tests" / "e2e" / "artifacts" / RUN_ID
BASE_HOST = "127.0.0.1"
BASE_URL = f"http://{BASE_HOST}:5050"


def _reader(proc: subprocess.Popen[str], lines: List[str]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((BASE_HOST, 0))
        return int(s.getsockname()[1])


def _wait_for_server(proc: subprocess.Popen[str], timeout_s: float = 40.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mesh-generator server exited early with code {proc.returncode}")
        try:
            r = requests.get(f"{BASE_URL}/api/projects", timeout=1.5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("mesh-generator server did not become ready in time")


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def _wait_download_complete(page, timeout_ms: int = 240000) -> None:
    page.wait_for_function(
        """
        () => {
          const btn = document.getElementById('btn-download');
          if (!btn) return false;
          return btn.textContent.trim() === 'Download Data' && btn.disabled === false;
        }
        """,
        timeout=timeout_ms,
    )


def _wait_data_ready(page, timeout_ms: int = 240000) -> None:
    page.wait_for_function(
        """
        () => {
          const btn = document.getElementById('btn-download');
          if (!btn) return false;
          const idle = btn.textContent.trim() === 'Download Data' && btn.disabled === false;
          const elevReady = (typeof _hasElevation !== 'undefined') ? !!_hasElevation : false;
          const gridReady = (typeof _isGridProviderReady === 'function') ? !!_isGridProviderReady() : false;
          return idle && elevReady && gridReady;
        }
        """,
        timeout=timeout_ms,
    )


def _create_sites(page) -> None:
    page.evaluate(
        """
        async () => {
          await fetch('/api/sites', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'pw_a', lat: 40.175, lon: 44.505, priority: 1, site_height_m: 0.0, fetch_city: false})
          });
          await fetch('/api/sites', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'pw_b', lat: 40.181, lon: 44.511, priority: 1, site_height_m: 0.0, fetch_city: false})
          });
          const resp = await fetch('/api/sites');
          sites = await resp.json();
          refresh();
          _bboxBounds = [[40.170, 44.500], [40.186, 44.516]];
          const bboxStatus = document.getElementById('bbox-status');
          if (bboxStatus) bboxStatus.style.display = 'inline';
        }
        """
    )


def _run_download_cycle(page, retries: int = 3) -> None:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            page.evaluate(
                """
                async () => {
                  await doFetchRoads();
                  await doFetchElevation();
                }
                """
            )
            _wait_data_ready(page, timeout_ms=360000)
            return
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            page.wait_for_timeout(2000 * attempt)
    raise last_exc if last_exc is not None else RuntimeError("Download cycle failed")


def _run_ui_checks(page) -> None:
    page.goto(BASE_URL, wait_until="networkidle")
    page.evaluate("localStorage.removeItem('meshProjectStateV1')")
    page.request.post(f"{BASE_URL}/api/clear")
    create_resp = page.request.post(f"{BASE_URL}/api/projects/create", data={"name": PROJECT_NAME})
    _assert(create_resp.status in (200, 409), f"Project create failed: {create_resp.status}")
    page.request.post(f"{BASE_URL}/api/projects/open", data={"project_name": PROJECT_NAME})
    page.goto(BASE_URL, wait_until="networkidle")
    page.evaluate(
        """
        (projectName) => {
          _setCurrentProject(projectName);
          const sel = document.getElementById('project-select');
          if (sel) {
            let opt = Array.from(sel.options).find(o => o.value === projectName);
            if (!opt) {
              opt = document.createElement('option');
              opt.value = projectName;
              opt.textContent = projectName;
              sel.appendChild(opt);
            }
            sel.value = projectName;
          }
        }
        """,
        PROJECT_NAME,
    )

    page.screenshot(path=str(ARTIFACT_DIR / "01-default-output-dir.png"), full_page=True)

    _create_sites(page)
    page.wait_for_timeout(800)

    _run_download_cycle(page)
    page.screenshot(path=str(ARTIFACT_DIR / "02-after-download-data.png"), full_page=True)

    page.click("#toolbar button.primary:has-text('Save')")
    config_path = PROJECT_DIR / "config.yaml"
    status_path = PROJECT_DIR / "status.json"
    grid_bundle = PROJECT_DIR / "grid_bundle.json"

    deadline = time.time() + 20
    while time.time() < deadline and (not config_path.exists() or not status_path.exists()):
        time.sleep(0.2)

    _assert(config_path.exists(), f"Missing saved config: {config_path}")
    _assert(status_path.exists(), f"Missing saved status: {status_path}")

    cache_dir = PROJECT_DIR / "cache"
    roads_cached = list(cache_dir.glob("roads_*.json"))
    elev_cached = list((cache_dir / "srtm").glob("*.npy"))
    _assert(cache_dir.is_dir(), f"Cache directory missing: {cache_dir}")
    _assert(bool(roads_cached), "Expected roads cache files")
    _assert(bool(elev_cached), "Expected elevation tile cache files")
    _assert(grid_bundle.exists(), f"Missing grid bundle: {grid_bundle}")

    page.evaluate("""(cfgPath) => _loadProjectFromPath(cfgPath)""", str(config_path))
    page.wait_for_function(
        """
        () => {
          const rows = document.querySelectorAll('#site-tbody tr');
          return rows.length >= 2;
        }
        """,
        timeout=45000,
    )

    coverage_resp = page.request.post(
        f"{BASE_URL}/api/tower-coverage/calculate",
        data={
            "source": {"source_id": "pw_src", "lat": 40.178, "lon": 44.508},
            "project_name": PROJECT_NAME,
        },
    )
    _assert(coverage_resp.status == 200, f"Tower coverage API failed: {coverage_resp.status}")
    coverage_json = coverage_resp.json()
    feature_count = int(coverage_json.get("feature_count") or 0)
    _assert(feature_count > 0, "Expected runtime tower coverage features")
    page.screenshot(path=str(ARTIFACT_DIR / "03-coverage-progress-and-result.png"), full_page=True)

    _run_download_cycle(page)


def run() -> None:
    global BASE_URL
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if PROJECT_DIR.exists():
        shutil.rmtree(PROJECT_DIR)
    port = int(os.environ.get("E2E_SMOKE_PORT", "0") or "0")
    if port <= 0:
        port = _pick_free_port()
    BASE_URL = f"http://{BASE_HOST}:{port}"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        "poetry",
        "run",
        "flask",
        "--app",
        "generator.app:create_app",
        "run",
        "--host",
        BASE_HOST,
        "--port",
        str(port),
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    logs: List[str] = []
    reader = threading.Thread(target=_reader, args=(proc, logs), daemon=True)
    reader.start()

    trace_path = ARTIFACT_DIR / "playwright-trace.zip"

    browser = None
    context = None
    try:
        _wait_for_server(proc)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on("dialog", lambda d: d.accept())

            _run_ui_checks(page)

            context.tracing.stop()
            context.close()
            context = None
            browser.close()
            browser = None

        print("Playwright smoke validation passed")
        print(f"Artifacts: {ARTIFACT_DIR}")
        print(f"Project output: {PROJECT_DIR}")

    except (AssertionError, PlaywrightTimeoutError, Exception) as exc:
        try:
            if context is not None:
                context.tracing.stop(path=str(trace_path))
        except Exception:
            pass
        try:
            if context is not None:
                context.close()
                context = None
            if browser is not None:
                browser.close()
                browser = None
        except Exception:
            pass
        finally:
            if logs:
                (ARTIFACT_DIR / "server.log").write_text("\n".join(logs), encoding="utf-8")
        raise exc
    finally:
        if logs:
            (ARTIFACT_DIR / "server.log").write_text("\n".join(logs), encoding="utf-8")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.e2e
def test_playwright_smoke() -> None:
    """Optional browser smoke test; enabled via RUN_E2E=1."""
    if os.environ.get("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to run Playwright smoke test")
    run()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"Playwright smoke validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
