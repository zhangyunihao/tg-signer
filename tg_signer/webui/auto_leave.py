"""自动退群：任务列表模式。

新建任务（选择账号、不更新天数阈值、扫描范围、包含/保护关键字、自动退出开关），
启动后按扫描范围（仅频道/仅群组/频道+群组）扫描所选账号超过 N 天未更新的对话
（N 支持小数，如 0.5 天；填 0 表示扫描全部对话，此时不允许自动退出）：
- 填写包含关键字时，标题命中任一关键字（或关系）的对话才进入扫描结果（留空不限制）；
- 未命中保护关键字的对话：开关打开时自动退出（无需确认，仅扫描范围为「仅频道」时可选）；
- 命中保护关键字的对话：始终展示在任务结果里，可手动勾选后退出。
自己是创建者的对话永远不会被退出。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List

from nicegui import ui
from pyrogram.errors import FloodWait, RPCError

from tg_signer.core import get_client, safe_disconnect, timeout_aiter
from tg_signer.utils import atomic_write_json
from tg_signer.webui.activity import should_poll
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    copy_text_to_clipboard,
    format_duration,
    mobile_card_fields,
    mobile_card_result,
    with_select_all,
)
from tg_signer.webui.random_chat import (
    build_account_options,
    list_session_names,
)

logger = logging.getLogger(__name__)

# 单次网络调用超时（秒）：手机代理不稳定场景，超时明确失败而非永久挂起
_NET_CALL_TIMEOUT = 90

CONFIG_NAME = "auto_leave_config.json"
LEAVE_TASKS_FILE = "webui_leave_tasks.json"

# 保护关键字（频道名命中即不自动退出，仅在结果中展示供手动勾选）
UNSELECT_KEYWORDS = ("抽奖", "嫩妹社", "野寻", "榜")

DEFAULT_KEYWORDS = "抽奖,嫩妹社,野寻,榜"

# 运行中任务的停止开关（跨浏览器标签页共享，供「停止所有任务」使用）
# {workdir|task_id|token -> stop_fn}
_ACTIVE_STOPS: Dict[str, Callable[[], None]] = {}

# 模块级页面日志缓冲：按 workdir 存储页面级退频道日志，
# 页面刷新后从这里回放历史日志，运行中任务也能继续往这里追加。
_LEAVE_GLOBAL_LOGS: Dict[str, List[str]] = {}
# 模块级活跃 log_area 引用：按 workdir 存储当前页面的 ui.log 对象，
# 让后台运行的任务能把日志推送到最新页面。
_LEAVE_ACTIVE_LOG_AREA: Dict[str, "ui.log"] = {}


def stop_all_leave_tasks(workdir) -> int:
    """请求停止当前工作目录下所有运行中的退频道任务，返回请求停止的数量。"""
    prefix = f"{Path(workdir).resolve()}|"
    count = 0
    for key, stop in list(_ACTIVE_STOPS.items()):
        if not key.startswith(prefix):
            continue
        try:
            stop()
            count += 1
        except Exception:
            logger.debug("停止退频道任务失败", exc_info=True)
    return count


def _to_days(value, default: float) -> float:
    """把天数阈值统一成非负浮点数；非法值回退到默认值。"""
    try:
        days = float(value)
    except (TypeError, ValueError):
        days = float(default)
    return max(0.0, days)


def _fmt_days(value) -> str:
    """天数显示：整数不带小数位，小数原样显示（1.5 -> "1.5"）。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _as_utc(value: datetime) -> datetime:
    """把消息时间统一成 UTC

    Pyrogram 2.x 的 message.date 由 datetime.fromtimestamp(ts) 生成，
    是「本地时区」的 naive datetime；naive 用 astimezone 会按系统本地时区解释，
    正好对应其生成方式，aware datetime 也能直接转换。
    """
    return value.astimezone(timezone.utc)


def _config_path(workdir) -> Path:
    return Path(workdir) / CONFIG_NAME


def load_config(workdir) -> Dict:
    """读取自动退群配置，文件缺失或损坏时返回默认值"""
    defaults = {
        "session_dir": ".",
        "days": 7,
        "accounts": [],
        "include_groups": False,
        "skip_pinned": True,
        # 页面级「扫描跳过」配置：标题命中任一关键字的频道/群组直接跳过
        "skip_keywords": [],
    }
    try:
        data = json.loads(_config_path(workdir).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update({k: v for k, v in data.items() if k in defaults})
    except Exception:
        logger.debug("读取自动退群配置文件失败，使用默认配置", exc_info=True)
    return defaults


def save_config(workdir, cfg: Dict) -> None:
    try:
        atomic_write_json(_config_path(workdir), cfg)
    except Exception:
        logger.debug("保存自动退群配置文件失败", exc_info=True)


def load_skip_keywords(workdir) -> List[str]:
    """读取页面级「扫描跳过」关键字（标题命中即跳过扫描结果）。"""
    return [
        str(kw).strip()
        for kw in (load_config(workdir).get("skip_keywords") or [])
        if str(kw).strip()
    ]


def _tasks_path(workdir) -> Path:
    return Path(workdir) / LEAVE_TASKS_FILE


def _scan_types_of(obj: Dict) -> str:
    """读取扫描范围；旧配置只有 include_groups 布尔时按其推导（True→both）"""
    st = str(obj.get("scan_types") or "")
    if st in ("channel", "group", "both"):
        return st
    return "both" if obj.get("include_groups") else "channel"


def load_leave_tasks(workdir) -> List[Dict]:
    """读取退频道任务列表；缺失时尝试从旧版单配置迁移出一条默认任务"""
    try:
        data = json.loads(_tasks_path(workdir).read_text(encoding="utf-8"))
        if isinstance(data, list):
            tasks = [t for t in data if isinstance(t, dict) and t.get("id")]
            for t in tasks:
                t.setdefault("scan_types", _scan_types_of(t))
                t.setdefault("include_keywords", [])
            return tasks
    except Exception:
        logger.debug("读取退频道任务列表失败，视为空", exc_info=True)
    cfg = load_config(workdir)
    if cfg.get("accounts"):
        return [
            {
                "id": f"leave_{datetime.now():%Y%m%d%H%M%S}",
                "name": "默认退频道任务",
                "accounts": list(cfg.get("accounts") or []),
                "days": float(cfg.get("days") or 7),
                "scan_types": _scan_types_of(cfg),
                "skip_pinned": bool(cfg.get("skip_pinned", True)),
                "keywords": list(UNSELECT_KEYWORDS),
                "include_keywords": [],
                "auto_leave": False,
            }
        ]
    return []


def save_leave_tasks(workdir, tasks: List[Dict]) -> None:
    try:
        atomic_write_json(_tasks_path(workdir), tasks)
    except Exception:
        logger.debug("保存退频道任务列表失败", exc_info=True)


# 频道/群组已满时自动创建的退频道任务：阈值 1 天、仅扫频道、未命中保护字自动退出
CHANNELS_FULL_DAYS = 1
_AUTO_TASK_ACTIVE_STATUSES = ("待执行", "排队等待账号空闲", "扫描中", "退出中")


def tasks_mtime_ns(workdir) -> int:
    """任务文件修改时间，供界面轮询外部（自动触发）任务的变化。"""
    try:
        return _tasks_path(workdir).stat().st_mtime_ns
    except OSError:
        return 0


def _patch_leave_task(workdir, task_id: str, **fields) -> Dict | None:
    """重新读取后更新单个任务，避免覆盖其他进程刚写入的字段。"""
    tasks = load_leave_tasks(workdir)
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if task is None:
        return None
    task.update(fields)
    save_leave_tasks(workdir, tasks)
    return task


def create_auto_leave_task(
    workdir,
    account: str,
    days: float = CHANNELS_FULL_DAYS,
    detail: str = "",
    status: str = "待执行",
) -> Dict | None:
    """频道/群组已满时创建退频道任务。

    保护关键字取默认值（命中即保留待确认），未命中的频道自动退出；
    同一账号已有排队/运行中的自动任务时不重复创建。
    """
    account = str(account or "").strip()
    if not account:
        return None
    tasks = load_leave_tasks(workdir)
    for t in tasks:
        if t.get("trigger") != "channels_full":
            continue
        if account not in (t.get("accounts") or []):
            continue
        if str(t.get("status") or "") in _AUTO_TASK_ACTIVE_STATUSES:
            return None
    now = datetime.now()
    task = {
        "id": f"leave_{now:%Y%m%d%H%M%S%f}",
        "name": f"退频道任务 {now:%m-%d %H:%M}（频道已满）",
        "accounts": [account],
        "days": float(days),
        "scan_types": "channel",
        "skip_pinned": True,
        "keywords": list(UNSELECT_KEYWORDS),
        "include_keywords": [],
        "auto_leave": True,
        "status": str(status or "待执行"),
        "trigger": "channels_full",
        "reason": str(detail or "")[:200],
        "start_time": "",
        "duration": "",
        "rows": [],
    }
    tasks.append(task)
    save_leave_tasks(workdir, tasks)
    return task


async def run_auto_leave_task(
    workdir,
    session_dir,
    task_id: str,
    wait_busy: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """后台执行自动创建的退频道任务（不依赖界面，抽奖任务可直接调用）。"""
    log_fn = log or logger.info
    queued = False
    while wait_busy is not None and wait_busy():
        if not queued:
            log_fn(f"账号仍被抽奖任务占用，排队等待空闲后执行退频道（{task_id}）")
            queued = True
        if _patch_leave_task(workdir, task_id, status="排队等待账号空闲") is None:
            return
        await asyncio.sleep(5)
    task = _patch_leave_task(workdir, task_id)
    if task is None:
        return
    days = _to_days(task.get("days"), CHANNELS_FULL_DAYS)
    if days <= 0:
        # 自动任务必须带阈值，避免 0（扫描全部）导致全量退出
        days = float(CHANNELS_FULL_DAYS)
    path = Path(str(session_dir or ".")) if session_dir else Path(".")
    started = time.time()
    auto_left = 0
    rows: List[Dict] = []
    status = ""
    task = _patch_leave_task(
        workdir,
        task_id,
        status="扫描中",
        start_time=f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        duration="",
    )
    if task is None:
        return
    task_name = str(task.get("name") or task_id)
    skip_keywords = load_skip_keywords(workdir)
    log_fn(
        f"[{task_name}] 启动：{', '.join(task.get('accounts') or [])}，"
        f"阈值 {_fmt_days(days)} 天"
    )
    stop_flag = {"stop": False}
    stop_key = f"{Path(workdir).resolve()}|{task_id}|auto"
    _ACTIVE_STOPS[stop_key] = lambda: stop_flag.__setitem__("stop", True)
    try:
        for account in list(task.get("accounts") or []):
            if stop_flag["stop"]:
                break
            found = await scan_stale_chats(
                account,
                path,
                days,
                _scan_types_of(task),
                bool(task.get("skip_pinned", True)),
                log_fn,
                skip_keywords=skip_keywords,
            )
            auto_rows = [r for r in found if not _kw_hit(task, r["title"])]
            kept = [r for r in found if _kw_hit(task, r["title"])]
            if auto_rows:
                _patch_leave_task(workdir, task_id, status="退出中", rows=rows + kept)
                done = await _leave_rows(
                    account,
                    path,
                    auto_rows,
                    log_fn,
                    should_stop=lambda: stop_flag["stop"],
                )
                auto_left += len(done)
            rows.extend(kept)
            log_fn(
                f"[{account}] 命中保护关键字 {len(kept)} 个（待手动确认），"
                f"未命中 {len(auto_rows)} 个自动退出"
            )
        status = (
            "已停止"
            if stop_flag["stop"]
            else f"完成：自动退出 {auto_left} 个，待确认 {len(rows)} 个"
        )
    except Exception as exc:
        status = f"异常：{exc}"
        log_fn(f"自动退频道任务运行异常: {exc}")
        logger.debug("自动退频道任务运行异常", exc_info=True)
    finally:
        _ACTIVE_STOPS.pop(stop_key, None)
        _patch_leave_task(
            workdir,
            task_id,
            status=status,
            rows=[dict(r) for r in rows],
            duration=format_duration(time.time() - started),
        )
        log_fn(f"[{task_name}] {status}")


def _chat_type(chat) -> str:
    return str(getattr(chat, "type", "")).upper().replace("CHATTYPE.", "")


async def scan_stale_chats(
    account: str,
    session_dir: Path,
    days: float,
    scan_types: str = "both",
    skip_pinned: bool = True,
    log: Callable[[str], None] = None,
    skip_keywords: List[str] | None = None,
) -> List[Dict]:
    """扫描单个账号，返回超过 days 天不更新的对话列表（只读，不做任何退出）

    scan_types: 扫描范围——"channel" 仅频道 / "group" 仅群组 / "both" 频道+群组
    days: 不更新天数阈值（支持小数，如 0.5），0 表示不按天数过滤（扫描全部符合条件的对话）
    skip_keywords: 页面级跳过关键字，标题命中即直接跳过（不进入结果）
    """
    scan_types = str(scan_types or "both")
    want_channel = scan_types in ("channel", "both")
    want_group = scan_types in ("group", "both")
    days = _to_days(days, 0)
    skip_keywords = [str(kw).strip() for kw in (skip_keywords or []) if str(kw).strip()]
    found: List[Dict] = []
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过")
            return found
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days) if days > 0 else None
        scanned = 0
        # 逐项拉取套超时：代理僵死时明确失败，不永久挂起
        async for dialog in timeout_aiter(
            client.get_dialogs(), _NET_CALL_TIMEOUT, "get_dialogs"
        ):
            scanned += 1
            if scanned % 200 == 0:
                log(f"[{account}] 已扫描 {scanned} 个对话...")
            chat = dialog.chat
            ctype = _chat_type(chat)
            is_channel = ctype == "CHANNEL"
            is_group = ctype in ("GROUP", "SUPERGROUP")
            if not ((is_channel and want_channel) or (is_group and want_group)):
                continue
            title = getattr(chat, "title", None) or "N/A"
            if getattr(chat, "is_creator", False):
                continue  # 自己是创建者，绝不退出
            # 页面级跳过配置：标题命中即直接跳过，不进入扫描结果
            if skip_keywords and any(
                kw.lower() in str(title).lower() for kw in skip_keywords
            ):
                log(f"[{account}] 跳过指定频道/群组：{title}")
                continue
            if skip_pinned and getattr(dialog, "pinned_message", None) is not None:
                log(f"[{account}] 跳过置顶对话：{title}")
                continue
            top = dialog.top_message
            if top is None or getattr(top, "date", None) is None:
                log(f"[{account}] 无最新消息信息，跳过：{title}")
                continue
            top_date = _as_utc(top.date)
            if cutoff is None or top_date < cutoff:
                age_days = round(max(0.0, (now - top_date).total_seconds() / 86400), 1)
                found.append(
                    {
                        "key": f"{account}:{chat.id}",
                        "account": account,
                        "chat_id": chat.id,
                        "title": title,
                        "type": ctype,
                        "age_days": age_days,
                    }
                )
        stale_desc = (
            f"超过 {_fmt_days(days)} 天未更新" if days > 0 else "全部（未按天数过滤）"
        )
        log(f"[{account}] 扫描完成，共 {scanned} 个对话，{len(found)} 个{stale_desc}")
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
    except Exception as exc:
        log(f"[{account}] 扫描失败: {exc}")
    finally:
        if client is not None:
            await safe_disconnect(client)
    return found


async def leave_one(client, chat_id, title: str, log: Callable[[str], None]) -> bool:
    """退出单个对话，处理 FloodWait（等待后重试一次）；单次调用限时网络超时"""

    for attempt in (1, 2):
        try:
            await asyncio.wait_for(client.leave_chat(chat_id), _NET_CALL_TIMEOUT)
            log(f"[已退出] {title} ({chat_id})")
            return True
        except asyncio.TimeoutError:
            log(f"[失败] {title}: 网络超时（{_NET_CALL_TIMEOUT} 秒，网络异常）")
            return False
        except FloodWait as exc:
            wait = min(int(getattr(exc, "value", 0) or 60), 120)
            if attempt == 2:
                log(f"[失败] {title}: FloodWait 超时放弃")
                return False
            log(f"[限流] 等待 {wait}s 后重试：{title}")
            await asyncio.sleep(wait + 1)
        except RPCError as exc:
            log(f"[失败] {title}: {exc}")
            return False
    return False


async def _leave_rows(
    account: str,
    session_dir: Path,
    rows: List[Dict],
    log: Callable[[str], None],
    should_stop: Callable[[], bool] | None = None,
) -> set:
    """按账号并发退出给定对话（并发 8），返回成功退出的 key 集合"""
    done: set = set()
    if not rows:
        return done
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过 {len(rows)} 个")
            return done
        log(f"[{account}] 开始批量退出 {len(rows)} 个对话（并发 8）")
        done_lock = asyncio.Lock()
        sem = asyncio.Semaphore(8)

        async def _do_leave(row: Dict) -> None:  # noqa: B023
            if should_stop is not None and should_stop():
                return
            async with sem:  # noqa: B023
                success = await leave_one(
                    client,  # noqa: B023
                    row["chat_id"],
                    row["title"],
                    log,  # noqa: B023
                )
                if success:
                    async with done_lock:  # noqa: B023
                        done.add(row["key"])  # noqa: B023

        await asyncio.gather(*[_do_leave(row) for row in rows])
        log(f"[{account}] 批量退出完成：成功 {len(done)}/{len(rows)}")
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
    except Exception as exc:
        log(f"[{account}] 退出失败: {exc}")
    finally:
        if client is not None:
            await safe_disconnect(client)
    return done


def _split_keywords(text: str) -> List[str]:
    return [
        kw.strip() for kw in (text or "").replace("，", ",").split(",") if kw.strip()
    ]


def _kw_hit(task: Dict, title: str) -> bool:
    return any(kw in str(title or "") for kw in (task.get("keywords") or []))


def auto_leave_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建「批量退频道」标签页内容，返回刷新函数以纳入全局刷新。"""
    session_dir = Path(default_session_dir or ".")
    wd_key = str(Path(workdir).resolve())
    tasks = load_leave_tasks(workdir)
    if tasks:
        save_leave_tasks(workdir, tasks)  # 旧配置迁移后立即落盘

    # 每个任务（执行记录）的运行时状态：running/stop/rows/selected/status/auto_left
    # rows（命中/待确认频道）从任务记录恢复，任务完成后重新打开仍可见
    def _default_state(task: Dict) -> Dict:
        rows = [dict(r) for r in (task.get("rows") or []) if isinstance(r, dict)]
        return {
            "running": False,
            "stop": False,
            "rows": rows,
            # 未命中保护关键字的对话默认勾选，命中的保持不勾等手动确认
            "selected": {
                r["key"] for r in rows if not _kw_hit(task, str(r.get("title") or ""))
            },
            "status": str(task.get("status") or "—"),
            "auto_left": 0,
        }

    task_states: Dict[str, Dict] = {t["id"]: _default_state(t) for t in tasks}
    # 从模块级全局缓冲获取日志列表：页面刷新后可回放历史，
    # 运行中后台协程也能继续往这里追加。
    log_lines = _LEAVE_GLOBAL_LOGS.setdefault(wd_key, [])
    # 页面级持久弹窗的引用与状态（弹窗不在事件槽位里动态创建）
    detail_refs: Dict = {"task_id": None, "body": None, "count": None}
    delete_state: Dict = {"task_id": None}
    cards_limit = {"n": 5}

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        log_lines.append(line)
        if len(log_lines) > 1000:
            del log_lines[:-1000]
        # 推送到当前活跃页面的 log_area（如果存在）；
        # 后台运行的任务在页面刷新后，会通过 _LEAVE_ACTIVE_LOG_AREA
        # 找到新页面的 ui.log 对象继续推送。
        active_area = _LEAVE_ACTIVE_LOG_AREA.get(wd_key)
        if active_area is not None:
            try:
                active_area.push(line)
            except Exception:
                logger.debug("推送日志到界面日志区失败", exc_info=True)

    def _skip_btn_text() -> str:
        n = len(load_skip_keywords(workdir))
        return f"跳过设置（{n}）" if n else "跳过设置"

    def find_task(task_id: str) -> Dict | None:
        return next((t for t in tasks if t["id"] == task_id), None)

    def _seq_of(task_id: str) -> int:
        # 序号按创建顺序编号（最早的任务为 1，与发言任务一致）
        return next(
            (idx for idx, t in enumerate(tasks, start=1) if t["id"] == task_id), 0
        )

    async def run_task(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task or not st or st["running"]:
            return
        accounts = list(task.get("accounts") or [])
        if not accounts:
            ui.notify("请先为该任务选择账号（删除后新建）", type="warning")
            return
        st["running"] = True
        st["stop"] = False
        st["rows"].clear()
        st["selected"].clear()
        st["auto_left"] = 0
        st["status"] = "扫描中"
        st["start_ts"] = time.time()
        stop_key = f"{wd_key}|{task_id}|run|{id(st)}"
        _ACTIVE_STOPS[stop_key] = lambda: st.__setitem__("stop", True)
        task["start_time"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        task["duration"] = ""
        # 运行期间立即落盘状态：其他标签页/刷新后仍能看到「扫描中」而不是「—」
        _patch_leave_task(
            workdir,
            task_id,
            status=st["status"],
            start_time=task["start_time"],
            duration="",
        )
        render_list()
        days = _to_days(task.get("days"), 7)
        skip_keywords = load_skip_keywords(workdir)
        log(
            f"[{task['name']}] 启动：{len(accounts)} 个账号，"
            + (f"阈值 {_fmt_days(days)} 天" if days > 0 else "阈值 0 天（扫描全部）")
        )
        auto_leave_on = bool(task.get("auto_leave"))
        if auto_leave_on and days <= 0:
            # 不更新天数为 0（扫描全部对话）时不允许自动退出
            log(
                f"[{task['name']}] 不更新天数为 0（扫描全部），自动退出不可用，仅扫描记录"
            )
            auto_leave_on = False
        if auto_leave_on and _scan_types_of(task) != "channel":
            # 扫描范围包含群组时不允许自动退出（仅频道可自动退出）
            log(f"[{task['name']}] 扫描范围包含群组，自动退出不可用，仅扫描记录")
            auto_leave_on = False
        try:
            for account in accounts:
                if st["stop"]:
                    log(f"[{task['name']}] 已停止，跳过账号 {account}")
                    break
                found = await scan_stale_chats(
                    account,
                    session_dir,
                    days,
                    _scan_types_of(task),
                    bool(task.get("skip_pinned", True)),
                    log,
                    skip_keywords=skip_keywords,
                )
                inc_kws = [str(k) for k in (task.get("include_keywords") or []) if k]
                if inc_kws:
                    before = len(found)
                    found = [
                        r for r in found if any(k in str(r["title"]) for k in inc_kws)
                    ]
                    log(f"[{account}] 包含关键字过滤：{before} -> {len(found)} 个")
                if auto_leave_on:
                    auto_rows = [r for r in found if not _kw_hit(task, r["title"])]
                    if auto_rows:
                        done = await _leave_rows(
                            account,
                            session_dir,
                            auto_rows,
                            log,
                            should_stop=lambda: st["stop"],
                        )
                        st["auto_left"] += len(done)
                    kept = [r for r in found if _kw_hit(task, r["title"])]
                    log(
                        f"[{account}] 命中保护关键字 {len(kept)} 个（待手动确认），"
                        f"未命中 {len(auto_rows)} 个自动退出"
                    )
                    st["rows"].extend(kept)
                else:
                    st["rows"].extend(found)
                    # 不自动退出时，未命中保护关键字的对话默认勾选，命中关键字的不勾
                    st["selected"].update(
                        r["key"]
                        for r in found
                        if not _kw_hit(task, str(r.get("title") or ""))
                    )
                render_list()
            if st["stop"]:
                st["status"] = "已停止"
            else:
                st["status"] = (
                    f"完成：自动退出 {st['auto_left']} 个，待确认 {len(st['rows'])} 个"
                )
            log(f"[{task['name']}] {st['status']}")
        except Exception as exc:
            st["status"] = f"异常：{exc}"
            log(f"[{task['name']}] 运行异常: {exc}")
            logger.debug("退频道任务运行异常", exc_info=True)
        finally:
            _ACTIVE_STOPS.pop(stop_key, None)
            st["running"] = False
            # 终态与命中频道写回记录并持久化，刷新页面后仍可见
            task["status"] = st["status"]
            task["rows"] = [dict(r) for r in st["rows"]]
            if st.get("start_ts"):
                task["duration"] = format_duration(time.time() - st["start_ts"])
            _patch_leave_task(
                workdir,
                task_id,
                status=task["status"],
                rows=task["rows"],
                duration=task["duration"],
            )
            render_list()

    def stop_task(task_id: str) -> None:
        st = task_states.get(task_id)
        if st:
            st["stop"] = True

    async def leave_selected(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task or not st or st["running"]:
            return
        selected = [r for r in st["rows"] if r["key"] in st["selected"]]
        if not selected:
            ui.notify("请先勾选要退出的对话", type="warning")
            return
        # 不需要二次确认：关闭明细弹窗后直接启动退出
        detail_dialog.close()
        st["running"] = True
        st["stop"] = False
        st["status"] = "退出中"
        stop_key = f"{wd_key}|{task_id}|leave|{id(st)}"
        _ACTIVE_STOPS[stop_key] = lambda: st.__setitem__("stop", True)
        # 运行期间立即落盘状态：其他标签页/刷新后仍能看到「退出中」
        _patch_leave_task(workdir, task_id, status=st["status"])
        ui.notify("退出流程已启动，可在任务列表查看进度", type="positive")
        render_list()
        try:
            by_account: Dict[str, List[Dict]] = {}
            for row in selected:
                by_account.setdefault(row["account"], []).append(row)
            done_keys: set = set()
            for account, items in by_account.items():
                if st["stop"]:
                    break
                done_keys |= await _leave_rows(
                    account,
                    session_dir,
                    items,
                    log,
                    should_stop=lambda: st["stop"],
                )
            if done_keys:
                st["rows"] = [r for r in st["rows"] if r["key"] not in done_keys]
                st["selected"].difference_update(done_keys)
            st["status"] = (
                "已停止" if st["stop"] else f"退出完成，剩余待确认 {len(st['rows'])} 个"
            )
        except Exception as exc:
            st["status"] = f"异常：{exc}"
            log(f"[{task['name']}] 退出异常: {exc}")
            logger.debug("退频道手动退出异常", exc_info=True)
        finally:
            _ACTIVE_STOPS.pop(stop_key, None)
            st["running"] = False
            task["status"] = st["status"]
            task["rows"] = [dict(r) for r in st["rows"]]
            _patch_leave_task(
                workdir, task_id, status=task["status"], rows=task["rows"]
            )
            _refresh_detail(task_id)
            render_list()

    def delete_task(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task:
            return
        if st and st["running"]:
            ui.notify("任务运行中，请先停止再删除", type="warning")
            return
        # 填充页面级持久确认弹窗（不在事件槽位里动态创建弹窗）
        delete_state["task_id"] = task_id
        delete_label.text = f"确认删除任务 #{_seq_of(task['id'])}？"
        delete_dialog.open()

    def _do_delete() -> None:
        tid = delete_state.get("task_id")
        task = find_task(tid) if tid else None
        delete_dialog.close()
        if not task:
            return
        seq = _seq_of(task["id"])
        tasks.remove(task)
        task_states.pop(task["id"], None)
        if detail_refs.get("task_id") == task["id"]:
            detail_refs.update({"task_id": None, "body": None, "count": None})
        save_leave_tasks(workdir, tasks)
        log(f"已删除任务 #{seq}")
        render_list()

    def open_create_dialog(prefill: Dict | None = None) -> None:
        prefill = prefill or {}
        names = list_session_names(session_dir, workdir)
        # 填充页面级持久新建/复制弹窗（不在事件槽位里动态创建弹窗）
        create_title.text = "复制退频道任务" if prefill else "新建退频道任务"
        account_sel.options = with_select_all(build_account_options(session_dir, names))
        account_sel.value = [
            a for a in (prefill.get("accounts") or []) if a in names
        ] or None
        account_sel.update()
        d = prefill.get("days")
        days_input.value = float(d) if d is not None else 7
        scope_sel.value = _scan_types_of(prefill)
        sp.value = bool(prefill.get("skip_pinned", True))
        kw_input.value = ",".join(prefill.get("keywords") or []) or DEFAULT_KEYWORDS
        ik_input.value = ",".join(prefill.get("include_keywords") or [])
        auto_cb.value = bool(prefill.get("auto_leave"))
        _sync_auto_cb()
        create_dialog.open()

    async def _do_create() -> None:
        accounts = list(account_sel.value or [])
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        scope = str(scope_sel.value or "both")
        d = days_input.value
        days = _to_days(d, 7)
        task = {
            "id": f"leave_{datetime.now():%Y%m%d%H%M%S%f}",
            "name": f"退频道任务 {datetime.now():%m-%d %H:%M}",
            "accounts": accounts,
            "days": days,
            "scan_types": scope,
            "skip_pinned": bool(sp.value),
            "keywords": _split_keywords(str(kw_input.value or "")),
            "include_keywords": _split_keywords(str(ik_input.value or "")),
            # 扫描范围包含群组、或天数为 0（扫描全部）时不允许自动退出
            "auto_leave": bool(auto_cb.value) and scope == "channel" and days > 0,
            "status": "—",
        }
        tasks.append(task)
        task_states[task["id"]] = _default_state(task)
        save_leave_tasks(workdir, tasks)
        create_dialog.close()
        render_list()
        # 每次执行都是一条新记录，创建后立即启动
        await run_task(task["id"])

    def _short_status(st: Dict) -> str:
        full = str(st.get("status") or "—")
        if st.get("running") or full in _AUTO_TASK_ACTIVE_STATUSES:
            return "运行中"
        if full.startswith("完成") or full.startswith("退出完成"):
            return "已完成"
        if full == "已停止":
            return "已停止"
        if full.startswith("异常"):
            return "失败"
        return "—"

    def _status_color(status: str) -> str:
        if status == "已完成":
            return "positive"
        if status == "失败":
            return "negative"
        if status == "运行中":
            return "info"
        return "grey-6"

    def _event_row(e) -> Dict | None:
        """从 Quasar 表格事件对象中取出行 dict（事件参数可能被包成 list）。"""
        row = getattr(e, "args", None)
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        return row if isinstance(row, dict) else None

    def _prefill_of(task: Dict) -> Dict:
        # 复制仅取配置，不带上次状态与命中频道
        return {k: v for k, v in task.items() if k not in ("status", "rows")}

    def _dup_by_id(task_id: str) -> None:
        task = find_task(task_id)
        if task:
            open_create_dialog(_prefill_of(task))

    # ---- 任务明细弹窗：展示命中/待确认频道，可勾选手动退出 ----

    def _detail_on_toggle(e, key: str, task_id: str) -> None:
        sts = task_states.get(task_id)
        if not sts:
            return
        if e.value:
            sts["selected"].add(key)
        else:
            sts["selected"].discard(key)
        cnt = detail_refs.get("count")
        if cnt is not None:
            cnt.text = f"已选 {len(sts['selected'])}/{len(sts['rows'])}"

    def _detail_select(task_id: str, all_on: bool) -> None:
        sts = task_states.get(task_id)
        if not sts:
            return
        if all_on:
            sts["selected"].update(r["key"] for r in sts["rows"])
        else:
            sts["selected"].clear()
        render_detail_rows(task_id)

    # 明细分组懒加载：每批渲染的分组数，条目过多时分批渲染避免卡顿
    DETAIL_CHUNK = 30

    def _render_detail_group(st: Dict, task_id: str, group: List[Dict]) -> None:
        group.sort(key=lambda r: (-r["age_days"], str(r["account"])))
        head = group[0]
        group_keys = {r["key"] for r in group}
        group_days = max(r["age_days"] for r in group)
        all_on = group_keys <= st["selected"]

        def _toggle_group(e, keys=group_keys, t=task_id) -> None:
            sts = task_states.get(t)
            if not sts:
                return
            if e.value:
                sts["selected"].update(keys)
            else:
                sts["selected"].difference_update(keys)
            render_detail_rows(t)

        with ui.column().classes("w-full gap-1 p-2 rounded border"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.checkbox(value=all_on, on_change=_toggle_group).props("dense")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(str(head["title"])).classes("text-sm font-semibold").style(
                        "overflow-wrap: anywhere;"
                    )
                    ui.label(
                        f"{head['type']}　{len(group)} 个账号"
                        + (
                            ""
                            if len(group) == 1
                            else f"　最多 {_fmt_days(group_days)} 天未更新"
                        )
                    ).classes("text-xs text-gray-500")
            for row in group:
                key = row["key"]
                with ui.row().classes("w-full items-center gap-2 no-wrap pl-6"):
                    ui.checkbox(
                        value=key in st["selected"],
                        on_change=lambda e, k=key, t=task_id: _detail_on_toggle(
                            e, k, t
                        ),
                    ).props("dense")
                    ui.label(
                        f"{row['account']}（{_fmt_days(row['age_days'])}天未更新）"
                    ).classes("text-xs").style("overflow-wrap: anywhere;")

    def render_detail_rows(task_id: str, append: bool = False) -> None:
        body = detail_refs.get("body")
        if body is None:
            return
        st = task_states.get(task_id) or {"rows": [], "selected": set()}
        rows = st["rows"]
        # 按频道聚合（chat_id 跨账号相同），组间按未更新天数由大到小排序
        groups: Dict = {}
        for row in rows:
            groups.setdefault(row.get("chat_id", row["key"]), []).append(row)
        ordered = sorted(
            groups.values(),
            key=lambda g: max(r["age_days"] for r in g),
            reverse=True,
        )

        if append:
            # 追加下一批前，移除上一批尾部的「加载更多」按钮与提示
            for ref_key in ("more", "tail"):
                old = detail_refs.pop(ref_key, None)
                if old is not None:
                    old.delete()
            start = detail_refs.get("shown") or 0
        else:
            body.clear()
            start = 0
            with body:
                if not rows:
                    detail_refs["shown"] = 0
                    ui.label("暂无待确认的频道").classes("text-sm text-gray-400")
                    return
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.button(
                        "全选", on_click=lambda t=task_id: _detail_select(t, True)
                    ).props("flat dense")
                    ui.button(
                        "全不选", on_click=lambda t=task_id: _detail_select(t, False)
                    ).props("flat dense")
                    ui.button(
                        "退出所选", on_click=lambda t=task_id: leave_selected(t)
                    ).props("outline dense color=negative")
                    detail_refs["count"] = ui.label(
                        f"已选 {len(st['selected'])}/{len(rows)}"
                    ).classes("text-xs text-gray-500 ml-auto")
            # 非追加渲染保留已展开的批次数，避免勾选分组后列表折叠回第一页
        if append:
            end = start + DETAIL_CHUNK
        else:
            end = detail_refs.get("shown") or DETAIL_CHUNK
        shown_end = min(end, len(ordered))
        detail_refs["shown"] = shown_end
        with body:
            for group in ordered[start:shown_end]:
                _render_detail_group(st, task_id, group)
            if shown_end < len(ordered):
                remaining = len(ordered) - shown_end
                detail_refs["more"] = ui.button(
                    f"加载更多（剩余 {remaining} 组）",
                    on_click=lambda t=task_id: render_detail_rows(t, append=True),
                ).props("flat dense full-width")
            else:
                detail_refs.pop("more", None)
            detail_refs["tail"] = ui.label("退出不可撤销，请仔细核对").classes(
                "text-xs text-gray-500"
            )

    def open_detail_dialog(task_id: str) -> None:
        task = find_task(task_id)
        if not task:
            return
        if task_id not in task_states:
            task_states[task_id] = _default_state(task)
        # 填充页面级持久明细弹窗（不在事件槽位里动态创建弹窗）
        detail_refs["task_id"] = task_id
        detail_refs["body"] = detail_body
        detail_refs["shown"] = 0  # 每次打开弹窗重置懒加载，从第一批开始渲染
        detail_title.text = f"任务明细 — #{_seq_of(task['id'])}"
        render_detail_rows(task_id)
        detail_dialog.open()

    def _refresh_detail(task_id: str) -> None:
        if (
            detail_refs.get("task_id") == task_id
            and detail_refs.get("body") is not None
        ):
            try:
                render_detail_rows(task_id)
            except Exception:
                logger.debug("刷新任务明细弹窗失败", exc_info=True)

    # ---- 任务列表：桌面表格 + 移动端卡片双渲染 ----

    def render_mobile_cards(rows: List[Dict]) -> None:
        cards_container.clear()
        with cards_container:
            shown = rows[: cards_limit["n"]]
            if not shown:
                ui.label("暂无执行记录，点击「新建任务」开始").classes(
                    "text-sm text-gray-400"
                )
            for row in shown:
                with ui.card().classes("w-full mobile-list-card border"):
                    with ui.row().classes(
                        "w-full items-center gap-2 no-wrap justify-between"
                    ):
                        with ui.row().classes("items-center gap-2 no-wrap min-w-0"):
                            ui.label(f"任务 #{row['seq']}").classes(
                                "text-base font-semibold"
                            )
                            if row.get("auto"):
                                ui.badge("自动").props("color=teal")
                        ui.badge(row["status"]).props(
                            f"color={_status_color(row['status'])}"
                        )
                    duration = row["duration"]
                    time_text = (
                        f"{row['start_time']}（耗时 {duration}）"
                        if duration and duration != "—"
                        else row["start_time"]
                    )
                    mobile_card_fields(
                        [
                            ("账号", row["accounts"], True),
                            ("执行时间", time_text, True),
                        ]
                    )
                    result_text = str(row["result"] or "")
                    if result_text.startswith("完成："):
                        result_text = result_text.removeprefix("完成：")
                    mobile_card_result(
                        "结果",
                        result_text,
                        runres=str(row["id"] or ""),
                    )
                    with ui.row().classes("w-full mobile-card-actions"):
                        ui.button(
                            "复制", on_click=lambda r=row: _dup_by_id(str(r["id"]))
                        ).props("flat dense color=primary")
                        ui.button(
                            "明细",
                            on_click=lambda r=row: open_detail_dialog(str(r["id"])),
                        ).props("flat dense color=grey-8")
                        if row["running"]:
                            ui.button(
                                "停止",
                                on_click=lambda r=row: stop_task(str(r["id"])),
                            ).props("flat dense color=negative")
                        ui.button(
                            "删除", on_click=lambda r=row: delete_task(str(r["id"]))
                        ).props("flat dense color=negative")
            if len(rows) > cards_limit["n"]:

                def _more() -> None:
                    cards_limit["n"] += 10
                    render_list()

                ui.button(f"展开更多（共 {len(rows)} 条）", on_click=_more).props(
                    "flat dense"
                ).classes("w-full")

    def render_list() -> None:
        # 最新执行的记录显示在最上方；序号按创建顺序编号（最早的任务为 1，与发言任务一致）
        rows = []
        for task in reversed(tasks):
            if task["id"] not in task_states:
                task_states[task["id"]] = _default_state(task)
            st = task_states[task["id"]]
            rows.append(
                {
                    "id": task["id"],
                    "seq": _seq_of(task["id"]),
                    "start_time": str(task.get("start_time") or "") or "—",
                    "duration": (
                        format_duration(time.time() - st["start_ts"])
                        if st.get("running") and st.get("start_ts")
                        else (str(task.get("duration") or "") or "—")
                    ),
                    "accounts": "、".join(str(a) for a in (task.get("accounts") or []))
                    or "未选择",
                    "status": _short_status(st),
                    "result": str(st.get("status") or "—"),
                    "running": bool(st.get("running")),
                    # 频道已满自动触发的任务，卡片/表格上标记「自动」
                    "auto": task.get("trigger") == "channels_full",
                }
            )
        leave_table.rows = rows
        leave_table.update()
        render_mobile_cards(rows)

    def _on_leave_dup(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            _dup_by_id(str(row["id"]))

    def _on_leave_detail(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            open_detail_dialog(str(row["id"]))

    def _on_leave_stop(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            stop_task(str(row["id"]))

    def _on_leave_del(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            delete_task(str(row["id"]))

    ui.label("批量退频道").classes("text-lg font-semibold")
    ui.label(
        "扫描所选账号超过指定天数没有新消息的频道（可选包含群组）并批量退出；"
        "不更新天数阈值支持小数（如 0.5），填 0 表示扫描全部频道/群组，此时不允许自动退出。"
        "开启「自动退出未命中关键字」后，未命中保护关键字的频道自动退出；"
        "命中关键字的频道在「明细」中展示，需手动勾选退出。自己是创建者的对话永远不会被退出。"
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-center"):
        ui.button("新建任务", on_click=lambda: open_create_dialog()).props(
            "color=primary"
        )
        ui.label(
            "每次执行都会在列表新增一条记录，可复制配置再次执行；点击「明细」查看命中的频道"
        ).classes("text-xs text-gray-500 ml-auto")
        skip_btn = (
            ui.button(_skip_btn_text(), on_click=lambda: open_skip_dialog())
            .props("flat dense color=primary")
            .tooltip("扫描时直接跳过指定频道/群组（标题命中即跳过，不展示也不退出）")
        )

    leave_table = (
        ui.table(
            columns=[
                {
                    "name": "seq",
                    "label": "序号",
                    "field": "seq",
                    "align": "left",
                },
                {
                    "name": "start_time",
                    "label": "执行时间",
                    "field": "start_time",
                    "align": "left",
                },
                {
                    "name": "duration",
                    "label": "执行时长",
                    "field": "duration",
                    "align": "left",
                },
                {
                    "name": "accounts",
                    "label": "账号",
                    "field": "accounts",
                    "align": "left",
                    "style": "max-width: 180px;",
                },
                {
                    "name": "status",
                    "label": "状态",
                    "field": "status",
                    "align": "left",
                },
                {
                    "name": "result",
                    "label": "结果",
                    "field": "result",
                    "align": "left",
                    "style": "min-width: 280px;",
                    "classes": "result-col",
                },
                {
                    "name": "actions",
                    "label": "操作",
                    "field": "actions",
                    "align": "left",
                    "classes": "col-actions",
                    "style": "white-space: nowrap;",
                },
            ],
            rows=[],
            pagination=5,
        )
        .classes("w-full tgs-desktop-table")
        .props("flat dense")
    )
    # 状态列渲染为彩色徽章：已完成=绿、失败=红、运行中=蓝、其他=灰
    # 频道已满自动触发的任务额外带一个「自动」标记
    leave_table.add_slot(
        "body-cell-status",
        '<q-td :props="props">'
        "<q-badge :color=\"props.row.status === '已完成' ? 'positive' : "
        "props.row.status === '失败' ? 'negative' : "
        "props.row.status === '运行中' ? 'info' : 'grey-6'\">"
        "{{ props.row.status }}</q-badge>"
        '<q-badge v-if="props.row.auto" color="teal" class="q-ml-xs">自动</q-badge>'
        "</q-td>",
    )
    # 账号列限宽：超长账号列表单行省略号截断，悬停显示完整内容
    leave_table.add_slot(
        "body-cell-accounts",
        '<q-td :props="props" style="max-width: 180px;">'
        '<div class="ellipsis" style="max-width: 180px;" :title="props.row.accounts">'
        "{{ props.row.accounts }}</div></q-td>",
    )
    # 操作列：复制 / 明细（命中频道）/ 停止（运行中）/ 删除
    leave_table.add_slot(
        "body-cell-actions",
        '<q-td :props="props" class="col-actions">'
        '<q-btn flat dense color="primary" '
        'label="复制" @click="$parent.$emit(\'leaveDup\', props.row)" />'
        '<q-btn flat dense color="grey-8" '
        'label="明细" @click="$parent.$emit(\'leaveDetail\', props.row)" />'
        '<q-btn v-if="props.row.running" flat dense color="negative" '
        'label="停止" @click="$parent.$emit(\'leaveStop\', props.row)" />'
        '<q-btn flat dense color="negative" '
        'label="删除" @click="$parent.$emit(\'leaveDel\', props.row)" />'
        "</q-td>",
    )
    leave_table.on("leaveDup", _on_leave_dup)
    leave_table.on("leaveDetail", _on_leave_detail)
    leave_table.on("leaveStop", _on_leave_stop)
    leave_table.on("leaveDel", _on_leave_del)
    # 移动端（<=640px）用纵向卡片列表替代横滑宽表，桌面端隐藏
    cards_container = ui.column().classes("w-full gap-2 tgs-mobile-cards mt-1")
    render_list()

    # ---- 页面级持久弹窗（统一在页面构建时创建，事件里只填充并打开，
    # 避免在表格单元格等事件槽位里动态创建弹窗导致 Quasar 渲染异常）----

    with (
        ui.dialog() as create_dialog,
        ui.card().classes("w-[520px] max-w-[96vw] max-h-[88vh] overflow-auto"),
    ):
        add_dialog_close_button(create_dialog)
        create_title = ui.label("新建退频道任务").classes("text-lg font-semibold")
        account_sel = ui.select(
            label="账号（可多选）", options=[], multiple=True, with_input=True
        ).classes("w-full")
        attach_select_all(account_sel)
        with ui.row().classes("w-full items-end flex-wrap gap-2"):
            days_input = ui.number(
                label="不更新天数阈值（天，可填小数；0=全部）",
                value=7,
                min=0,
                step=0.5,
            ).classes("w-56")
            scope_sel = ui.select(
                {"channel": "仅频道", "group": "仅群组", "both": "频道+群组"},
                value="both",
                label="扫描范围",
            ).classes("w-40")
            sp = ui.checkbox("跳过置顶", value=True)
        ui.label(
            "不更新天数阈值支持小数（如 0.5 天）；填 0 表示扫描全部频道/群组，"
            "此时不允许勾选自动退出"
        ).classes("text-xs text-gray-500")
        kw_input = ui.input("保护关键字（逗号分隔）", value=DEFAULT_KEYWORDS).classes(
            "w-full"
        )
        ik_input = ui.input(
            "包含关键字（逗号分隔，任一命中即算，留空不限制）", value=""
        ).classes("w-full")
        ui.label(
            "多个关键字为或关系：标题命中任一关键字即算；"
            "填写后仅命中的频道/群组会进入扫描结果（再按保护关键字区分自动退出与手动确认）"
        ).classes("text-xs text-gray-500")
        ui.label(
            "未命中关键字的频道将在启动后自动退出；命中关键字的频道仅展示，需手动勾选退出"
        ).classes("text-xs text-gray-500")
        auto_cb = ui.checkbox("自动退出未命中关键字的频道")

        def _sync_auto_cb() -> None:
            # 扫描范围包含群组、或天数为 0（扫描全部）时自动退出不可选
            days_ok = (days_input.value or 0) > 0
            if str(scope_sel.value or "both") == "channel" and days_ok:
                auto_cb.enable()
            else:
                auto_cb.value = False
                auto_cb.disable()
                if not days_ok:
                    auto_cb.tooltip("不更新天数为 0（扫描全部）时不允许自动退出")
                else:
                    auto_cb.tooltip("扫描范围包含群组时不允许自动退出")

        scope_sel.on_value_change(lambda _e: _sync_auto_cb())
        days_input.on_value_change(lambda _e: _sync_auto_cb())
        _sync_auto_cb()
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=create_dialog.close).props("flat")
            ui.button("启动执行", on_click=_do_create).props("color=primary")

    with (
        ui.dialog() as detail_dialog,
        ui.card().classes("w-[560px] max-w-[96vw] max-h-[88vh] overflow-auto"),
    ):
        add_dialog_close_button(detail_dialog)
        detail_title = ui.label("任务明细").classes("text-lg font-semibold")
        ui.label(
            "未命中保护关键字的对话已默认勾选，命中关键字的保持不勾；"
            "核对后点「退出所选」立即退出（无需二次确认）。明细已持久化，任务完成后重新打开仍可见。"
        ).classes("text-xs text-gray-500")
        detail_body = ui.column().classes("w-full gap-2")

    with ui.dialog() as delete_dialog, ui.card():
        add_dialog_close_button(delete_dialog)
        delete_label = ui.label("确认删除任务？").classes("text-lg font-semibold")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=delete_dialog.close).props("flat")
            ui.button("删除", on_click=_do_delete).props("color=negative")

    # ---- 扫描跳过设置：页面级配置，标题命中的频道/群组扫描时直接跳过 ----

    def open_skip_dialog() -> None:
        skip_input.value = ", ".join(load_skip_keywords(workdir))
        skip_input.update()
        skip_dialog.open()

    def _save_skip() -> None:
        kws = _split_keywords(str(skip_input.value or "").replace("\n", ","))
        cfg = load_config(workdir)
        cfg["skip_keywords"] = kws
        save_config(workdir, cfg)
        skip_btn.text = _skip_btn_text()
        skip_btn.update()
        skip_dialog.close()
        log("扫描跳过设置已更新：" + (", ".join(kws) if kws else "（空）"))

    with (
        ui.dialog() as skip_dialog,
        ui.card().classes("w-[520px] max-w-[96vw]"),
    ):
        add_dialog_close_button(skip_dialog)
        ui.label("扫描跳过设置").classes("text-lg font-semibold")
        ui.label(
            "标题命中任一名称/关键字的频道、群组在扫描时直接跳过（不展示、不自动退出）；"
            "多个用逗号或换行分隔，对所有退频道任务（含自动/组合）生效。"
        ).classes("text-xs text-gray-500")
        skip_input = (
            ui.textarea("跳过的频道/群组名称", value="")
            .classes("w-full")
            .props("outlined autogrow")
        )
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=skip_dialog.close).props("flat")
            ui.button("保存", on_click=_save_skip).props("color=primary")

    def copy_log() -> None:
        if not log_lines:
            ui.notify("暂无可复制的日志", type="warning")
            return
        copy_text_to_clipboard("\n".join(log_lines))
        ui.notify(f"已复制 {len(log_lines)} 行日志", type="positive")

    fullscreen_on = False

    def toggle_fullscreen() -> None:
        """切换运行日志全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
        nonlocal fullscreen_on
        fullscreen_on = not fullscreen_on
        if fullscreen_on:
            log_area.classes(add="tgs-page-log-fullscreen")
            exit_full_btn.classes(add="tgs-log-exit-visible")
        else:
            log_area.classes(remove="tgs-page-log-fullscreen")
            exit_full_btn.classes(remove="tgs-log-exit-visible")
        log_area.update()
        exit_full_btn.update()

    with ui.row().classes("w-full items-center justify-between mt-2"):
        ui.label("运行日志").classes("font-semibold")
        with ui.row().classes("gap-1 items-center"):
            ui.button(icon="content_copy", on_click=copy_log).props(
                "flat round dense"
            ).tooltip("一键复制日志")
            ui.button(icon="fullscreen", on_click=toggle_fullscreen).props(
                "flat round dense"
            ).tooltip("全屏显示")
    log_area = ui.log(max_lines=1000).classes("w-full h-48")
    # 注册当前活跃 log_area：后台运行的任务通过它推送到最新页面
    _LEAVE_ACTIVE_LOG_AREA[wd_key] = log_area
    for line in log_lines:
        try:
            log_area.push(line)
        except Exception:
            logger.debug("回放历史日志到界面日志区失败", exc_info=True)
    # 退出全屏悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
    exit_full_btn = (
        ui.button(icon="close", on_click=toggle_fullscreen)
        .props("flat round dense unelevated")
        .classes("tgs-log-exit-full")
    )

    def refresh() -> None:
        render_list()

    # 轮询任务文件：抽奖任务因频道已满在后台自动创建/推进的任务也要显示在本页
    last_mtime = {"v": tasks_mtime_ns(workdir)}

    def _sync_from_disk() -> None:
        disk = load_leave_tasks(workdir)
        known = {t["id"]: t for t in tasks}
        changed = False
        for item in disk:
            tid = item["id"]
            local = known.get(tid)
            if local is None:
                tasks.append(item)
                task_states[tid] = _default_state(item)
                changed = True
                continue
            st = task_states.get(tid)
            if st is None or st.get("running"):
                continue  # 本页正在执行的任务以内存状态为准
            local.update(item)
            st["status"] = str(item.get("status") or "—")
            st["rows"] = [
                dict(r) for r in (item.get("rows") or []) if isinstance(r, dict)
            ]
            st["selected"] = {
                r["key"]
                for r in st["rows"]
                if not _kw_hit(local, str(r.get("title") or ""))
            }
            changed = True
        if changed:
            render_list()

    poll_client = ui.context.client

    async def _poll_external_tasks() -> None:
        while True:
            await asyncio.sleep(3)
            # 页面不可见或不在该 tab 时跳过磁盘检查，切回后自动恢复
            if not should_poll(poll_client, "leave"):
                continue
            try:
                mtime = tasks_mtime_ns(workdir)
                if mtime == last_mtime["v"]:
                    continue
                last_mtime["v"] = mtime
                _sync_from_disk()
            except Exception:
                logger.debug("同步退频道任务列表失败", exc_info=True)

    asyncio.create_task(_poll_external_tasks())

    return refresh
