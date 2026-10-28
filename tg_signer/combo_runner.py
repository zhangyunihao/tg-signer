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

spec 可带可选的 ``start_at``（``YYYY-MM-DD HH:MM:SS``）：进程先等待到该
时间，再探测 TG 网络；不通则按 ``network_retry_seconds``（默认 1800 秒）
间隔重试，最多 ``network_max_retries`` 次（默认 3 次），仍不通则退出码 3
（视为本次组合任务失败）。
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

# 开始时间到达后若 TG 网络不通：默认每 30 分钟重试一次，最多重试 3 次；
# 可通过 spec 的 network_retry_seconds / network_max_retries 覆盖
NETWORK_RETRY_INTERVAL = 30 * 60
NETWORK_MAX_RETRIES = 3
NETWORK_PROBE_TIMEOUT = 8.0
# 等待开始时间期间输出剩余时间的间隔（秒）
_WAIT_LOG_INTERVAL = 600

_START_AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse_start_at(value) -> datetime | None:
    """解析可选的计划开始时间（完整日期时间）；空值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _START_AT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_retry_settings(spec: dict) -> tuple[int, int]:
    """读取网络重试间隔（秒）与次数；缺失或非法时用默认值，负值归零。"""
    try:
        interval = int(spec.get("network_retry_seconds"))
    except (TypeError, ValueError):
        interval = NETWORK_RETRY_INTERVAL
    try:
        retries = int(spec.get("network_max_retries"))
    except (TypeError, ValueError):
        retries = NETWORK_MAX_RETRIES
    return max(1, interval), max(0, retries)


def _fmt_remaining(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟"
    return f"{secs}秒"


def _wait_until_start(start_at: datetime) -> None:
    """阻塞等待到计划开始时间，期间每 10 分钟输出一次剩余时间。"""
    print(f">>> 等待开始时间 {start_at:%Y-%m-%d %H:%M:%S}", flush=True)
    last_log = time.monotonic()
    while True:
        remaining = (start_at - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        if time.monotonic() - last_log >= _WAIT_LOG_INTERVAL:
            print(f">>> 距开始时间还有 {_fmt_remaining(remaining)}", flush=True)
            last_log = time.monotonic()
        time.sleep(min(remaining, 60))


def _check_telegram_network() -> tuple[bool, str]:
    """通过当前代理配置探测 Telegram DC 是否可连接。"""
    from tg_signer.netcheck import check_telegram_connectivity

    return check_telegram_connectivity(NETWORK_PROBE_TIMEOUT)


def _fmt_interval(seconds: int) -> str:
    """把重试间隔格式化为「N 分钟」或「N 秒」。"""
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _wait_for_network(interval: int, max_retries: int) -> bool:
    """开始时间到达后确认 TG 网络可用；不通则按 interval 秒重试，最多 max_retries 次。"""
    ok, detail = _check_telegram_network()
    retry = 0
    while not ok and retry < max_retries:
        retry += 1
        print(
            f">>> TG 网络不通（{detail}），{_fmt_interval(interval)}后"
            f"第 {retry}/{max_retries} 次重试",
            flush=True,
        )
        time.sleep(interval)
        ok, detail = _check_telegram_network()
    if not ok:
        print(
            f">>> TG 网络连续 {max_retries + 1} 次不通（{detail}），放弃执行",
            flush=True,
        )
        return False
    print(f">>> TG 网络正常（{detail}），开始执行子任务", flush=True)
    return True


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
    raw_start_at = spec.get("start_at")
    start_at = _parse_start_at(raw_start_at)
    if raw_start_at and start_at is None:
        print(f"开始时间格式无效: {raw_start_at}", flush=True)
        raise SystemExit(2)
    if start_at is not None:
        _wait_until_start(start_at)
        interval, retries = _parse_retry_settings(spec)
        if not _wait_for_network(interval, retries):
            raise SystemExit(3)
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
