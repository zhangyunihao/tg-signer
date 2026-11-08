"""测量 WebUI 事件循环延迟与同步 IO 阻塞热点。

用法（项目根目录）：
    python scripts/perf_loop_lag.py                    # 20 秒基线
    python scripts/perf_loop_lag.py --seconds 40       # 延长采样
    python scripts/perf_loop_lag.py --headed           # 显示浏览器

Playwright 需要独立安装，规则与 scripts/ui_check.py 一致：
若当前解释器没有 Playwright，用 TG_SIGNER_UI_PLAYWRIGHT_PYTHON 指向
另一个已安装 Playwright 的解释器。被测 WebUI 子进程默认使用仓库 .venv。

输出：
- 事件循环延迟分位（p50/p95/p99/max）
- 同步 IO 统计（调用点、次数、最大耗时）
- 同步 IO 告警条数与最慢的前 10 个调用点
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WARN_RE = re.compile(r"事件循环同步 IO 阻塞 ([\d.]+)ms：(.+?) @ (.+)$")


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
            "当前解释器没有安装 Playwright。请设置 TG_SIGNER_UI_PLAYWRIGHT_PYTHON，"
            "或参考 scripts/ui_check.py 的临时环境安装说明。"
        )


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _read_lag(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    samples: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            samples.append(
                {"t": float(payload["t"]), "lag_ms": float(payload["lag_ms"])}
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return samples


def _read_stats(path: Path) -> dict[str, dict[str, float]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    stats = payload.get("stats")
    return stats if isinstance(stats, dict) else {}


def _read_warnings(paths: tuple[Path, ...]) -> list[tuple[float, str, str]]:
    found: list[tuple[float, str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = WARN_RE.search(line)
            if match:
                found.append((float(match.group(1)), match.group(2), match.group(3)))
    return found


ALERT_FILES = (
    "webui_join_alerts.json",
    "webui_speak_alerts.json",
    "webui_speech_alerts.json",
    "webui_speech_insufficient_alerts.json",
    "webui_win_alerts.json",
    "webui_channels_full_alerts.json",
    "webui_scan_cap_alerts.json",
    "webui_checklist_unactionable_alerts.json",
)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_session_file(path: Path, user_id: int) -> None:
    """造一个可被账号身份读取的 session 文件（仅含 sessions 表）。"""
    if path.exists():
        return
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE sessions (user_id INTEGER)")
        conn.execute("INSERT INTO sessions (user_id) VALUES (?)", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _seed_perf_dataset(run_dir: Path, accounts: int, history: int, alerts: int) -> None:
    """在 e2e 基础数据集之上叠加更接近真实使用的数据量。

    覆盖 P0 未触发的重负载：账号数拉满（含 session 文件）、运行中的组合任务
    快照、8 类提醒文件、全量任务历史与账号状态，让 P2 有真实的前后对比对象。
    """

    workdir = run_dir / ".signer"
    users_dir = workdir / "users"
    session_names = []
    channel_stats = {}
    for index in range(accounts):
        name = f"perf_user_{index:03d}"
        session_names.append(name)
        user_dir = users_dir / name
        user_dir.mkdir(parents=True, exist_ok=True)
        me = {
            "id": 100000 + index,
            "first_name": f"Perf {index:03d}",
            "username": f"perf_{index:03d}",
        }
        chats = [
            {
                "chat_id": f"@perf_{index:03d}_{slot}",
                "name": f"Perf Chat {slot}",
                "type": "channel" if slot % 2 else "group",
                "unread": slot,
            }
            for slot in range(5)
        ]
        (user_dir / "me.json").write_text(
            json.dumps(me, ensure_ascii=False), encoding="utf-8"
        )
        (user_dir / "latest_chats.json").write_text(
            json.dumps(chats, ensure_ascii=False), encoding="utf-8"
        )
        _write_session_file(run_dir / f"{name}.session", 100000 + index)
        channel_stats[name] = {
            "joined": 100 + index % 300,
            "limit": 500,
            "groups": index % 30,
            "channels": index % 50,
        }

    _write_json(
        workdir / "channel_stats.json",
        {"updated_at": "2026-01-01 12:00:00", "accounts": channel_stats},
    )

    records = []
    for index in range(history):
        records.append(
            {
                "id": f"perf-{index:03d}",
                "task": f"perf-task-{index:03d}",
                "task_type": "组合任务",
                "task_name": f"perf-task-{index:03d}",
                "status": "成功" if index % 3 else "失败",
                "accounts": ",".join(
                    f"perf_user_{(index + offset) % max(1, accounts):03d}"
                    for offset in range(10)
                ),
                "start_time": f"2026-01-{(index % 28) + 1:02d} 12:00:00",
                "duration": "12秒",
                "result": "完成" if index % 3 else "部分失败",
                "details": [
                    {
                        "account": f"perf_user_{(index + offset) % max(1, accounts):03d}",
                        "chat": f"@perf_{offset}",
                        "status": "成功" if offset % 4 else "失败",
                        "note": "备注" * 8,
                    }
                    for offset in range(10)
                ],
            }
        )
    _write_json(workdir / "webui_task_history.json", records)

    # 运行中的组合任务：进度快照 + 组合模板（UI 轮询会反复读取这些文件）
    composite_tasks = [
        {
            "name": f"perf-combo-{index}",
            "subs": [
                {"type": "签到 (run-once)", "task": f"perf-task-{index}"},
                {"type": "批量退频道 (auto leave)", "task": "demo"},
            ],
        }
        for index in range(5)
    ]
    _write_json(workdir / "webui_composite_tasks.json", composite_tasks)

    now = time.time()
    combo_progress = {}
    for index in range(30):
        task_name = f"perf-combo-{index}"
        run_id = f"combo-run-{index:03d}"
        status = "运行中" if index == 0 else "成功"
        combo_progress[run_id] = {
            "id": run_id,
            "record": {
                "id": run_id,
                "task_type": "组合任务",
                "task_name": task_name,
                "accounts": ",".join(
                    f"perf_user_{(index + offset) % max(1, accounts):03d}"
                    for offset in range(10)
                ),
                "status": status,
                "start_time": f"2026-01-{(index % 28) + 1:02d} 12:00:00",
                "duration": "" if status == "运行中" else "30秒",
                "result": "" if status == "运行中" else "完成",
            },
            "details": [
                {
                    "account": f"perf_user_{(index + offset) % max(1, accounts):03d}",
                    "chat": f"@perf_{offset}",
                    "status": "进行中" if status == "运行中" else "成功",
                    "note": "进度备注" * 6,
                    "sub_type": "签到 (run-once)",
                }
                for offset in range(30)
            ],
            "combo_subs": [
                {"type": "签到 (run-once)", "task": f"perf-task-{index}"},
                {"type": "批量退频道 (auto leave)", "task": "demo"},
            ],
            "procs": [
                {
                    "account": f"perf_user_{offset:03d}",
                    "sub_labels": ["签到 (run-once) · perf-task"],
                    "stats": {"joined_total": offset},
                }
                for offset in range(5)
            ],
            "logs": [f"第 {line} 行组合任务日志" for line in range(200)],
            "start_ts": now,
            "updated_at": now - index,
        }
    _write_json(workdir / "webui_combo_progress.json", combo_progress)

    for filename in ALERT_FILES:
        _write_json(
            workdir / filename,
            {
                "alerts": [
                    {
                        "account": f"perf_user_{index % max(1, accounts):03d}",
                        "chat": f"@perf_{index}",
                        "error": "失败原因" * 10,
                        "time": "2026-01-01 12:00:00",
                    }
                    for index in range(alerts)
                ]
            },
        )

    _write_json(
        workdir / "webui_accounts.json",
        {
            "disabled": [f"perf_user_{index:03d}" for index in range(0, accounts, 20)],
            "two_way": [f"perf_user_{index:03d}" for index in range(1, accounts, 20)],
            "order": session_names,
        },
    )
    _write_json(
        workdir / "webui_task_accounts.json",
        {f"签到|perf-task-{index:03d}": session_names[:10] for index in range(history)},
    )
    _write_json(
        workdir / "webui_message_read.json",
        {"read": [f"win|{index}" for index in range(1000)]},
    )
    _write_json(
        workdir / "webui_message_pin.json",
        {"pinned": [f"win|{index}" for index in range(200)]},
    )
    _write_json(
        workdir / "webui_win_settings.json",
        {
            "win_forward_target": ["me"],
            "win_sweep_limit": 100,
            "giveaway_timeout_min": 10,
            "giveaway_scan_cap": 100,
        },
    )
    _write_json(
        workdir / "giveaway_tasks.json",
        {
            "seq": 20,
            "tasks": {
                str(index): {
                    "accounts": session_names[:5],
                    "chats": [f"@perf_{index}_{slot}" for slot in range(5)],
                    "last_start_time": "2026-01-01 12:00:00",
                    "last_duration": "20秒",
                    "last_outcome": "已完成",
                    "last_joined": index,
                }
                for index in range(20)
            },
        },
    )


def _scene_for(sample_time: float, marks: list[tuple[float, str]]) -> str:
    """按时间戳把 lag 样本归属到最近一次场景动作。"""
    label = "before"
    for stamp, name in marks:
        if stamp > sample_time:
            break
        label = name
    return label


def _group_by_scene(
    lag: list[dict], marks: list[tuple[float, str]]
) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for item in lag:
        groups.setdefault(_scene_for(item["t"], marks), []).append(item["lag_ms"])
    return groups


def _format_scene_stats(lag: list[dict], marks: list[tuple[float, str]]) -> list[str]:
    """每个场景单独给出分位数，用于定位冻结发生在哪次页面/标签切换。"""
    if not lag:
        return []
    rows = []
    for name, values in _group_by_scene(lag, marks).items():
        rows.append(
            f"  {name:<18} n={len(values):<5} "
            f"p50 {_percentile(values, 0.50):>7.1f}ms "
            f"p95 {_percentile(values, 0.95):>8.1f}ms "
            f"max {max(values):>8.1f}ms"
        )
    return rows


def _format_stats(stats: dict[str, dict[str, float]]) -> list[str]:
    rows = []
    for site, item in sorted(
        stats.items(), key=lambda kv: kv[1].get("max_ms", 0.0), reverse=True
    ):
        rows.append(
            f"  {item.get('max_ms', 0.0):>7.1f}ms  "
            f"x{int(item.get('count', 0)):<5} {site}"
        )
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="WebUI 事件循环延迟与同步 IO 测量")
    parser.add_argument("--seconds", type=float, default=20.0, help="采样秒数")
    parser.add_argument(
        "--server-python",
        default=os.environ.get("TG_SIGNER_UI_SERVER_PYTHON"),
        help="启动 WebUI 的解释器（默认仓库 .venv）",
    )
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动分配）")
    parser.add_argument(
        "--artifacts",
        default=str(REPO_ROOT / "artifacts" / "perf"),
        help="结果输出目录",
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--keep-run-dir", action="store_true", help="保留临时数据目录")
    parser.add_argument(
        "--accounts",
        type=int,
        default=200,
        help="合成数据集里的账号数量（同时生成 .session 文件）",
    )
    parser.add_argument(
        "--history", type=int, default=300, help="合成数据集里的任务历史条数"
    )
    parser.add_argument(
        "--alerts", type=int, default=200, help="每类提醒文件里的提醒条数"
    )
    args = parser.parse_args(argv)

    _ensure_playwright()

    from playwright.sync_api import sync_playwright

    from tests.e2e.runner import CLICK_TAB_JS
    from tests.e2e.server import UiServer, seed_dataset

    run_dir = Path(tempfile.mkdtemp(prefix="tg-signer-perf-"))
    lag_path = run_dir / "loop-lag.jsonl"
    stats_path = run_dir / "io-stats.json"
    extra_env = {
        "TG_SIGNER_WEBUI_IO_GUARD": "1",
        "TG_SIGNER_WEBUI_IO_LOG_MS": "10",
        "TG_SIGNER_WEBUI_IO_STATS_PATH": str(stats_path),
        "TG_SIGNER_WEBUI_LAG_LOG": str(lag_path),
    }

    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    # 数据集必须在子进程启动前落盘：Store 在 ui.run 之前一次性导入旧文件，
    # 启动后再写文件不会被导入，前后对比会失真
    seed_dataset(run_dir)
    _seed_perf_dataset(run_dir, args.accounts, args.history, args.alerts)

    with UiServer(
        server_python=args.server_python,
        port=args.port or None,
        run_dir=run_dir,
        keep_run_dir=args.keep_run_dir,
        extra_env=extra_env,
        seed=False,
    ) as server:
        print(f"WebUI 已启动：{server.base_url}（数据目录 {server.run_dir}）")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            scene_marks: list[tuple[float, str]] = []
            scene_marks.append((time.time(), "load:/"))
            page.goto(server.base_url, wait_until="networkidle")
            page.wait_for_timeout(1200)

            # 轮换主页面 5 个 tab + 消息中心 + 组合任务进度页，
            # 让提醒文件、组合进度快照的轮询路径也进入 sample 窗口
            scenes: list[tuple[str, int | str]] = [
                *[("tab", index) for index in range(5)],
                ("path", "/messages"),
                ("path", "/combo-progress"),
                ("path", "/"),
            ]
            deadline = time.monotonic() + max(3.0, args.seconds)
            step = 0
            while time.monotonic() < deadline:
                kind, value = scenes[step % len(scenes)]
                label = f"tab{value}" if kind == "tab" else f"path{value}"
                scene_marks.append((time.time(), label))
                if kind == "tab":
                    page.evaluate(CLICK_TAB_JS, value)
                else:
                    page.goto(
                        f"{server.base_url.rstrip('/')}{value}",
                        wait_until="networkidle",
                    )
                page.wait_for_timeout(2000)
                step += 1
            scene_marks.append((time.time(), "end"))

            browser.close()

        # 必须在 UiServer 退出（清理临时目录）之前读取采样文件
        lag = _read_lag(lag_path)
        stats = _read_stats(stats_path)
        warnings = _read_warnings(server.log_paths)

    lag_values = [item["lag_ms"] for item in lag]
    print("\n" + "=" * 72)
    if lag:
        print(f"事件循环延迟（{len(lag)} 个样本，20ms 采样间隔）")
        print(f"  p50 {_percentile(lag_values, 0.50):.1f}ms")
        print(f"  p95 {_percentile(lag_values, 0.95):.1f}ms")
        print(f"  p99 {_percentile(lag_values, 0.99):.1f}ms")
        print(f"  max {max(lag_values):.1f}ms")
        print("\n按场景拆分")
        print("\n".join(_format_scene_stats(lag, scene_marks) or ["  （无）"]))
        print("\n最长的 5 个样本（绝对时间用于与诊断日志对齐）")
        base = scene_marks[0][0] if scene_marks else 0.0
        for item in sorted(lag, key=lambda x: x["lag_ms"], reverse=True)[:5]:
            scene = _scene_for(item["t"], scene_marks)
            print(
                f"  {item['lag_ms']:>8.1f}ms  t+{item['t'] - base:6.2f}s  "
                f"abs={item['t']:.3f}  {scene}"
            )
    else:
        print("事件循环延迟：无样本（检查 TG_SIGNER_WEBUI_LAG_LOG 是否生效）")

    print(f"\n同步 IO 告警（>{10}ms）：{len(warnings)} 条")
    for elapsed, operation, site in sorted(warnings, reverse=True)[:10]:
        print(f"  {elapsed:>7.1f}ms  {operation} @ {site}")

    print("\n同步 IO 调用点（按最大耗时排序，前 15）")
    rows = _format_stats(stats)
    print("\n".join(rows[:15]) if rows else "  （无统计）")
    print("=" * 72)

    payload = {
        "seconds": args.seconds,
        "lag_samples": len(lag),
        "lag_ms": {
            "p50": _percentile(lag_values, 0.50),
            "p95": _percentile(lag_values, 0.95),
            "p99": _percentile(lag_values, 0.99),
            "max": max(lag_values) if lag_values else 0.0,
        },
        "scene_lag": {
            name: {
                "samples": len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "max": max(values),
            }
            for name, values in _group_by_scene(lag, scene_marks).items()
        },
        "warnings": [
            {"elapsed_ms": elapsed, "operation": operation, "site": site}
            for elapsed, operation, site in warnings
        ],
        "stats": stats,
    }
    (artifacts / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n结果 JSON：{artifacts / 'latest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
