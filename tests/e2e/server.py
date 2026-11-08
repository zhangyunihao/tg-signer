"""在独立端口、独立数据目录启动被测 WebUI 子进程。

隔离原则：
- 运行目录是临时目录，工作目录固定为 <run_dir>/.signer，绝不读写用户真实 .signer
- 子进程 cwd 指向临时目录，因此账号扫描（Path(".").glob("*.session")）也看不到真实账号
- 不使用 8080/8099 等常用端口，端口由系统分配后排除禁用列表
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PORTS = {8080, 8099}


def _default_server_python() -> str:
    override = os.environ.get("TG_SIGNER_UI_SERVER_PYTHON")
    if override:
        return override
    candidates = (
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _pick_free_port() -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("无法分配可用端口")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_dataset(run_dir: Path) -> Path:
    """造最小数据集：一个签到配置、一条任务历史、一份日志。"""
    workdir = run_dir / ".signer"
    _write_json(
        workdir / "signs" / "demo" / "config.json",
        {
            "chats": [
                {
                    "chat_id": "@demo_channel",
                    "name": "示例签到",
                    "actions": [{"action": 1, "text": "签到"}],
                    "action_interval": 1,
                }
            ],
            "sign_at": "0 6 * * *",
            "random_seconds": 0,
            "sign_interval": 1,
        },
    )
    _write_json(
        workdir / "webui_task_history.json",
        [
            {
                "id": "rec-1",
                "task": "demo",
                "task_type": "签到",
                "task_name": "demo",
                "status": "成功",
                "accounts": "demo_account",
                "start_time": "2026-01-01 12:00:00",
                "duration": "3秒",
                "result": "签到完成",
                "details": [
                    {
                        "account": "demo_account",
                        "chat": "@demo_channel",
                        "status": "成功",
                    }
                ],
            }
        ],
    )
    _write_json(
        workdir / "channel_stats.json",
        {
            "updated_at": "2026-01-01 12:00:00",
            "accounts": {
                "demo_account": {
                    "joined": 12,
                    "limit": 500,
                    "groups": 4,
                    "channels": 8,
                }
            },
        },
    )
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "tg-signer.log").write_text(
        "\n".join(
            [
                "2026-01-01 12:00:00 INFO WebUI 测试数据集已加载",
                "2026-01-01 12:00:01 INFO 示例签到任务准备就绪",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return workdir


class UiServer:
    """被测 WebUI 子进程的生命周期管理。"""

    def __init__(
        self,
        server_python: Optional[str] = None,
        port: Optional[int] = None,
        run_dir: Optional[Path] = None,
        keep_run_dir: bool = False,
        startup_timeout: float = 60.0,
        extra_env: Optional[dict[str, str]] = None,
        seed: bool = True,
    ) -> None:
        self.server_python = server_python or _default_server_python()
        self.port = port or _pick_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.run_dir = (
            Path(run_dir) if run_dir else Path(tempfile.mkdtemp(prefix="tg-signer-ui-"))
        )
        self.keep_run_dir = keep_run_dir
        self.startup_timeout = startup_timeout
        self.extra_env = dict(extra_env or {})
        self.seed = seed
        self.proc: Optional[subprocess.Popen] = None
        self._stdout = None
        self._stderr = None

    @property
    def log_paths(self) -> tuple[Path, Path]:
        return (
            self.run_dir / "server.stdout.log",
            self.run_dir / "server.stderr.log",
        )

    def __enter__(self) -> "UiServer":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.seed:
            workdir = seed_dataset(self.run_dir)
        else:
            # 调用方已在启动前自行播种（Store 模式下必须发生在 ui.run 之前）
            workdir = self.run_dir / ".signer"
            workdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TG_SIGNER_WORKDIR"] = str(workdir)
        env.update(self.extra_env)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 不使用授权码，避免每个视口都要先过验证页
        env.pop("TG_SIGNER_GUI_AUTHCODE", None)
        # pytest 会把 PYTEST_CURRENT_TEST 传给子进程，NiceGUI 据此进入 pytest 模式并
        # 读取 NICEGUI_SCREEN_TEST_PORT，导致测试自行启动的 WebUI 直接退出。
        env.pop("PYTEST_CURRENT_TEST", None)
        env.pop("NICEGUI_USER_SIMULATION", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        out_path, err_path = self.log_paths
        self._stdout = open(out_path, "w", encoding="utf-8")
        self._stderr = open(err_path, "w", encoding="utf-8")
        cmd = [
            self.server_python,
            "-c",
            "from tg_signer.__main__ import signer; signer()",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.run_dir),
            env=env,
            stdout=self._stdout,
            stderr=self._stderr,
        )
        try:
            self._wait_ready()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("WebUI 子进程提前退出：\n" + self.tail_logs())
            try:
                with urllib.request.urlopen(f"{self.base_url}/", timeout=2) as resp:
                    if resp.status == 200:
                        return
                    last_error = f"HTTP {resp.status}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
            time.sleep(0.3)
        raise RuntimeError(
            f"WebUI 启动超时（最后状态：{last_error}）\n" + self.tail_logs()
        )

    def tail_logs(self, lines: int = 60) -> str:
        chunks = []
        for path in self.log_paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tail = "\n".join(text.splitlines()[-lines:])
            if tail.strip():
                chunks.append(f"--- {path.name} ---\n{tail}")
        return "\n".join(chunks) or "(无子进程日志)"

    def __exit__(self, *exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for handle in (self._stdout, self._stderr):
            if handle is not None:
                handle.close()
        if not self.keep_run_dir:
            import shutil

            shutil.rmtree(self.run_dir, ignore_errors=True)
