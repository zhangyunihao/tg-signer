"""组合任务子进程入口：按配置顺序逐个执行子任务。

用法（由 WebUI 的组合任务启动，每账号一个进程）:

    python -m tg_signer.combo_runner '<json spec>'

spec 格式::

    {"subs": [{"label": "签到 (run-once) · 任务A", "args": ["-w", ..., "run-once", "任务A"]},
              {"label": "自动抽奖 (giveaway run) · 1", "module": "tg_signer.giveaway_runner",
               "spec": {"account": "...", ...}}, ...]}

带 ``args`` 的子任务通过 CLI 入口执行；带 ``module`` 的子任务以
``python -m <module> '<json spec>'`` 方式执行（自动抽奖/批量退频道等
无 CLI 的任务类型）。子任务按列表顺序串行执行；任一子任务退出码非 0
时整体退出码为 1。stdout/stderr 直接透传给父进程，由 WebUI 解析
「>>> 子任务开始/结束」标记行生成明细进度。
"""

import json
import os
import subprocess
import sys


def _run_with_timeout(cmd: list[str], timeout: int | None) -> int:
    """运行子进程并等待退出；timeout（秒）为空/<=0 时不限时长。

    超时强杀整个进程树（子任务进程及其子进程），返回 124 作为超时退出码。
    """
    proc = subprocess.Popen(cmd)
    try:
        if timeout and timeout > 0:
            return proc.wait(timeout=timeout)
        return proc.wait()
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
        except OSError:
            proc.kill()
        proc.wait()
        return 124


def main() -> None:
    if len(sys.argv) < 2:
        print("缺少组合任务参数", flush=True)
        raise SystemExit(2)
    try:
        spec = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"组合任务参数解析失败: {exc}", flush=True)
        raise SystemExit(2)
    subs = spec.get("subs") or []
    total = len(subs)
    codes: list[int] = []
    for i, sub in enumerate(subs, 1):
        label = str(sub.get("label") or " ".join(sub.get("args") or []))
        print(f">>> 子任务开始 [{i}/{total}]: {label}", flush=True)
        if sub.get("module"):
            cmd = [
                sys.executable,
                "-m",
                str(sub["module"]),
                json.dumps(sub.get("spec") or {}, ensure_ascii=False),
            ]
        else:
            cmd = [sys.executable, "-m", "tg_signer.task_runner"] + list(
                sub.get("args") or []
            )
        # 子任务可选超时（秒）：仅对配置了 timeout 的子任务生效（如自动抽奖），
        # 超时强杀并记为失败（退出码 124），后续子任务继续执行
        raw_timeout = sub.get("timeout")
        try:
            timeout = int(raw_timeout) if raw_timeout else None
        except (TypeError, ValueError):
            timeout = None
        if timeout and timeout > 0:
            print(f"    子任务超时限制：{timeout} 秒", flush=True)
        code = _run_with_timeout(cmd, timeout)
        codes.append(code)
        if code == 124:
            print(f">>> 子任务超时 [{i}/{total}] 已达 {timeout} 秒强制退出", flush=True)
        # 多账号错峰延迟只对首个子任务生效，避免后续串行子任务重复等待
        if i == 1:
            os.environ.pop("TG_SIGNER_START_DELAY", None)
        print(f">>> 子任务结束 [{i}/{total}] 退出码 {code}", flush=True)
    raise SystemExit(0 if all(c == 0 for c in codes) else 1)


if __name__ == "__main__":
    main()
