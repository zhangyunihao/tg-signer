"""浏览器级回归的 pytest 包装（默认跳过，CI/本地通过 scripts/ui_check.py 运行）。

在仓库外准备了 Playwright 的环境里，可这样跑单个冒烟场景：
    TG_SIGNER_RUN_UI_E2E=1 python -m pytest tests/e2e/test_ui_regression.py -q
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="需要单独安装 Playwright")

if os.environ.get("TG_SIGNER_RUN_UI_E2E") != "1":
    pytest.skip(
        "设置 TG_SIGNER_RUN_UI_E2E=1 才会启动真实 WebUI 做浏览器回归",
        allow_module_level=True,
    )

from tests.e2e.runner import run_matrix
from tests.e2e.scenarios import SCENARIO_BY_KEY, VIEWPORT_BY_KEY
from tests.e2e.server import UiServer


def test_homepage_smoke() -> None:
    targets = [
        (VIEWPORT_BY_KEY["360x740"], SCENARIO_BY_KEY["main-run"]),
        (VIEWPORT_BY_KEY["1280x800"], SCENARIO_BY_KEY["main-run"]),
    ]
    with tempfile.TemporaryDirectory(prefix="tg-signer-ui-pytest-") as tmp:
        tmp_path = Path(tmp)
        with UiServer(run_dir=tmp_path / "run") as server:
            results, _meta = run_matrix(
                targets,
                server.base_url,
                tmp_path / "artifacts",
                tmp_path / "baseline.json",
                per_scenario_timeout=60,
            )
    assert all(result.ok for result in results), [
        (r.viewport, r.failures) for r in results if not r.ok
    ]
