"""WebUI 回归测试独立入口。

用法（项目根目录）：
    python scripts/ui_check.py                 # 默认矩阵：360/1280 全场景 + 407/430 主页面
    python scripts/ui_check.py --full          # 4 档视口 × 全部场景
    python scripts/ui_check.py --update-baseline
    python scripts/ui_check.py --scenarios main-run,menu-logs --viewports 360x740

Playwright 需要独立安装，不要污染仓库 .venv；若当前解释器没有 Playwright，
可设置 TG_SIGNER_UI_PLAYWRIGHT_PYTHON 指向另一个已安装 Playwright 的解释器。
被测 WebUI 子进程默认使用仓库 .venv，可用 --server-python 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        alt = os.environ.get("TG_SIGNER_UI_PLAYWRIGHT_PYTHON")
        if alt and not os.environ.get("TG_SIGNER_UI_REEXEC"):
            env = os.environ.copy()
            env["TG_SIGNER_UI_REEXEC"] = "1"
            result = subprocess.run(
                [alt, str(Path(__file__).resolve()), *sys.argv[1:]],
                cwd=str(REPO_ROOT),
                env=env,
            )
            sys.exit(result.returncode)
        sys.exit(
            "当前解释器没有安装 Playwright。请选择其一：\n"
            "  1) 在仓库外的临时环境安装：\n"
            "     python -m venv %TEMP%\\tgs-ui-pw\n"
            "     %TEMP%\\tgs-ui-pw\\Scripts\\python -m pip install playwright\n"
            "     %TEMP%\\tgs-ui-pw\\Scripts\\python -m playwright install chromium\n"
            "  2) 设置 TG_SIGNER_UI_PLAYWRIGHT_PYTHON 指向已安装 Playwright 的 python\n"
            "  3) 紧急情况下跳过：发布脚本加 --skip-ui-check\n"
        )


def _parse_keys(value: Optional[str]) -> Optional[tuple]:
    if not value:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _print_summary(results, meta) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    print("\n" + "=" * 72)
    print(f"结果：{passed}/{total} 通过，{failed} 失败")
    print(f"耗时：{meta['duration_seconds']}s")
    print(f"截图目录：{meta['screenshots_dir']}")
    print(f"截图数量：{sum(1 for r in results if r.screenshot)}")
    print("=" * 72)

    grouped: dict[str, list] = {}
    for result in results:
        grouped.setdefault(result.viewport, []).append(result)
    for viewport, items in grouped.items():
        print(f"\n[{viewport}]")
        for item in items:
            mark = "PASS" if item.ok else "FAIL"
            print(
                f"  {mark:<4} {item.scenario:<20} {item.duration:>5.1f}s  {item.title}"
            )

    if failed:
        print("\n" + "-" * 72)
        print("失败明细")
        print("-" * 72)
        for result in results:
            if result.ok:
                continue
            print(f"\n* {result.viewport} / {result.scenario} ({result.title})")
            for failure in result.failures:
                print(f"  - [{failure.assertion}] {failure.detail}")
            print(f"  截图: {result.screenshot}")
        print("\n提示：失败场景同时保存了 .failure.html，可直接在浏览器打开查看 DOM。")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="tg-signer WebUI 回归测试")
    parser.add_argument(
        "--full",
        action="store_true",
        help="4 档视口跑全部场景（默认 407/430 只跑主页面）",
    )
    parser.add_argument("--viewports", help="逗号分隔的视口 key，如 360x740,1280x800")
    parser.add_argument("--scenarios", help="逗号分隔的场景 key，如 main-run,menu-logs")
    parser.add_argument(
        "--server-python",
        default=os.environ.get("TG_SIGNER_UI_SERVER_PYTHON"),
        help="启动被测 WebUI 的 Python 解释器（默认仓库 .venv）",
    )
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动分配）")
    parser.add_argument(
        "--artifacts",
        default=str(REPO_ROOT / "artifacts" / "ui-baseline"),
        help="截图与结果输出目录",
    )
    parser.add_argument(
        "--baseline",
        default=str(REPO_ROOT / "tests" / "e2e" / "ui_baseline.json"),
        help="DOM/HTML 基线文件",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="忽略旧基线并重新写入（UI 结构有意变更后使用）",
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--timeout", type=float, default=45.0, help="单场景超时秒数")
    parser.add_argument("--verbose", action="store_true", help="输出 console 明细")
    parser.add_argument("--keep-run-dir", action="store_true", help="保留临时数据目录")
    args = parser.parse_args(argv)

    _ensure_playwright()

    from tests.e2e.runner import run_matrix
    from tests.e2e.scenarios import select_targets
    from tests.e2e.server import UiServer

    targets = select_targets(
        full=args.full,
        viewport_keys=_parse_keys(args.viewports),
        scenario_keys=_parse_keys(args.scenarios),
    )
    if not targets:
        print("没有匹配的场景/视口，请检查 --viewports / --scenarios")
        return 2

    artifacts = Path(args.artifacts)
    baseline = Path(args.baseline)
    print(f"准备执行 {len(targets)} 个（视口 × 场景）组合；基线文件：{baseline}")

    with UiServer(
        server_python=args.server_python,
        port=args.port or None,
        keep_run_dir=args.keep_run_dir,
    ) as server:
        print(f"WebUI 已启动：{server.base_url}（数据目录 {server.run_dir}）")

        def progress(index: int, total: int, title: str, ok: bool) -> None:
            mark = "PASS" if ok else "FAIL"
            print(f"[{index}/{total}] {mark} {title}", flush=True)

        results, meta = run_matrix(
            targets,
            server.base_url,
            artifacts,
            baseline,
            update_baseline=args.update_baseline,
            headed=args.headed,
            per_scenario_timeout=args.timeout,
            verbose=args.verbose,
            progress=progress,
        )

    payload = {
        "meta": meta,
        "results": [r.to_dict() for r in results],
    }
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "latest-results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (Path(meta["screenshots_dir"]) / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _print_summary(results, meta)
    print(f"\n结果 JSON：{artifacts / 'latest-results.json'}")
    if meta["baseline_created"]:
        print(f"首次运行：已生成 DOM/HTML 基线 {baseline}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
