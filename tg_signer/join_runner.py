"""重新加入群组/频道子任务入口（由 WebUI「任务明细」勾选失败频道后启动）。

用法::

    python -m tg_signer.join_runner '<json spec>'

spec 格式::

    {"account": "账号名", "session_dir": ".signer", "task": "签到任务名",
     "chats": [{"token": "chat_id#thread_id", "label": "名称(chat_id)"}]}

签到失败常见原因是账号已不在群里，这里对勾选的频道调用 join_chat 重新加入：
优先使用签到任务配置里的原始 chat_id（可能是 @username 或邀请链接，私有群
必须用邀请链接才能加入），找不到配置项时才退回明细里的 chat_id。

日志为标记行，WebUI 据此写入任务明细::

    >>> 加群开始: <label>
    >>> 加群成功: <label>
    >>> 加群失败: <label>: <原因>
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pyrogram import errors, types

from tg_signer.config import SignConfigV3
from tg_signer.core import get_client, safe_disconnect


def _label_of(chat: Any) -> str:
    name = str(getattr(chat, "name", "") or "").strip()
    chat_id = getattr(chat, "chat_id", "")
    return f"{name}({chat_id})" if name else str(chat_id)


def _username_from_label(label: str) -> str:
    """从群名里提取 @username（很多签到配置把公开群用户名写在名称里）。"""
    match = re.search(r"@([A-Za-z][A-Za-z0-9_]{3,})", label or "")
    return match.group(1) if match else ""


def load_sign_chats(session_dir: Path, task: str) -> list[Any]:
    """读取签到任务的频道配置（读取失败返回空列表）。"""
    path = session_dir / "signs" / task / "config.json"
    if not path.is_file():
        return []
    try:
        # utf-8-sig 兼容手工编辑保存后带 BOM 的配置文件
        with open(path, "r", encoding="utf-8-sig") as fp:
            loaded = SignConfigV3.load(json.load(fp))
    except Exception:
        return []
    if not loaded:
        return []
    cfg, _ = loaded
    return list(cfg.chats)


def resolve_target(item: dict, chats: list[Any]) -> tuple[Any, str]:
    """把明细定位串映射回配置里的原始 chat_id；找不到时退回明细里的数值 id。"""
    token = str(item.get("token") or "").strip()
    label = str(item.get("label") or "").strip()
    chat_part, _, thread_part = token.partition("#")
    chat_part = chat_part.strip()
    thread_id = int(thread_part) if thread_part.strip().isdigit() else None
    for chat in chats:
        if chat_part and str(chat.chat_id) == chat_part:
            if thread_id is None or chat.message_thread_id == thread_id:
                return chat.chat_id, _label_of(chat)
    if label:
        for chat in chats:
            if _label_of(chat) == label:
                return chat.chat_id, label
    target: Any = int(chat_part) if chat_part.lstrip("-").isdigit() else chat_part
    return target, label or chat_part


async def join_one(client: Any, target: Any, label: str, log) -> bool:
    """加入单个群组/频道，返回是否成功（已加入也视为成功）。

    优先用配置里的原始 chat_id；失败且群名里带 @username 时，再用用户名重试一次
    （数字 id 对未缓存的私有群会被拒，公开群用用户名最稳）。
    """
    log(f">>> 加群开始: {label}")
    if not target:
        log(f">>> 加群失败: {label}: 无法从明细或签到配置定位该频道")
        return False
    attempts: list[Any] = [target]
    username = _username_from_label(label)
    if username and str(target).lower().lstrip("@") != username.lower():
        attempts.append(username)
    last_error = ""
    for index, attempt in enumerate(attempts):
        if index > 0:
            log(f">>> 加群重试: {label} 改用 @{attempt}")
        try:
            result = await asyncio.wait_for(client.join_chat(attempt), 90)
        except errors.UserAlreadyParticipant:
            log(f">>> 加群成功: {label}（已在该群组中）")
            return True
        except errors.FloodWait as exc:
            log(f">>> 加群失败: {label}: 触发限流，需等待 {exc.value} 秒")
            return False
        except errors.InviteRequestSent:
            log(f">>> 加群成功: {label}（已发送加入申请，等待管理员审批）")
            return True
        except asyncio.TimeoutError:
            last_error = "加入超时（网络或代理不可用）"
            continue
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
            continue
        if isinstance(result, types.ChatJoinResultRequestSent):
            log(f">>> 加群成功: {label}（已发送加入申请，等待管理员审批）")
            return True
        log(f">>> 加群成功: {label}")
        return True
    hint = ""
    if any(
        k in last_error
        for k in ("CHANNEL_INVALID", "CHANNEL_PRIVATE", "INVITE_HASH", "USER_NOT")
    ):
        hint = "（私有群无法用数字 ID 加入，需先获取邀请链接手动加入）"
    log(f">>> 加群失败: {label}: {last_error}{hint}")
    return False


async def run_join(spec: dict) -> int:
    account = str(spec.get("account") or "")
    if not account:
        print("缺少账号", flush=True)
        return 2

    def log(msg: str) -> None:
        print(msg, flush=True)

    # session_dir 是会话文件所在目录（与 CLI 一致，相对子进程 CWD，即 "."）；
    # workdir 是配置根目录（WebUI 工作目录，配置在 <workdir>/signs/<task>）
    session_dir = Path(str(spec.get("session_dir") or "."))
    workdir = Path(str(spec.get("workdir") or session_dir))
    chats = load_sign_chats(workdir, str(spec.get("task") or ""))
    items = [i for i in (spec.get("chats") or []) if isinstance(i, dict)]
    if not items:
        log(f"[{account}] 没有需要重新加入的频道")
        return 0
    if not chats:
        log(f"[{account}] 未读到签到任务频道配置，按明细 chat_id 尝试加入")

    delay = float(os.environ.get("TG_SIGNER_START_DELAY", "0") or 0)
    if delay > 0:
        log(f"[{account}] 多账号错峰启动：等待 {int(delay)} 秒后开始加群")
        await asyncio.sleep(delay)

    client = None
    failed = 0
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，无法重新加入")
            return 1
        for item in items:
            target, label = resolve_target(item, chats)
            if not await join_one(client, target, label, log):
                failed += 1
        log(f"[{account}] 重新加入完成：成功 {len(items) - failed}/{len(items)}")
    finally:
        await safe_disconnect(client)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("缺少 spec 参数", file=sys.stderr, flush=True)
        return 2
    try:
        spec = json.loads(args[0])
    except json.JSONDecodeError as exc:
        print(f"spec 解析失败: {exc}", file=sys.stderr, flush=True)
        return 2
    return asyncio.run(run_join(spec))


if __name__ == "__main__":
    sys.exit(main())
