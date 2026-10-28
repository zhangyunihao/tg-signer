"""自动抽奖标签页：监控指定频道中的 t.me/<bot>?start=<参数> 抽奖链接并自动参与。

触发方式：任务启动时自动扫描历史消息，开启补漏后按间隔周期性重拉最近消息。
账号会话/授权检测/从 TG 更新对话列表等能力复用 random_chat 模块。
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from nicegui import ui

from tg_signer.giveaway_core import (
    DEFAULT_GLOBAL_QPS,
    DEFAULT_MAX_CONCURRENT_ACCOUNTS,
    DEFAULT_SCAN_CAP,
    JOIN_DAILY_CAP,
    JOIN_FLOOD_BUFFER,
    JOIN_HOURLY_CAP,
    JOIN_PACE_RANGE,
    RESOLVE_PACE_RANGE,
    WIN_SWEEP_LIMIT,
)
from tg_signer.giveaway_parsing import DEFAULT_KEYWORDS
from tg_signer.giveaway_state import GiveawayState
from tg_signer.locks import file_lock
from tg_signer.utils import atomic_write_json
from tg_signer.webui.activity import should_poll
from tg_signer.webui.auto_leave import create_auto_leave_task, run_auto_leave_task
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    build_debug_report,
    copy_text_to_clipboard,
    format_duration,
    mobile_card_fields,
    pretty_json,
    read_file_tail,
    with_select_all,
)
from tg_signer.webui.components import (
    LOG_AREA_CLASS,
    LogFullscreen,
    exit_fullscreen_button,
)
from tg_signer.webui.data import load_user_infos
from tg_signer.webui.giveaway_process import GiveawayProcess
from tg_signer.webui.random_chat import (
    build_account_options,
    list_session_names,
    parse_chat_id,
    schedule_auth_checks,
    update_account_chats,
)

logger = logging.getLogger(__name__)

_GLOBAL_ENGINES: Dict[str, GiveawayProcess] = {}
_GLOBAL_LOGS: Dict[str, List[str]] = {}
_GLOBAL_STATUS_CBS: Dict[str, Callable[[bool], None]] = {}
# 已删除的任务编号（按 workdir）：落盘合并时不再从磁盘捡回，
# 避免其它页面持有的旧快照把刚删除的任务复活
_GLOBAL_REMOVED: Dict[str, set] = {}

# 日志文件达到该大小后滚动为 *.1，避免超限后静默停止落盘
LOG_FILE_MAX_BYTES = 2_000_000


def append_log_file(log_file: Path, line: str) -> None:
    """追加一行日志；文件超过上限时先滚动，保证新日志始终能落盘。"""
    if log_file.exists() and log_file.stat().st_size >= LOG_FILE_MAX_BYTES:
        backup = log_file.with_name(log_file.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            log_file.replace(backup)
        except OSError:
            logger.debug("滚动抽奖日志失败", exc_info=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def stop_all_giveaway_tasks(workdir) -> int:
    """停止当前工作目录下所有运行中的抽奖子进程，返回请求停止的数量。"""
    prefix = f"{Path(workdir).resolve()}|"
    count = 0
    for key, engine in list(_GLOBAL_ENGINES.items()):
        if not key.startswith(prefix) or engine is None or not engine.running:
            continue
        try:
            engine.stop()
            count += 1
        except Exception:
            logger.debug("停止自动抽奖任务失败", exc_info=True)
    return count


_TASKS_FILE = "giveaway_tasks.json"
# 任务来源类型：channel=监控频道/群组消息，bot=直接监控抽奖机器人私聊
SOURCE_CHANNEL = "channel"
SOURCE_BOT = "bot"
SOURCE_TYPE_LABELS = {SOURCE_CHANNEL: "频道/群组", SOURCE_BOT: "百宝箱机器人"}
# 百宝箱机器人任务默认监控对象与关键词（已达标消息才可点「点击参与抽奖」）
BAIBAOXIANG_BOT = "BaibaoxiangCjBot"
BAIBAOXIANG_KEYWORD = "您已获得抽奖资格（已达标）"
BAIBAOXIANG_SCAN_LIMIT = 200
# 账号未加入监控对象且自动加入失败的提醒（持久化，加入成功后自动消除）
JOIN_ALERTS_FILE = "webui_join_alerts.json"
JOIN_ALERTS_MAX = 200
# 抽奖过程中记录的未完成口令发言任务（持久化，口令发送成功后自动消除）
SPEAK_ALERTS_FILE = "webui_speak_alerts.json"
SPEAK_ALERTS_MAX = 200
# 报名成功后仍未达标的发言数任务（持久化，消息中心提示还需发言条数）
SPEECH_ALERTS_FILE = "webui_speech_alerts.json"
SPEECH_ALERTS_MAX = 200
# 中奖提醒（bot 私聊识别到中奖消息，持久化，不清除仅限量）
WIN_ALERTS_FILE = "webui_win_alerts.json"
WIN_ALERTS_MAX = 200
# 账号已加入频道/群组达到 Telegram 上限的提醒（持久化，按账号去重）
CHANNELS_FULL_ALERTS_FILE = "webui_channels_full_alerts.json"
CHANNELS_FULL_ALERTS_MAX = 200
# 单频道扫描达到上限的提醒（持久化，按账号+频道去重，只保留最新一条）
SCAN_CAP_ALERTS_FILE = "webui_scan_cap_alerts.json"
SCAN_CAP_ALERTS_MAX = 200
# 中奖提醒全局设置（消息中心配置，所有任务共用）：转发目标 + Server酱 SendKey
WIN_SETTINGS_FILE = "webui_win_settings.json"


class _AlertStore:
    """提醒列表的 JSON 持久化（统一格式：{"alerts": [...]}，超限截断）。

    多类提醒（加入失败/未完成口令/未达标发言/中奖/频道已满）字段不同，
    但读写逻辑一致，统一收敛到这里，避免重复实现。
    """

    def __init__(self, filename: str, limit: int, label: str) -> None:
        self.filename = filename
        self.limit = limit
        self.label = label
        # mtime 指纹缓存：消息中心/未读徽标每 10s 轮询读取，避免重复读盘解析
        self._cache: Optional[Tuple[Path, Optional[Tuple[int, int]], List[dict]]] = None

    @staticmethod
    def _fingerprint(path: Path) -> Optional[Tuple[int, int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self, workdir) -> List[dict]:
        path = Path(workdir) / self.filename
        fingerprint = self._fingerprint(path)
        cached = self._cache
        if cached is not None and cached[0] == path and cached[1] == fingerprint:
            return list(cached[2])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            alerts = data.get("alerts") if isinstance(data, dict) else None
            entries = [a for a in (alerts or []) if isinstance(a, dict)]
        except FileNotFoundError:
            entries = []
        except (OSError, ValueError):
            logger.debug("读取%s文件失败", self.label, exc_info=True)
            entries = []
        self._cache = (path, fingerprint, entries)
        return list(entries)

    def save(self, workdir, alerts: List[dict]) -> None:
        # 单写者假设：提醒只由 WebUI 主进程（抽奖事件回调）追加，子进程不写，
        # 因此未加文件锁；若子进程也需要写提醒，需改成读改写 + file_lock。
        path = Path(workdir) / self.filename
        kept = alerts[: self.limit]
        try:
            atomic_write_json(path, {"alerts": kept})
        except OSError:
            logger.debug("写入%s文件失败", self.label, exc_info=True)
            return
        self._cache = (path, self._fingerprint(path), kept)


_JOIN_ALERTS = _AlertStore(JOIN_ALERTS_FILE, JOIN_ALERTS_MAX, "加入失败提醒")
_SPEAK_ALERTS = _AlertStore(SPEAK_ALERTS_FILE, SPEAK_ALERTS_MAX, "未完成发言任务")
_SPEECH_ALERTS = _AlertStore(SPEECH_ALERTS_FILE, SPEECH_ALERTS_MAX, "未达标发言数任务")
_WIN_ALERTS = _AlertStore(WIN_ALERTS_FILE, WIN_ALERTS_MAX, "中奖提醒")
_CHANNELS_FULL_ALERTS = _AlertStore(
    CHANNELS_FULL_ALERTS_FILE, CHANNELS_FULL_ALERTS_MAX, "频道已满提醒"
)
_SCAN_CAP_ALERTS = _AlertStore(
    SCAN_CAP_ALERTS_FILE, SCAN_CAP_ALERTS_MAX, "扫描上限提醒"
)


def load_join_alerts(workdir) -> List[dict]:
    return _JOIN_ALERTS.load(workdir)


def save_join_alerts(workdir, alerts: List[dict]) -> None:
    _JOIN_ALERTS.save(workdir, alerts)


def format_join_alert(a: dict, title_map: Dict[str, str]) -> str:
    chat = str(a.get("chat") or "")
    title = title_map.get(chat)
    disp = f"{title} ({chat})" if title else chat
    return (
        f"账号 {a.get('account')} 未加入监控对象「{disp}」"
        f"且自动加入失败：{a.get('error')}（{a.get('time')}）"
    )


def load_speak_alerts(workdir) -> List[dict]:
    return _SPEAK_ALERTS.load(workdir)


def save_speak_alerts(workdir, alerts: List[dict]) -> None:
    _SPEAK_ALERTS.save(workdir, alerts)


def format_speak_alert(a: dict) -> str:
    password = str(a.get("password") or "")
    pwd_disp = f"「{password}」" if password else ""
    return (
        f"账号 {a.get('account')} 口令发言未完成：群组「{a.get('group')}」"
        f"需发送口令{pwd_disp}，原因：{a.get('error')}（{a.get('time')}）"
    )


def load_speech_alerts(workdir) -> List[dict]:
    return _SPEECH_ALERTS.load(workdir)


def save_speech_alerts(workdir, alerts: List[dict]) -> None:
    _SPEECH_ALERTS.save(workdir, alerts)


def format_speech_alert(a: dict) -> str:
    title = str(a.get("title") or "")
    title_part = f"抽奖「{title}」" if title else "抽奖"
    return (
        f"账号 {a.get('account')} {title_part}发言任务未完成："
        f"群组「{a.get('group')}」还需发言 {a.get('required')} 条"
        f"（{a.get('time')}）"
    )


def load_win_alerts(workdir) -> List[dict]:
    return _WIN_ALERTS.load(workdir)


def save_win_alerts(workdir, alerts: List[dict]) -> None:
    _WIN_ALERTS.save(workdir, alerts)


def format_win_alert(a: dict) -> str:
    text = str(a.get("text") or "").replace("\n", " ")
    return f"账号 {a.get('account')} 中奖（@{a.get('bot')}）：{text}（{a.get('time')}）"


def load_channels_full_alerts(workdir) -> List[dict]:
    return _CHANNELS_FULL_ALERTS.load(workdir)


def save_channels_full_alerts(workdir, alerts: List[dict]) -> None:
    _CHANNELS_FULL_ALERTS.save(workdir, alerts)


def format_channels_full_alert(a: dict) -> str:
    detail = str(a.get("detail") or "").strip()
    where = f"（{detail}）" if detail else ""
    return (
        f"账号 {a.get('account')} 已加入的频道/群组达到 Telegram 上限{where}，"
        "已自动创建并执行退频道任务（阈值 1 天，保护关键字默认，"
        f"自动退出未命中保护字的频道）（{a.get('time')}）"
    )


def load_scan_cap_alerts(workdir) -> List[dict]:
    return _SCAN_CAP_ALERTS.load(workdir)


def save_scan_cap_alerts(workdir, alerts: List[dict]) -> None:
    _SCAN_CAP_ALERTS.save(workdir, alerts)


def format_scan_cap_alert(a: dict) -> str:
    return (
        f"账号 {a.get('account')} 扫描「{a.get('chat')}」达到单轮上限 "
        f"{a.get('cap')} 条，已停止该频道本轮扫描（{a.get('time')}）"
    )


def _alert_time() -> str:
    return f"{datetime.now():%m-%d %H:%M:%S}"


def _alert_key(alert: dict, fields: tuple[str, ...]) -> str:
    return "|".join(str(alert.get(f) or "") for f in fields)


def _drop_alert(alerts: List[dict], fields: tuple[str, ...], key: str) -> List[dict]:
    return [a for a in alerts if _alert_key(a, fields) != key]


def _notify_alert(client, text: str, level: str, debug_msg: str) -> None:
    """在 WebUI 客户端上下文弹顶部通知；浏览器已断开时静默忽略。"""
    try:
        with client:
            ui.notify(text, type=level, multi_line=True)
    except Exception:
        logger.debug(debug_msg, exc_info=True)


JOIN_ALERT_KEY = ("account", "chat")
SPEAK_ALERT_KEY = ("account", "group", "password")
SPEECH_ALERT_KEY = ("account", "bot", "group", "required", "title")
CHANNELS_FULL_ALERT_KEY = ("account",)
SCAN_CAP_ALERT_KEY = ("account", "chat")


def _handle_scan_cap_event(client, workdir, ev: dict) -> None:
    """单频道扫描达到上限：写入消息中心并弹顶部提醒（按账号+频道去重）。"""
    acct = str(ev.get("account") or "")
    chat = str(ev.get("chat") or "")
    try:
        cap = int(ev.get("cap"))
    except (TypeError, ValueError):
        return
    if not acct or not chat:
        return
    alerts = _drop_alert(
        load_scan_cap_alerts(workdir), SCAN_CAP_ALERT_KEY, f"{acct}|{chat}"
    )
    alerts.insert(
        0,
        {"account": acct, "chat": chat, "cap": cap, "time": _alert_time()},
    )
    save_scan_cap_alerts(workdir, alerts)
    _notify_alert(
        client, format_scan_cap_alert(alerts[0]), "warning", "推送扫描上限提醒失败"
    )


def _handle_channels_full_event(client, workdir, ev: dict) -> None:
    """账号频道/群组已满：写消息中心并弹顶部提醒（按账号去重，只保留最新一条）。"""
    acct = str(ev.get("account") or "")
    if not acct:
        return
    alerts = _drop_alert(
        load_channels_full_alerts(workdir), CHANNELS_FULL_ALERT_KEY, acct
    )
    alerts.insert(
        0,
        {
            "account": acct,
            "detail": str(ev.get("detail") or "")[:200],
            "time": _alert_time(),
        },
    )
    save_channels_full_alerts(workdir, alerts)
    _notify_alert(
        client, format_channels_full_alert(alerts[0]), "warning", "推送频道已满提醒失败"
    )


def _handle_win_event(client, workdir, ev: dict) -> None:
    """中奖提醒：持久化 + 顶部弹窗（好消息，绿色通知）。"""
    acct = str(ev.get("account") or "")
    bot = str(ev.get("bot") or "")
    if not acct or not bot:
        return
    alerts = load_win_alerts(workdir)
    alerts.insert(
        0,
        {
            "account": acct,
            "bot": bot,
            "text": str(ev.get("text") or "")[:300],
            "time": _alert_time(),
        },
    )
    save_win_alerts(workdir, alerts)
    _notify_alert(client, format_win_alert(alerts[0]), "positive", "推送中奖提醒失败")


def _handle_join_event(workdir, ev: dict) -> None:
    """加入失败 → 仅写入消息中心，不弹顶部通知；加入成功（join_resolved）→ 消除提醒。"""
    acct = str(ev.get("account") or "")
    chat = str(ev.get("chat") or "")
    if not acct or not chat:
        return
    failed = ev.get("type") == "join_failed"
    alerts = _drop_alert(load_join_alerts(workdir), JOIN_ALERT_KEY, f"{acct}|{chat}")
    if failed:
        alerts.insert(
            0,
            {
                "account": acct,
                "chat": chat,
                "error": str(ev.get("error") or "")[:200],
                "time": _alert_time(),
            },
        )
    save_join_alerts(workdir, alerts)


def _handle_speak_event(client, workdir, ev: dict) -> None:
    """口令发言失败 → 提醒；口令已发（speak_done）→ 消除同类提醒。"""
    acct = str(ev.get("account") or "")
    group = str(ev.get("group") or "")
    password = str(ev.get("password") or "")
    if not acct or not group:
        return
    failed = ev.get("type") == "speak_failed"
    alerts = _drop_alert(
        load_speak_alerts(workdir), SPEAK_ALERT_KEY, f"{acct}|{group}|{password}"
    )
    if failed:
        alerts.insert(
            0,
            {
                "account": acct,
                "group": group,
                "password": password,
                "error": str(ev.get("error") or "")[:200],
                "time": _alert_time(),
            },
        )
        save_speak_alerts(workdir, alerts)
        _notify_alert(
            client, format_speak_alert(alerts[0]), "warning", "推送发言任务通知失败"
        )
    else:
        save_speak_alerts(workdir, alerts)


def _handle_speech_event(client, workdir, ev: dict) -> None:
    """报名成功后仍需发言 N 条：同一抽奖/群组/要求只提醒一次。"""
    acct = str(ev.get("account") or "")
    bot = str(ev.get("bot") or "")
    group = str(ev.get("group") or "")
    title = str(ev.get("title") or "")
    try:
        required = int(ev.get("required"))
    except (TypeError, ValueError):
        return
    if not acct or not group:
        return
    key = f"{acct}|{bot}|{group}|{required}|{title}"
    alerts = load_speech_alerts(workdir)
    if any(_alert_key(a, SPEECH_ALERT_KEY) == key for a in alerts):
        return
    alerts.insert(
        0,
        {
            "account": acct,
            "bot": bot,
            "title": title,
            "group": group,
            "required": required,
            "time": _alert_time(),
        },
    )
    save_speech_alerts(workdir, alerts)
    _notify_alert(
        client, format_speech_alert(alerts[0]), "warning", "推送发言数任务通知失败"
    )


def handle_giveaway_event(client, workdir, ev: dict) -> None:
    """引擎事件分发：中奖 / 加入失败或解除 / 口令 / 发言数 / 频道已满 / 扫描上限。"""
    etype = ev.get("type")
    if etype == "win":
        _handle_win_event(client, workdir, ev)
    elif etype in ("join_failed", "join_resolved"):
        _handle_join_event(workdir, ev)
    elif etype in ("speak_failed", "speak_done"):
        _handle_speak_event(client, workdir, ev)
    elif etype == "speech_required":
        _handle_speech_event(client, workdir, ev)
    elif etype == "channels_full":
        _handle_channels_full_event(client, workdir, ev)
    elif etype == "scan_cap":
        _handle_scan_cap_event(client, workdir, ev)


def load_win_forward_targets(workdir) -> List[str]:
    """中奖消息转发目标列表（全局设置），无配置/非法值返回 ["me"]。

    兼容旧配置：单目标字符串（或逗号分隔）自动转为列表。
    """
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["me"]
    except (OSError, ValueError):
        logger.debug("读取中奖消息转发目标失败", exc_info=True)
        return ["me"]
    raw = data.get("win_forward_target") if isinstance(data, dict) else None
    if raw is None:
        return ["me"]
    if isinstance(raw, str):
        raw = [p.strip() for p in re.split(r"[,，\s]+", raw) if p.strip()]
    targets: List[str] = []
    for item in raw:
        target = str(item).strip()
        if target and target not in targets:
            targets.append(target)
    return targets or ["me"]


def _update_win_settings(workdir, **fields) -> None:
    """更新全局设置文件里的若干键（一次读盘、一次写盘）。

    多个设置项共用一个文件，若各自「整文件读 → 改一个键 → 整文件写」，
    两次保存交错就会丢掉其中一项，因此统一走这里并在文件锁内完成。
    """
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        with file_lock(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.update(fields)
            atomic_write_json(path, data)
    except (OSError, ValueError, TimeoutError):
        logger.debug("写入中奖提醒全局设置失败", exc_info=True)


def save_win_forward_targets(workdir, targets) -> None:
    """保存中奖消息转发目标，保留文件中其他键（回扫条数等）。"""
    cleaned: List[str] = []
    for item in targets or []:
        target = str(item).strip()
        if target and target not in cleaned:
            cleaned.append(target)
    _update_win_settings(workdir, win_forward_target=cleaned or ["me"])


def load_win_sweep_limit(workdir) -> int:
    """中奖回扫条数（全局设置），无配置/非法值回退默认 WIN_SWEEP_LIMIT。"""
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        logger.debug("读取中奖回扫条数失败", exc_info=True)
        return WIN_SWEEP_LIMIT
    try:
        limit = int(data.get("win_sweep_limit"))  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return WIN_SWEEP_LIMIT
    return limit if limit > 0 else WIN_SWEEP_LIMIT


def save_win_sweep_limit(workdir, limit) -> None:
    """保存中奖回扫条数，保留文件中其他键（转发目标等）。"""
    try:
        value = max(1, int(limit))
    except (TypeError, ValueError):
        logger.debug("中奖回扫条数非法，忽略写入: %r", limit)
        return
    _update_win_settings(workdir, win_sweep_limit=value)


def _split_keywords(raw) -> List[str]:
    """逗号（中英文）、顿号、空白分隔的关键字串 → 去重列表。"""
    if isinstance(raw, str):
        parts = re.split(r"[,，、\s]+", raw)
    else:
        parts = list(raw or [])
    cleaned: List[str] = []
    for item in parts:
        kw = str(item).strip()
        if kw and kw not in cleaned:
            cleaned.append(kw)
    return cleaned


def load_win_skip_keywords(workdir) -> List[str]:
    """中奖消息跳过关键字（全局设置）：命中任一即不提醒、不转发，默认空。"""
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.debug("读取中奖消息跳过关键字失败", exc_info=True)
        return []
    raw = data.get("win_skip_keywords") if isinstance(data, dict) else None
    return _split_keywords(raw)


def save_win_skip_keywords(workdir, keywords) -> None:
    """保存中奖消息跳过关键字（支持逗号分隔字符串或列表），保留其他键。"""
    _update_win_settings(workdir, win_skip_keywords=_split_keywords(keywords))


# 抽奖任务超时默认值（分钟）：单独运行与组合子任务共用，0=不限
GIVEAWAY_TIMEOUT_MIN = 10


def load_giveaway_timeout_min(workdir) -> int:
    """抽奖任务超时（分钟，全局设置）：单独运行与组合子任务共用，0=不限。"""
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return GIVEAWAY_TIMEOUT_MIN
    try:
        val = int(data.get("giveaway_timeout_min"))  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return GIVEAWAY_TIMEOUT_MIN
    return max(0, val)


def save_giveaway_timeout_min(workdir, val) -> None:
    """保存抽奖任务超时（分钟），保留文件中其他键。"""
    try:
        value = max(0, int(val))
    except (TypeError, ValueError):
        logger.debug("抽奖任务超时非法，忽略写入: %r", val)
        return
    _update_win_settings(workdir, giveaway_timeout_min=value)


def load_giveaway_scan_cap(workdir) -> int:
    """频道/群组单轮单频道扫描上限（全局设置），默认 DEFAULT_SCAN_CAP。"""
    path = Path(workdir) / WIN_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return DEFAULT_SCAN_CAP
    try:
        val = int(data.get("giveaway_scan_cap"))  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return DEFAULT_SCAN_CAP
    return val if val > 0 else DEFAULT_SCAN_CAP


def save_giveaway_scan_cap(workdir, val) -> None:
    """保存单频道扫描上限（条），保留文件中其他键。"""
    try:
        value = max(1, int(val))
    except (TypeError, ValueError):
        logger.debug("单频道扫描上限非法，忽略写入: %r", val)
        return
    _update_win_settings(workdir, giveaway_scan_cap=value)


# 任务配置字段 -> 默认值，加载旧配置时补齐缺失字段
_CFG_FIELDS = {
    "accounts": list,
    "chats": list,
    "session_dir": str,
    # 任务来源类型：channel=频道/群组，bot=抽奖机器人私聊
    "source_type": lambda: SOURCE_CHANNEL,
    "bot_username": lambda: "",
    "scan_limit": lambda: None,
    "keywords": lambda: list(DEFAULT_KEYWORDS),
    "auto_scan": lambda: True,
    "auto_join": lambda: True,
    "ignore_history": lambda: True,  # 允许重复参与（忽略历史成功记录）
    "delay_min": lambda: 3,
    "delay_max": lambda: 10,
    "confirm_wait": lambda: 8,
    "max_joins": lambda: None,
    "rescue_interval_min": lambda: 0,  # 补漏扫描间隔（分钟），0=关闭（默认关闭）
    "rescue_limit": lambda: 10,  # 每次补漏每频道重拉条数
    "win_detect": lambda: True,  # 中奖识别：bot 私聊命中关键词时转发提醒
    "pinned": lambda: False,  # 列表置顶：仅影响任务展示顺序
    # —— 限流保护（任务级）——
    "join_pace_min": lambda: int(JOIN_PACE_RANGE[0]),  # 相邻两次加入最小间隔（秒）
    "join_pace_max": lambda: int(JOIN_PACE_RANGE[1]),
    "join_hourly_cap": lambda: JOIN_HOURLY_CAP,  # 单账号滚动 1 小时加入上限
    "join_daily_cap": lambda: JOIN_DAILY_CAP,  # 单账号滚动 24 小时加入上限
    "join_flood_buffer": lambda: JOIN_FLOOD_BUFFER,  # FLOOD_WAIT 额外缓冲（秒）
    "resolve_pace_min": lambda: int(RESOLVE_PACE_RANGE[0]),  # 解析类调用最小间隔
    "resolve_pace_max": lambda: int(RESOLVE_PACE_RANGE[1]),
    # 全局调用节奏（同进程跨账号共享）：0=不限制
    "global_qps": lambda: int(DEFAULT_GLOBAL_QPS),  # 所有账号合计 API 次/秒
    "max_concurrent_accounts": lambda: DEFAULT_MAX_CONCURRENT_ACCOUNTS,
    "last_joined": lambda: 0,  # 上次运行新参与抽奖总数
    "last_joined_by_account": dict,  # 上次运行各账号新参与数 {account: count}
    "last_joined_by_account_chat": dict,  # 上次运行各账号按来源频道/群组分布 {account: {chat: count}}
}


def list_known_channels(workdir) -> Dict[str, str]:
    """汇总 users/*/latest_chats.json 中的频道与群组，返回 {chat_id: "标题 (id)"}"""
    options: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            ctype = str(chat.get("type") or "").lower().replace("chattype.", "")
            if ctype != "channel" and not ctype.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or "N/A"
            options[str(cid)] = f"{title} ({cid})"
    return options


def chat_kind_map(workdir) -> Dict[str, str]:
    """返回 {chat_id: "channel"|"group"}，供监控对象类型筛选"""
    mapping: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            ctype = str(chat.get("type") or "").lower().replace("chattype.", "")
            if ctype != "channel" and not ctype.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            kind = "channel" if ctype == "channel" else "group"
            mapping.setdefault(str(cid), kind)
    return mapping


def channel_title_map(workdir) -> Dict[str, str]:
    """返回 {chat_id: 频道名}，供任务表格展示"""
    mapping: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or ""
            mapping.setdefault(str(cid), title)
    return mapping


def _split_items(raw: str) -> List[str]:
    """把逗号/空格/顿号/换行分隔的输入拆成列表"""
    return [x for x in re.split(r"[\s,，、;；]+", raw or "") if x.strip()]


def _store_path(workdir) -> Path:
    return Path(workdir) / _TASKS_FILE


def load_task_store(workdir) -> dict:
    """读取持久化任务配置：{"seq": N, "tasks": {id: cfg}}，并补齐缺失字段"""
    path = _store_path(workdir)
    if not path.is_file():
        return {"seq": 1, "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"seq": 1, "tasks": {}}
    if not isinstance(data, dict):
        return {"seq": 1, "tasks": {}}
    data.setdefault(
        "seq",
        max((int(k) for k in data.get("tasks", {}) if str(k).isdigit()), default=0) + 1,
    )
    data.setdefault("tasks", {})
    for cfg in data["tasks"].values():
        if not isinstance(cfg, dict):
            continue
        for field, default in _CFG_FIELDS.items():
            if field not in cfg or cfg[field] is None:
                cfg[field] = default()
    return data


def save_task_store(workdir, store: dict, removed=None) -> None:
    """保存任务配置：文件锁内「读 → 合并 → 写」。

    每个页面（桌面/手机）与每个 WebUI 实例各持一份内存快照，若直接整文件覆盖，
    后保存的旧快照会把其它页面刚创建、尚未执行的任务冲掉（表现为刷新后任务消失）。
    因此磁盘上多出来的任务一律保留（``removed`` 里的是本页刚删掉的，不再保留），
    序号取两者较大值。
    """
    path = _store_path(workdir)
    clean = dict(store.get("tasks") or {})
    removed = {str(n) for n in (removed or ())}
    try:
        with file_lock(path):
            disk = load_task_store(workdir)
            for name, cfg in (disk.get("tasks") or {}).items():
                if str(name) in removed:
                    continue
                clean.setdefault(name, cfg)
            seq = max(int(store.get("seq") or 1), int(disk.get("seq") or 1))
            atomic_write_json(path, {"seq": seq, "tasks": clean})
    except (TimeoutError, OSError):
        logger.warning("更新抽奖任务配置失败，本次修改未保存: %s", path)
        return
    store["seq"] = seq


def add_task_to_store(workdir, cfg: dict) -> str:
    """文件锁内按磁盘序号分配任务编号并写入配置，返回编号。

    编号必须取磁盘最新值：两个页面/实例同时创建时若各自用内存里的序号，
    会生成同名任务互相覆盖。
    """
    path = _store_path(workdir)
    with file_lock(path):
        disk = load_task_store(workdir)
        seq = int(disk.get("seq") or 1)
        name = str(seq)
        disk["tasks"][name] = cfg
        disk["seq"] = seq + 1
        atomic_write_json(path, disk)
    return name


def giveaway_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建“自动抽奖”标签页内容，返回刷新函数以纳入全局刷新。"""
    wd_key = str(Path(workdir).resolve())
    store = load_task_store(workdir)
    tasks: Dict[str, dict] = {
        str(k): v for k, v in store["tasks"].items() if isinstance(v, dict)
    }
    chat_options_map: Dict[str, str] = {}
    chat_title_map: Dict[str, str] = {}
    kind_map: Dict[str, str] = {}
    table_ref: Dict[str, object] = {"table": None}
    # 移动端卡片「执行时间」元素：运行中按启动时间每秒刷新耗时
    card_time_els: Dict[str, ui.html] = {}
    card_time_texts: Dict[str, str] = {}

    def _persist() -> None:
        clean = {}
        for name, cfg in tasks.items():
            source_type = str(cfg.get("source_type") or SOURCE_CHANNEL)
            bot_username = str(cfg.get("bot_username") or "").lstrip("@").strip()
            chats = (
                [bot_username]
                if source_type == SOURCE_BOT and bot_username
                else [str(c) for c in (cfg.get("chats") or [])]
            )
            clean[name] = {
                "accounts": list(cfg.get("accounts") or []),
                "chats": chats,
                "session_dir": str(cfg.get("session_dir") or "."),
                "source_type": source_type,
                "bot_username": bot_username,
                "scan_limit": (
                    max(1, int(cfg.get("scan_limit") or 0))
                    if cfg.get("scan_limit")
                    else None
                ),
                "keywords": list(cfg.get("keywords") or []),
                "auto_scan": bool(cfg.get("auto_scan", True)),
                "auto_join": bool(cfg.get("auto_join", True)),
                "ignore_history": bool(cfg.get("ignore_history", True)),
                "delay_min": int(cfg.get("delay_min") or 0),
                "delay_max": int(cfg.get("delay_max") or 0),
                "confirm_wait": int(cfg.get("confirm_wait") or 0),
                "max_joins": (int(cfg["max_joins"]) if cfg.get("max_joins") else None),
                "rescue_interval_min": max(0, int(cfg.get("rescue_interval_min") or 0)),
                "rescue_limit": max(1, int(cfg.get("rescue_limit") or 10)),
                # 中奖识别配置（转发目标/Server酱为消息中心全局设置）
                "win_detect": bool(cfg.get("win_detect", True)),
                # 列表置顶（仅影响任务展示顺序）
                "pinned": bool(cfg.get("pinned")),
                # 限流保护：加入/解析节奏与配额（0=不限制，缺省用常量默认值）
                "join_pace_min": max(0, int(cfg.get("join_pace_min") or 0)),
                "join_pace_max": max(0, int(cfg.get("join_pace_max") or 0)),
                "join_hourly_cap": max(0, int(cfg.get("join_hourly_cap") or 0)),
                "join_daily_cap": max(0, int(cfg.get("join_daily_cap") or 0)),
                "join_flood_buffer": max(0, int(cfg.get("join_flood_buffer") or 0)),
                "resolve_pace_min": max(0, int(cfg.get("resolve_pace_min") or 0)),
                "resolve_pace_max": max(0, int(cfg.get("resolve_pace_max") or 0)),
                # 全局调用节奏（跨账号共享，0=不限制）
                "global_qps": max(0, int(cfg.get("global_qps") or 0)),
                "max_concurrent_accounts": max(
                    0, int(cfg.get("max_concurrent_accounts") or 0)
                ),
                # 上次执行的开始时间与时长（运行结束或停止时写入时长）
                "last_start_time": str(cfg.get("last_start_time") or ""),
                "last_duration": str(cfg.get("last_duration") or ""),
                # 上次运行新参与抽奖总数与各账号分布
                "last_joined": int(cfg.get("last_joined") or 0),
                "last_joined_by_account": {
                    str(k): int(v)
                    for k, v in (cfg.get("last_joined_by_account") or {}).items()
                },
                "last_joined_by_account_chat": {
                    str(k): {str(ck): int(cv) for ck, cv in (v or {}).items()}
                    for k, v in (cfg.get("last_joined_by_account_chat") or {}).items()
                },
                # 上次中奖回扫进度（结束后继续展示）
                "last_sweep": str(cfg.get("last_sweep") or ""),
            }
        save_task_store(
            workdir,
            {"seq": store["seq"], "tasks": clean},
            _GLOBAL_REMOVED.get(wd_key, set()),
        )

    def engine_key(name: str) -> str:
        return f"{wd_key}|{name}"

    def get_engine(name: str) -> Optional[GiveawayProcess]:
        return _GLOBAL_ENGINES.get(engine_key(name))

    def capture_engine_stats(name: str) -> bool:
        """把当前运行统计写回任务配置（不落盘），供运行中/停止后展示与持久化。"""
        cfg = tasks.get(name)
        eng = get_engine(name)
        if cfg is None or eng is None:
            return False
        cfg["last_joined"] = int(eng.joined)
        cfg["last_joined_by_account"] = dict(eng.joined_by_account)
        cfg["last_joined_by_account_chat"] = {
            k: dict(v) for k, v in eng.joined_by_account_chat.items()
        }
        cfg["last_sweep"] = str(getattr(eng, "win_sweep_text", "") or "")
        return True

    def ensure_engine(name: str) -> GiveawayProcess:
        engine = get_engine(name)
        if engine is None:
            engine = GiveawayProcess(name, workdir)
            _GLOBAL_ENGINES[engine_key(name)] = engine
        return engine

    def chat_display(cid) -> str:
        title = chat_title_map.get(str(cid))
        # 监控对象只展示标题，不附带会话 id，避免移动端卡片被长 id 挤占
        return title or str(cid)

    def make_log(name: str) -> Callable[[str], None]:
        log_file = Path(workdir) / "logs" / "giveaway.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        def _log(msg: str) -> None:
            buf = _GLOBAL_LOGS.setdefault(wd_key, [])
            # 子进程日志已带「[#任务] HH:MM:SS」前缀且已自行落盘，直接沿用
            from_child = bool(re.match(r"^\[#[^\]]+\] \d{2}:\d{2}:\d{2} ", msg))
            if from_child:
                line = msg
            else:
                # 消息已带时间戳（如「已参与」行）则不重复加
                ts = f"{datetime.now():%H:%M:%S}"
                if not re.match(r"^\d{2}:\d{2}:\d{2}", msg):
                    msg = f"{ts} {msg}"
                line = f"[#{name}] {msg}"
            buf.append(line)
            if len(buf) > 1000:
                del buf[:-1000]
            if from_child:
                return
            # 同时落盘，便于服务重启后仍可追溯
            try:
                append_log_file(log_file, line)
            except OSError:
                logger.debug("写入抽奖日志文件失败", exc_info=True)

        return _log

    def _set_account_options() -> None:
        session_dir = Path(default_session_dir or ".")
        names = list_session_names(session_dir, workdir)
        account_select.options = with_select_all(
            build_account_options(session_dir, names)
        )
        account_select.update()

    def apply_channel_filter() -> None:
        """按类型筛选监控对象下拉选项（全部/仅频道/仅群组），不影响已选值"""
        try:
            mode = channel_type_toggle.value or "all"
        except NameError:
            return
        if mode == "all":
            filtered = dict(chat_options_map)
        else:
            filtered = {
                cid: label
                for cid, label in chat_options_map.items()
                if kind_map.get(cid) == mode
            }
        channel_select.options = filtered
        channel_select.update()

    def refresh_options() -> None:
        session_dir = Path(default_session_dir or ".")
        _set_account_options()
        names = list_session_names(session_dir, workdir)
        schedule_auth_checks(session_dir, names, on_done=_set_account_options)
        chat_options_map.clear()
        chat_options_map.update(list_known_channels(workdir))
        chat_title_map.clear()
        chat_title_map.update(channel_title_map(workdir))
        kind_map.clear()
        kind_map.update(chat_kind_map(workdir))
        apply_channel_filter()

    def task_running(name: str) -> bool:
        engine = get_engine(name)
        return engine is not None and engine.running

    def _status_text(name: str) -> str:
        engine = get_engine(name)
        if engine is None or not engine.running:
            return "未运行"
        return engine.progress_text() or "运行中"

    def _sweep_text(name: str) -> str:
        """中奖消息回扫进度（与频道扫描分开展示），结束后展示上次完成进度。"""
        engine = get_engine(name)
        if engine is not None and engine.running:
            return (getattr(engine, "win_sweep_text", "") or "").strip()
        return str((tasks.get(name) or {}).get("last_sweep") or "").strip()

    def joined_count(name: str) -> int:
        """本次运行（运行中实时）或上次运行（结束后持久化）新参与抽奖总数"""
        engine = get_engine(name)
        if engine is not None and engine.running:
            return int(engine.joined)
        return int((tasks.get(name) or {}).get("last_joined") or 0)

    def joined_by_account(name: str) -> Dict[str, int]:
        """各账号新参与数：运行中取引擎实时值，结束后取持久化值"""
        engine = get_engine(name)
        if engine is not None and engine.running:
            return {str(k): int(v) for k, v in engine.joined_by_account.items()}
        raw = (tasks.get(name) or {}).get("last_joined_by_account") or {}
        return {str(k): int(v) for k, v in raw.items()}

    def joined_by_account_chat(name: str) -> Dict[str, Dict[str, int]]:
        """各账号按来源频道/群组的新参与数：运行中取实时值，结束后取持久化值"""
        engine = get_engine(name)
        if engine is not None and engine.running:
            return {str(k): dict(v) for k, v in engine.joined_by_account_chat.items()}
        raw = (tasks.get(name) or {}).get("last_joined_by_account_chat") or {}
        return {str(k): dict(v) for k, v in raw.items()}

    def remove_task(name: str) -> None:
        if task_running(name):
            ui.notify(f"任务 #{name} 正在运行，请先停止再删除", type="warning")
            return
        tasks.pop(name, None)
        _GLOBAL_REMOVED.setdefault(wd_key, set()).add(str(name))
        _GLOBAL_ENGINES.pop(engine_key(name), None)
        _GLOBAL_STATUS_CBS.pop(engine_key(name), None)
        _persist()
        render_tasks()

    def toggle_pin(name: str) -> None:
        """切换任务置顶：置顶任务在任务列表中排到最前（不影响运行状态）。"""
        cfg = tasks.get(name)
        if cfg is None:
            return
        cfg["pinned"] = not bool(cfg.get("pinned"))
        _persist()
        render_tasks()
        ui.notify(
            f"任务 #{name} 已{'置顶' if cfg['pinned'] else '取消置顶'}",
            type="positive",
        )

    def duplicate_task(name: str) -> None:
        cfg = tasks[name]
        source_type = str(cfg.get("source_type") or SOURCE_CHANNEL)
        account_select.value = list(cfg["accounts"])
        refresh_options()
        source_type_toggle.value = source_type
        bot_username_input.value = cfg.get("bot_username") or BAIBAOXIANG_BOT
        bot_scan_limit_input.value = cfg.get("scan_limit") or BAIBAOXIANG_SCAN_LIMIT
        channel_select.value = (
            [] if source_type == SOURCE_BOT else [str(c) for c in cfg["chats"]]
        )
        manual_chats_input.value = ""
        keywords_input.value = "，".join(cfg.get("keywords") or [])
        max_joins_input.value = cfg.get("max_joins") or None
        auto_scan_switch.value = cfg["auto_scan"]
        auto_join_switch.value = cfg.get("auto_join", True)
        ignore_hist_switch.value = bool(cfg.get("ignore_history", True))
        delay_min_input.value = cfg["delay_min"]
        delay_max_input.value = cfg["delay_max"]
        rescue_interval_input.value = cfg.get("rescue_interval_min") or 30
        rescue_limit_input.value = cfg.get("rescue_limit") or 10
        rescue_switch.value = bool(cfg.get("rescue_interval_min"))
        win_switch.value = bool(cfg.get("win_detect", True))
        join_pace_min_input.value = cfg.get("join_pace_min")
        join_pace_max_input.value = cfg.get("join_pace_max")
        join_hourly_cap_input.value = cfg.get("join_hourly_cap")
        join_daily_cap_input.value = cfg.get("join_daily_cap")
        join_flood_buffer_input.value = cfg.get("join_flood_buffer")
        resolve_pace_min_input.value = cfg.get("resolve_pace_min")
        resolve_pace_max_input.value = cfg.get("resolve_pace_max")
        global_qps_input.value = cfg.get("global_qps")
        max_concurrent_accounts_input.value = cfg.get("max_concurrent_accounts")
        _apply_rescue_visibility()
        _apply_source_visibility()
        for el in (
            account_select,
            source_type_toggle,
            bot_username_input,
            bot_scan_limit_input,
            channel_select,
            manual_chats_input,
            keywords_input,
            max_joins_input,
            auto_scan_switch,
            auto_join_switch,
            ignore_hist_switch,
            delay_min_input,
            delay_max_input,
            rescue_interval_input,
            rescue_limit_input,
            win_switch,
            join_pace_min_input,
            join_pace_max_input,
            join_hourly_cap_input,
            join_daily_cap_input,
            join_flood_buffer_input,
            resolve_pace_min_input,
            resolve_pace_max_input,
            global_qps_input,
            max_concurrent_accounts_input,
        ):
            el.update()
        ui.notify("已复制任务参数，可修改后点击「确定添加」", type="positive")
        add_dlg.open()

    async def start_task(name: str, set_status: Callable[[bool], None]) -> None:
        async def _auto_stop_later(_n: str, _mins: int, _ts: float) -> None:
            """最长运行时长到点自动停止；手动停止或重启后旧定时器失效。"""
            await asyncio.sleep(_mins * 60)
            cfg_late = tasks.get(_n)
            if cfg_late is None or cfg_late.get("_start_ts") != _ts:
                return
            eng = get_engine(_n)
            if eng is not None and eng.running:
                if capture_engine_stats(_n):
                    _persist()
                try:
                    eng.stop()
                except Exception:
                    logger.debug("任务运行超时自动停止失败", exc_info=True)
                ui.notify(
                    f"任务 #{_n} 已达最长运行时长 {_mins} 分钟，自动停止",
                    type="warning",
                )

        cfg = tasks[name]
        for other, ocfg in tasks.items():
            if (
                other != name
                and task_running(other)
                and set(cfg["accounts"]) & set(ocfg["accounts"])
            ):
                ui.notify(
                    f"账号已被运行中的任务 #{other} 占用（同一账号不能并行建立连接），"
                    "请先停止该任务",
                    type="warning",
                )
                return

        # 运行中每次统计变化都写回配置并定期落盘，停止/重启后明细仍可查
        last_saved = {"joined": -1, "ts": 0.0}
        # 卡片状态刷新的去重标记：进度没变就不重绘，避免每秒无谓更新
        last_status = {"sig": None}

        def _on_stats(payload: dict, _n: str = name) -> None:
            cfg_now = tasks.get(_n)
            if cfg_now is None:
                return
            joined = int(payload.get("joined") or 0)
            cfg_now["last_joined"] = joined
            cfg_now["last_joined_by_account"] = {
                str(k): int(v) for k, v in (payload.get("by_account") or {}).items()
            }
            cfg_now["last_joined_by_account_chat"] = {
                str(k): {str(c): int(n) for c, n in (v or {}).items()}
                for k, v in (payload.get("by_chat") or {}).items()
            }
            # 每秒刷新卡片/表格里的运行状态与扫描进度（如「扫描中 (3/12)」），
            # 中奖消息回扫进度单独刷新
            sig = (
                joined,
                str(payload.get("progress") or ""),
                str(payload.get("sweep") or ""),
            )
            if sig != last_status["sig"] and task_running(_n):
                last_status["sig"] = sig
                cb_now = _GLOBAL_STATUS_CBS.get(engine_key(_n))
                if cb_now is not None:
                    _apply_status(cb_now, True)
            now = time.monotonic()
            if joined == last_saved["joined"] and now - last_saved["ts"] < 30:
                return
            last_saved["joined"] = joined
            last_saved["ts"] = now
            _persist()

        def _on_finish(_n: str = name) -> None:
            # 运行结束/停止：按启动时间计算执行时长并持久化，
            # 同时保存本次新参与抽奖总数与各账号分布，供表格与明细展示
            cfg_done = tasks.get(_n)
            if cfg_done is not None:
                if cfg_done.get("_start_ts"):
                    cfg_done["last_duration"] = format_duration(
                        time.time() - cfg_done["_start_ts"]
                    )
                capture_engine_stats(_n)
                _persist()
            cb = _GLOBAL_STATUS_CBS.get(engine_key(_n))
            if cb is not None:
                _apply_status(cb, False)

        def _account_busy(acct: str) -> bool:
            """该账号是否仍被其他运行中的抽奖任务占用（不能并行建立连接）。"""
            for other, ocfg in tasks.items():
                if other == name or acct not in (ocfg.get("accounts") or []):
                    continue
                eng = get_engine(other)
                if eng is not None and eng.running:
                    return True
            return False

        def _start_channels_full_leave(ev: dict) -> None:
            """频道/群组已满：自动建退频道任务（阈值 1 天）并后台执行。"""
            acct = str(ev.get("account") or "").strip()
            if not acct:
                return
            busy = _account_busy(acct)
            task = create_auto_leave_task(
                workdir,
                acct,
                detail=str(ev.get("detail") or ""),
                status="排队等待账号空闲" if busy else "待执行",
            )
            if task is None:
                return
            session_dir = Path(
                str(ev.get("session_dir") or cfg.get("session_dir") or ".")
            )

            async def _run() -> None:
                await run_auto_leave_task(
                    workdir,
                    session_dir,
                    task["id"],
                    wait_busy=lambda: _account_busy(acct),
                    log=make_log("自动退频道"),
                )

            asyncio.create_task(_run())

        def _on_event(ev: dict) -> None:
            """引擎事件 → 顶部通知/消息中心 + 持久化（按事件类型分发，见 handle_giveaway_event）"""
            handle_giveaway_event(client, workdir, ev)
            if ev.get("type") == "channels_full":
                _start_channels_full_leave(ev)

        engine = ensure_engine(name)
        start_ts = time.time()
        # 引擎在独立子进程运行：WebUI 不再承载长时间扫描/并发账号的 CPU 与内存
        started = engine.start(
            cfg,
            log=make_log(name),
            on_event=_on_event,
            on_finish=_on_finish,
            on_stats=_on_stats,
        )
        if started:
            cfg["last_start_time"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            cfg["last_duration"] = ""
            cfg["_start_ts"] = start_ts
            _persist()
            set_status(True)
            ui.notify(f"任务 #{name} 已启动", type="positive")
            # 超时自动停止：全局设置「抽奖任务超时」（分钟，0=不限）
            runtime_min = load_giveaway_timeout_min(workdir)
            if runtime_min > 0:
                asyncio.create_task(_auto_stop_later(name, runtime_min, start_ts))
        else:
            ui.notify(f"任务 #{name} 正在运行或收尾中，请稍后再试", type="warning")

    async def stop_task(name: str, set_status: Callable[[bool], None]) -> None:
        engine = get_engine(name)
        if engine is not None and engine.running:
            # 停止前先把当前统计落盘，避免明细只存在于内存
            if capture_engine_stats(name):
                _persist()
            engine.stop()
        else:
            set_status(False)

    def _apply_status(set_status: Callable[[bool], None], running: bool) -> None:
        try:
            set_status(running)
        except Exception:
            logger.debug("更新任务运行状态回调执行失败", exc_info=True)

    def on_add_task() -> None:
        accounts = list(account_select.value or [])
        # 清理历史标签后缀
        accounts = [str(a).replace(" ⚠️未授权", "") for a in accounts]
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        source_type = str(source_type_toggle.value or SOURCE_CHANNEL)
        bot_username = str(bot_username_input.value or "").lstrip("@").strip()
        chats: List[str] = []
        if source_type == SOURCE_BOT:
            if not bot_username:
                ui.notify("请填写要监控的机器人用户名", type="warning")
                return
            chats = [bot_username]
        else:
            for cid in list(channel_select.value or []):
                chats.append(str(cid))
            for raw in _split_items(manual_chats_input.value or ""):
                parsed = parse_chat_id(raw)
                if parsed is not None and str(parsed) not in chats:
                    chats.append(str(parsed))
            if not chats:
                ui.notify("请至少选择或手动填写一个监控对象", type="warning")
                return
        keywords = _split_items(keywords_input.value or "")
        try:
            max_joins = (
                max(0, int(max_joins_input.value))
                if max_joins_input.value is not None
                else None
            )
            delay_min = max(0, int(delay_min_input.value or 0))
            delay_max = max(0, int(delay_max_input.value or 0))
            rescue_interval_min = (
                max(0, int(rescue_interval_input.value or 0))
                if rescue_switch.value
                else 0
            )
            rescue_limit = max(1, int(rescue_limit_input.value or 10))
            join_pace_min = max(0, int(join_pace_min_input.value or 0))
            join_pace_max = max(0, int(join_pace_max_input.value or 0))
            join_hourly_cap = max(0, int(join_hourly_cap_input.value or 0))
            join_daily_cap = max(0, int(join_daily_cap_input.value or 0))
            join_flood_buffer = max(0, int(join_flood_buffer_input.value or 0))
            resolve_pace_min = max(0, int(resolve_pace_min_input.value or 0))
            resolve_pace_max = max(0, int(resolve_pace_max_input.value or 0))
            global_qps = max(0, int(global_qps_input.value or 0))
            max_concurrent_accounts = max(
                0, int(max_concurrent_accounts_input.value or 0)
            )
            scan_limit = (
                max(1, int(bot_scan_limit_input.value or BAIBAOXIANG_SCAN_LIMIT))
                if source_type == SOURCE_BOT
                else None
            )
        except (TypeError, ValueError):
            ui.notify("参与上限/延迟/补漏/限流参数需为数字", type="warning")
            return
        if delay_min > delay_max:
            delay_min, delay_max = delay_max, delay_min
        if join_pace_min > join_pace_max:
            join_pace_min, join_pace_max = join_pace_max, join_pace_min
        if resolve_pace_min > resolve_pace_max:
            resolve_pace_min, resolve_pace_max = resolve_pace_max, resolve_pace_min
        if source_type == SOURCE_BOT:
            # 机器人任务只处理已达标消息，自动加入无意义且可能误加推广目标
            if not keywords:
                keywords = [BAIBAOXIANG_KEYWORD]
            auto_join = False
        else:
            auto_join = bool(auto_join_switch.value)
        cfg = {
            "accounts": accounts,
            "chats": chats,
            "session_dir": str(Path(default_session_dir or ".")),
            "source_type": source_type,
            "bot_username": bot_username if source_type == SOURCE_BOT else "",
            "scan_limit": scan_limit,
            "keywords": keywords,
            "max_joins": max_joins,
            "auto_scan": bool(auto_scan_switch.value),
            "auto_join": auto_join,
            "ignore_history": bool(ignore_hist_switch.value),
            "delay_min": delay_min,
            "delay_max": delay_max,
            "confirm_wait": 8,
            "rescue_interval_min": rescue_interval_min,
            "rescue_limit": rescue_limit,
            "win_detect": bool(win_switch.value),
            "pinned": False,
            "join_pace_min": join_pace_min,
            "join_pace_max": join_pace_max,
            "join_hourly_cap": join_hourly_cap,
            "join_daily_cap": join_daily_cap,
            "join_flood_buffer": join_flood_buffer,
            "resolve_pace_min": resolve_pace_min,
            "resolve_pace_max": resolve_pace_max,
            "global_qps": global_qps,
            "max_concurrent_accounts": max_concurrent_accounts,
        }
        # 编号以磁盘顺序为准并在文件锁内落盘：其它页面/实例的旧快照不会冲掉新任务
        try:
            name = add_task_to_store(workdir, cfg)
        except (TimeoutError, OSError):
            ui.notify("保存任务配置失败，请稍后重试", type="warning")
            return
        store["seq"] = max(int(store.get("seq") or 1), int(name) + 1)
        tasks[name] = cfg
        ensure_engine(name)
        render_tasks()
        add_dlg.close()
        ui.notify(f"任务 #{name} 已添加，正在启动...", type="positive")
        asyncio.create_task(start_task(name, tasks[name]["_set_status"]))

    def _card_time_html(text: str) -> str:
        return (
            '<div class="tgs-fields">'
            '<div class="tgs-field tgs-field-full">'
            '<span class="tgs-field-k">执行时间</span>'
            f'<span class="tgs-field-v">{escape(str(text))}</span>'
            "</div></div>"
        )

    def _card_time_text(name: str, running: bool) -> str:
        """卡片「执行时间」文案：运行中实时计算耗时，结束后用持久化时长。"""
        cfg = tasks.get(name) or {}
        start = str(cfg.get("last_start_time") or "") or "—"
        if running:
            start_ts = cfg.get("_start_ts")
            dur = format_duration(time.time() - start_ts) if start_ts else ""
        else:
            dur = str(cfg.get("last_duration") or "")
        return f"{start}（耗时 {dur}）" if dur and dur != "—" else start

    def _update_card_time(name: str, text: str) -> None:
        el = card_time_els.get(name)
        if el is None or card_time_texts.get(name) == text:
            return
        card_time_texts[name] = text
        try:
            el.content = _card_time_html(text)
        except Exception:
            logger.debug("刷新卡片执行时间失败", exc_info=True)

    def _list_text(items: List[str], limit: Optional[int] = None) -> str:
        """移动端展示用：数量 + 前 limit 项（None=全部），超出以省略号结尾。"""
        items = [str(i) for i in items if str(i)]
        if not items:
            return "—"
        shown = items if limit is None else items[:limit]
        more = "…" if len(shown) < len(items) else ""
        return f"{len(items)} 个：" + "、".join(shown) + more

    def render_tasks() -> None:
        task_list_container.clear()
        table_ref["table"] = None
        card_time_els.clear()
        card_time_texts.clear()
        with task_list_container:
            if not tasks:
                ui.label("暂无任务，请点击「添加任务」创建。").classes(
                    "text-sm text-gray-400"
                )
                return
            columns = [
                {"name": "name", "label": "任务", "field": "name", "align": "left"},
                {
                    "name": "task_type",
                    "label": "类型",
                    "field": "task_type",
                    "align": "left",
                },
                {
                    "name": "accounts",
                    "label": "账号",
                    "field": "accounts",
                    "align": "left",
                },
                {
                    "name": "chats",
                    "label": "监控对象",
                    "field": "chats",
                    "align": "left",
                },
                {
                    "name": "keywords",
                    "label": "关键词",
                    "field": "keywords",
                    "align": "left",
                },
                {
                    "name": "scan",
                    "label": "参与上限",
                    "field": "scan",
                    "align": "left",
                },
                {
                    "name": "ignore_hist",
                    "label": "允许重复参与",
                    "field": "ignore_hist",
                    "align": "left",
                },
                {
                    "name": "joined",
                    "label": "新参加",
                    "field": "joined",
                    "align": "left",
                },
                {
                    "name": "last_time",
                    "label": "执行时间",
                    "field": "last_time",
                    "align": "left",
                },
                {
                    "name": "duration",
                    "label": "执行时长",
                    "field": "duration",
                    "align": "left",
                },
                {
                    "name": "sweep",
                    "label": "中奖回扫",
                    "field": "sweep",
                    "align": "left",
                },
                {
                    "name": "status",
                    "label": "状态",
                    "field": "status",
                    "align": "left",
                },
                {
                    "name": "actions",
                    "label": "操作",
                    "field": "actions",
                    "align": "left",
                    "classes": "col-actions",
                    "style": "white-space: nowrap;",
                },
            ]
            rows = []
            for name in sorted(
                tasks,
                # 置顶任务在前，其余保持原有「编号大（新建）在前」顺序
                key=lambda n: (
                    0 if (tasks.get(n) or {}).get("pinned") else 1,
                    -(int(n) if str(n).isdigit() else 0),
                ),
            ):
                cfg = tasks[name]
                is_bot = str(cfg.get("source_type") or SOURCE_CHANNEL) == SOURCE_BOT
                bot_username = str(cfg.get("bot_username") or "").lstrip("@")
                chat_names = (
                    [f"@{bot_username}"]
                    if is_bot and bot_username
                    else [chat_display(c) for c in cfg["chats"]]
                )
                running_now = task_running(name)
                start_ts = cfg.get("_start_ts")
                duration_text = (
                    format_duration(time.time() - start_ts)
                    if running_now and start_ts
                    else (str(cfg.get("last_duration") or "") or "—")
                )
                rows.append(
                    {
                        "name": str(name),
                        "pinned": bool(cfg.get("pinned")),
                        "task_type": SOURCE_TYPE_LABELS.get(
                            str(cfg.get("source_type") or SOURCE_CHANNEL),
                            SOURCE_TYPE_LABELS[SOURCE_CHANNEL],
                        ),
                        "accounts": ", ".join(cfg["accounts"]),
                        # 移动端单行摘要（数量 + 前两项）+ 点击展开的完整列表
                        "accounts_summary": _list_text(cfg["accounts"], 2),
                        "accounts_full": _list_text(cfg["accounts"]),
                        "chats": f"{len(chat_names)} 个："
                        + "、".join(chat_names[:3])
                        + ("…" if len(chat_names) > 3 else ""),
                        "chats_summary": _list_text(chat_names, 2),
                        "chats_full": _list_text(chat_names),
                        "keywords": "、".join(cfg.get("keywords") or []) or "全部链接",
                        "scan": "不限"
                        if not cfg.get("max_joins")
                        else str(cfg["max_joins"]),
                        "ignore_hist": (
                            "是" if cfg.get("ignore_history", True) else "否"
                        ),
                        "joined": f"{joined_count(name)} 个",
                        "last_time": str(cfg.get("last_start_time") or "") or "—",
                        "duration": duration_text,
                        "sweep": _sweep_text(name),
                        "status": _status_text(name),
                    }
                )
            table = (
                ui.table(
                    columns=columns,
                    rows=rows,
                    row_key="name",
                    pagination=5,
                )
                .classes("w-full tgs-desktop-table")
                .props("flat dense")
            )
            table_ref["table"] = table
            card_badges: dict = {}
            # 移动端卡片操作按钮：随状态变化切换「启动/停止」显隐
            card_start_btns: dict = {}
            card_stop_btns: dict = {}
            # 「扫描中」也算运行态（info 蓝色）；「常驻待命中」是开启补漏后的待命态
            running_expr = (
                "props.row.status.startsWith('运行中')"
                " || props.row.status.startsWith('扫描中')"
                " || props.row.status.startsWith('常驻待命中')"
                " || props.row.status.startsWith('停止中')"
            )
            table.add_slot(
                "body-cell-name",
                '<q-td :props="props">'
                '<q-icon v-if="props.row.pinned" name="push_pin" color="primary" '
                'size="16px" class="q-mr-xs" />'
                "{{ props.row.name }}</q-td>",
            )
            table.add_slot(
                "body-cell-status",
                '<q-td :props="props">'
                f"<q-badge :color=\"({running_expr}) ? 'info' : 'grey-6'\">"
                "{{ props.row.status }}</q-badge></q-td>",
            )
            table.add_slot(
                "body-cell-actions",
                '<q-td :props="props" class="col-actions">'
                f'<q-btn v-if="!({running_expr})" flat dense color="primary" '
                'label="启动" @click="$parent.$emit(\'startTask\', props.row)" />'
                f'<q-btn v-if="({running_expr})" flat dense color="negative" '
                'label="停止" @click="$parent.$emit(\'stopTask\', props.row)" />'
                '<q-btn flat dense icon="push_pin" '
                ":color=\"props.row.pinned ? 'primary' : 'grey-7'\" "
                ":aria-label=\"props.row.pinned ? '取消置顶' : '置顶'\" "
                "@click=\"$parent.$emit('pinTask', props.row)\">"
                "<q-tooltip>{{ props.row.pinned ? '取消置顶' : '置顶' }}"
                "</q-tooltip></q-btn>"
                '<q-btn flat dense color="primary" '
                'label="明细" @click="$parent.$emit(\'detailTask\', props.row)" />'
                '<q-btn flat dense color="primary" '
                'label="复制" @click="$parent.$emit(\'copyTask\', props.row)" />'
                '<q-btn flat dense color="negative" '
                'label="删除" @click="$parent.$emit(\'deleteTask\', props.row)" />'
                "</q-td>",
            )

            def make_set_status(n: str) -> Callable[[bool], None]:
                def _set(running: bool) -> None:
                    text = "运行中（已参与0）" if running else "未运行"
                    if not running:
                        text = "未运行"
                    else:
                        engine = get_engine(n)
                        # 刚启动时子进程尚未回报进度，回退「运行中」避免状态为空
                        text = (
                            (engine.progress_text() or "运行中") if engine else "运行中"
                        )
                    cfg_now = tasks.get(n) or {}
                    start_ts = cfg_now.get("_start_ts")
                    dur = (
                        format_duration(time.time() - start_ts)
                        if running and start_ts
                        else (str(cfg_now.get("last_duration") or "") or "—")
                    )
                    sweep = _sweep_text(n)
                    for r in table.rows:
                        if r["name"] == str(n):
                            r["status"] = text
                            r["duration"] = dur
                            r["sweep"] = sweep
                            try:
                                table.update()
                            except Exception:
                                logger.debug("刷新任务表格 UI 失败", exc_info=True)
                            break
                    badge = card_badges.get(str(n))
                    if badge is not None:
                        try:
                            badge.props(f"color={'info' if running else 'grey-6'}")
                            badge.text = f"{text} · {sweep}" if sweep else text
                            badge.update()
                        except Exception:
                            logger.debug("刷新任务状态徽标 UI 失败", exc_info=True)
                    # 移动端卡片的「启动/停止」按钮随运行状态切换
                    start_btn = card_start_btns.get(str(n))
                    if start_btn is not None:
                        try:
                            start_btn.set_visibility(not running)
                            start_btn.update()
                        except Exception:
                            logger.debug("刷新卡片启动按钮失败", exc_info=True)
                    stop_btn = card_stop_btns.get(str(n))
                    if stop_btn is not None:
                        try:
                            stop_btn.set_visibility(running)
                            stop_btn.update()
                        except Exception:
                            logger.debug("刷新卡片停止按钮失败", exc_info=True)
                    _update_card_time(str(n), _card_time_text(str(n), running))

                return _set

            async def on_start_event(e) -> None:
                row = e.args
                if isinstance(row, dict):
                    n = str(row.get("name") or "")
                    if n in tasks:
                        await start_task(n, make_set_status(n))

            async def on_stop_event(e) -> None:
                row = e.args
                if isinstance(row, dict):
                    n = str(row.get("name") or "")
                    if n in tasks:
                        await stop_task(n, make_set_status(n))

            def on_copy_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    duplicate_task(str(row["name"]))

            def on_detail_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    open_detail(str(row["name"]))

            def on_delete_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    remove_task(str(row["name"]))

            def on_pin_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    toggle_pin(str(row["name"]))

            table.on("startTask", on_start_event)
            table.on("stopTask", on_stop_event)
            table.on("copyTask", on_copy_event)
            table.on("detailTask", on_detail_event)
            table.on("deleteTask", on_delete_event)
            table.on("pinTask", on_pin_event)

            # —— 移动端卡片列表 ——
            mobile_cards = ui.column().classes("w-full gap-2 tgs-mobile-cards")
            shown_rows = rows if mobile_state["show_all"] else rows[:5]

            async def _card_start(nn: str, cb) -> None:
                await start_task(nn, cb)
                render_tasks()

            async def _card_stop(nn: str, cb) -> None:
                await stop_task(nn, cb)
                render_tasks()

            with mobile_cards:
                for row in shown_rows:
                    n = str(row["name"])
                    running = row["status"] != "未运行"
                    set_status = make_set_status(n)
                    time_text = _card_time_text(n, running)
                    with ui.card().classes("w-full mobile-list-card border"):
                        with ui.row().classes(
                            "w-full items-center gap-2 no-wrap justify-between"
                        ):
                            with ui.row().classes("items-center gap-1 no-wrap"):
                                if row["pinned"]:
                                    ui.icon("push_pin").classes("text-primary").props(
                                        "size=16px"
                                    )
                                ui.label(f"任务 #{n}").classes(
                                    "text-base font-semibold"
                                )
                            # 中奖回扫进度合并进状态徽标，回扫时显示「状态 · 回扫进度」
                            badge_text = (
                                f"{row['status']} · {row['sweep']}"
                                if row["sweep"]
                                else row["status"]
                            )
                            badge = ui.badge(badge_text).props(
                                f"color={'info' if running else 'grey-6'}"
                            )
                            card_badges[n] = badge
                        mobile_card_fields(
                            [
                                ("类型", row["task_type"]),
                                ("重复参与", row["ignore_hist"]),
                                (
                                    "账号",
                                    row["accounts_summary"],
                                    True,
                                    True,
                                    row["accounts_full"],
                                ),
                                (
                                    "监控对象",
                                    row["chats_summary"],
                                    True,
                                    True,
                                    row["chats_full"],
                                ),
                                ("关键词", row["keywords"], True),
                                ("参与上限", row["scan"]),
                                ("新参加", f"{row['joined']}抽奖"),
                            ]
                        )
                        # 执行时间单独渲染，运行中由状态回调/轮询每秒刷新耗时
                        card_time_els[n] = ui.html(_card_time_html(time_text)).classes(
                            "w-full"
                        )
                        card_time_texts[n] = time_text
                        with ui.row().classes("w-full mobile-card-actions"):
                            # 启动/停止两个按钮都创建，按运行状态切换显隐（创建后自动启动也能跟着变）
                            start_btn = ui.button(
                                "启动",
                                on_click=lambda nn=n,
                                cb=set_status: asyncio.create_task(_card_start(nn, cb)),
                            ).props("flat dense color=primary")
                            stop_btn = ui.button(
                                "停止",
                                on_click=lambda nn=n,
                                cb=set_status: asyncio.create_task(_card_stop(nn, cb)),
                            ).props("flat dense color=negative")
                            if running:
                                start_btn.set_visibility(False)
                            else:
                                stop_btn.set_visibility(False)
                            card_start_btns[n] = start_btn
                            card_stop_btns[n] = stop_btn
                            pin_color = "primary" if row["pinned"] else "grey-7"
                            ui.button(
                                icon="push_pin",
                                on_click=lambda nn=n: toggle_pin(nn),
                            ).props(f"flat dense color={pin_color}").tooltip(
                                "取消置顶" if row["pinned"] else "置顶"
                            )
                            ui.button(
                                "明细",
                                on_click=lambda nn=n: open_detail(nn),
                            ).props("flat dense color=primary")
                            ui.button(
                                "复制",
                                on_click=lambda nn=n: duplicate_task(nn),
                            ).props("flat dense color=primary")
                            ui.button(
                                "删除",
                                on_click=lambda nn=n: remove_task(nn),
                            ).props("flat dense color=negative")
                if len(rows) > 5:
                    if mobile_state["show_all"]:
                        ui.button(
                            "收起",
                            on_click=lambda: (
                                mobile_state.update(show_all=False),
                                render_tasks(),
                            ),
                        ).props("flat dense").classes("w-full")
                    else:
                        ui.button(
                            f"展开更多（共 {len(rows)} 条）",
                            on_click=lambda: (
                                mobile_state.update(show_all=True),
                                render_tasks(),
                            ),
                        ).props("flat dense").classes("w-full")

            for name in list(tasks):
                set_status = make_set_status(str(name))
                tasks[name]["_set_status"] = set_status
                _GLOBAL_STATUS_CBS[engine_key(name)] = set_status

    ui.label("自动抽奖").classes("text-lg font-semibold")
    ui.label(
        "监控指定频道/群组消息中的 t.me 抽奖机器人链接（?start= 参数），自动点击参与；"
        "任务启动时可自动扫描历史消息（至参与上限或连续 5 条已开奖为止）。"
        "同一抽奖链接每个账号只参与一次，"
        "记录持久化在工作目录，重启不重复参与。"
    ).classes("text-sm text-gray-500")

    # —— 清除所有已参与抽奖记录 ——
    def open_clear_records_dlg() -> None:
        if any(task_running(n) for n in tasks):
            ui.notify("有任务正在运行，请先停止再清除参与记录", type="warning")
            return
        # 参与记录由子进程写入，这里每次重新读盘，避免页面持有过期快照
        total = GiveawayState(workdir).count()
        clear_count_label.text = (
            f"当前共 {total} 条已参与记录（覆盖所有账号）。"
            "清除后，相同抽奖链接对所有账号都视为未参与，下次扫描将重新参与；"
            "中奖消息去重记录与各账号 × bot 的回扫进度（水位线）保留，"
            "已提醒过的旧中奖消息不会被重复推送。"
            "此操作不可恢复，确定清除？"
        )
        clear_records_dlg.open()

    def do_clear_records() -> None:
        total = GiveawayState(workdir).clear()
        clear_records_dlg.close()
        if total:
            log_buf.append(
                f"{datetime.now():%H:%M:%S} 已清除所有账号的 {total} 条已参与抽奖记录"
            )
            ui.notify(f"已清除 {total} 条已参与记录", type="positive")
        else:
            ui.notify("当前没有已参与记录", type="info")

    with ui.dialog() as clear_records_dlg, ui.card().classes("w-full max-w-md"):
        add_dialog_close_button(clear_records_dlg)
        ui.label("清除所有已参与抽奖记录").classes("text-lg font-semibold")
        clear_count_label = ui.label("").classes("text-sm text-gray-700")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=clear_records_dlg.close).props("flat")
            ui.button("确认清除", color="negative", on_click=do_clear_records)

    with ui.row().classes("w-full items-center justify-between"):
        ui.label("任务列表").classes("font-semibold")
        with ui.row().classes("gap-2 items-center"):
            ui.button(
                "清除参与记录",
                icon="delete_sweep",
                on_click=open_clear_records_dlg,
            ).props("outline color=negative").tooltip(
                "删除所有账号的已参与抽奖记录（保留中奖消息去重与回扫进度），"
                "清除后相同链接可再次参与"
            )
            ui.button("添加任务", icon="add", on_click=lambda: add_dlg.open()).props(
                "outline"
            )
    task_list_container = ui.column().classes("w-full gap-2")
    mobile_state = {"show_all": False}

    ui.separator()

    def clear_log() -> None:
        log_buf.clear()
        _seen["n"] = len(log_buf)
        send_log.clear()

    def copy_log() -> None:
        if not log_buf:
            ui.notify("暂无可复制的日志", type="warning")
            return
        copy_text_to_clipboard("\n".join(log_buf))
        ui.notify(f"已复制 {len(log_buf)} 行日志", type="positive")

    def copy_debug_log() -> None:
        """复制调试日志：环境信息 + 任务配置/状态 + 任务历史 + giveaway.log 尾部。

        手机端报错后一键复制，粘贴到电脑即可拿到定位/复现所需的全部上下文。
        """
        wd = Path(workdir)
        # tasks 里存有 UI 回调（_set_status 等 _ 前缀内部键），不能直接序列化
        serializable_tasks = {
            n: {k: v for k, v in cfg.items() if not str(k).startswith("_")}
            for n, cfg in tasks.items()
        }
        sections: list[tuple[str, str]] = [
            ("任务配置（所有抽奖任务）", pretty_json(serializable_tasks)),
            ("任务状态", pretty_json({n: _status_text(n) for n in tasks})),
        ]
        try:
            hist = json.loads(
                (wd / "webui_task_history.json").read_text(encoding="utf-8-sig")
            )
            if isinstance(hist, list) and hist:
                sections.append(("任务历史（最近15条）", pretty_json(hist[-15:])))
        except (OSError, json.JSONDecodeError):
            logger.debug("读取任务历史失败", exc_info=True)
        sections.append(
            (
                "logs/giveaway.log 最近200行",
                read_file_tail(wd / "logs" / "giveaway.log"),
            )
        )
        copy_text_to_clipboard(build_debug_report(wd, sections))
        ui.notify(
            "已复制调试日志（含环境信息与任务配置），粘贴给电脑即可排查",
            type="positive",
        )

    def toggle_fullscreen() -> None:
        """切换共用日志全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
        fullscreen.toggle()

    with ui.row().classes("w-full items-center justify-between"):
        ui.label("共用日志（所有任务）").classes("font-semibold")
        with ui.row().classes("gap-1 items-center"):
            ui.button("复制调试日志", icon="bug_report", on_click=copy_debug_log).props(
                "flat dense"
            ).tooltip("复制环境信息+任务配置+任务历史+日志文件，粘贴到电脑排查复现")
            ui.button(icon="content_copy", on_click=copy_log).props(
                "flat round dense"
            ).tooltip("一键复制日志")
            ui.button(icon="delete_outline", on_click=clear_log).props(
                "flat round dense"
            ).tooltip("清空日志")
            ui.button(icon="fullscreen", on_click=toggle_fullscreen).props(
                "flat round dense"
            ).tooltip("全屏显示")
    send_log = ui.log(max_lines=1000).classes(LOG_AREA_CLASS).style("height: 320px")
    log_buf = _GLOBAL_LOGS.setdefault(wd_key, [])
    _seen = {"n": len(log_buf)}
    for line in log_buf:
        try:
            send_log.push(line)
        except Exception:
            logger.debug("回放历史日志到界面日志区失败", exc_info=True)

    # 退出全屏的悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
    exit_full_btn = exit_fullscreen_button(toggle_fullscreen)
    fullscreen = LogFullscreen(send_log, exit_full_btn)

    client = ui.context.client

    async def _poll_loop() -> None:
        while True:
            await asyncio.sleep(2.0)
            if not client.has_socket_connection:
                return
            if not should_poll(client, "giveaway"):
                # 页面不可见/不在该 tab 时跳过刷新，仅推进日志游标（保留最近 200 行）
                _seen["n"] = max(_seen["n"], len(log_buf) - 200)
                continue
            table = table_ref["table"]
            if table is not None:
                try:
                    changed = False
                    for r in table.rows:
                        n = str(r.get("name") or "")
                        if n in tasks:
                            new_status = _status_text(n)
                            if r.get("status") != new_status:
                                r["status"] = new_status
                                changed = True
                            new_joined = f"{joined_count(n)} 个"
                            if r.get("joined") != new_joined:
                                r["joined"] = new_joined
                                changed = True
                            cfg_now = tasks[n]
                            start_ts = cfg_now.get("_start_ts")
                            if start_ts and task_running(n):
                                dur = format_duration(time.time() - start_ts)
                                if r.get("duration") != dur:
                                    r["duration"] = dur
                                    changed = True
                            # 移动端卡片：运行中每秒刷新耗时，结束后显示持久化时长
                            _update_card_time(n, _card_time_text(n, task_running(n)))
                    if changed:
                        table.update()
                except Exception:
                    logger.debug("轮询刷新任务表格 UI 失败", exc_info=True)
            if _seen["n"] > len(log_buf):
                _seen["n"] = len(log_buf)
            new_lines = log_buf[_seen["n"] :]
            if not new_lines:
                continue
            _seen["n"] = len(log_buf)
            try:
                with client:
                    for line in new_lines:
                        try:
                            send_log.push(line)
                        except Exception:
                            logger.debug("推送新日志到界面日志区失败", exc_info=True)
            except Exception:
                return

    asyncio.create_task(_poll_loop())

    # —— 参与明细弹窗：按用户显示本次/上次新参加的抽奖数 ——
    with ui.dialog() as detail_dlg, ui.card().classes("w-full max-w-md"):
        add_dialog_close_button(detail_dlg)
        detail_title_label = ui.label("参与明细").classes("text-lg font-semibold")
        detail_total_label = ui.label("").classes("text-sm text-gray-700")
        detail_list = ui.column().classes("w-full gap-1")

    def open_detail(name: str) -> None:
        by_acct = joined_by_account(name)
        by_acct_chat = joined_by_account_chat(name)
        total = joined_count(name)
        engine = get_engine(name)
        scope = "本次运行" if engine is not None and engine.running else "上次运行"
        detail_title_label.text = f"任务 #{name} 参与明细"
        detail_total_label.text = f"{scope}共新参加 {total} 个抽奖"
        detail_list.clear()
        with detail_list:
            if not by_acct:
                ui.label("暂无参与记录").classes("text-sm text-gray-400")
            else:
                for acct in sorted(by_acct, key=lambda a: by_acct[a], reverse=True):
                    with ui.column().classes("w-full gap-0.5"):
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(acct).classes("text-sm font-medium")
                            ui.badge(f"{by_acct[acct]} 个抽奖", color="primary")
                        # 该账号在每个来源频道/群组新参与的抽奖数
                        chats = by_acct_chat.get(acct) or {}
                        for chat_name in sorted(
                            chats, key=lambda c: chats[c], reverse=True
                        ):
                            shown = str(chat_name)
                            if len(shown) > 24:
                                shown = shown[:24] + "…"
                            with ui.row().classes(
                                "w-full items-center justify-between pl-4"
                            ):
                                ui.label(f"└ {shown}").classes("text-xs text-gray-600")
                                ui.badge(f"{chats[chat_name]} 个", color="grey")
        detail_dlg.open()

    # —— 添加任务弹窗 ——
    with ui.dialog() as add_dlg, ui.card().classes("w-full max-w-3xl"):
        add_dialog_close_button(add_dlg)
        ui.label("添加任务").classes("text-lg font-semibold")
        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            source_type_toggle = ui.toggle(
                {SOURCE_CHANNEL: "频道/群组", SOURCE_BOT: "百宝箱机器人"},
                value=SOURCE_CHANNEL,
                on_change=lambda _: _apply_source_visibility(),
            ).props("dense")
            bot_username_input = ui.input(
                label="机器人用户名",
                value=BAIBAOXIANG_BOT,
            ).classes("w-56")
            bot_scan_limit_input = ui.number(
                label="回扫消息条数",
                value=BAIBAOXIANG_SCAN_LIMIT,
                min=1,
                max=1000,
                format="%d",
            ).classes("w-44")
        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            account_select = (
                ui.select(
                    label="选择账号（可多选）",
                    options=[],
                    multiple=True,
                    clearable=True,
                )
                .classes("min-w-[260px]")
                .props("use-chips")
            )
            attach_select_all(account_select)
            ui.button("刷新账号/频道", on_click=lambda: refresh_options()).props(
                "outline"
            )

            async def update_chats_from_tg() -> None:
                accounts = list(account_select.value or [])
                if not accounts:
                    accounts = list_session_names(
                        Path(default_session_dir or "."), workdir
                    )
                if not accounts:
                    ui.notify("没有可用账号", type="warning")
                    return
                update_btn.disable()

                def _log(msg: str) -> None:
                    log_buf.append(f"{datetime.now():%H:%M:%S} {msg}")

                try:
                    _log(f"开始从 Telegram 更新 {len(accounts)} 个账号的对话列表...")
                    for a in accounts:
                        await update_account_chats(
                            a, Path(default_session_dir or "."), workdir, _log
                        )
                    refresh_options()
                    ui.notify("频道列表已从 Telegram 更新", type="positive")
                finally:
                    update_btn.enable()

            update_btn = ui.button("从TG更新频道", on_click=update_chats_from_tg).props(
                "outline"
            )

        with ui.row().classes("items-end w-full gap-3 flex-wrap") as channel_pick_row:
            channel_select = (
                ui.select(
                    label="选择监控频道/群组（可混选，输入关键字过滤）",
                    options={},
                    multiple=True,
                )
                .classes("min-w-[320px] flex-grow")
                .props("use-chips use-input options-dense")
            )
            channel_type_toggle = ui.toggle(
                {"all": "全部", "channel": "仅频道", "group": "仅群组"},
                value="all",
                on_change=lambda _: apply_channel_filter(),
            ).props("dense")
            manual_chats_input = ui.input(
                label="手动添加频道（逗号分隔 chat_id 或 @username）",
                placeholder="-1001234567890, @somechannel",
            ).classes("min-w-[300px] flex-grow")

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            keywords_input = ui.input(
                label="消息关键词（逗号分隔，留空=含 bot 链接就参与）",
                value="，".join(DEFAULT_KEYWORDS),
            ).classes("min-w-[280px] flex-grow")
            max_joins_input = ui.number(
                label="参与条数上限（留空=不限）",
                value=None,
                min=0,
                max=10000,
                format="%d",
            ).classes("w-56")
            auto_scan_switch = ui.switch("启动时自动扫描一轮", value=True).classes(
                "pt-6"
            )
            auto_join_switch = ui.switch(
                "自动加入抽奖要求的频道/群组", value=True
            ).classes("pt-6")
            ignore_hist_switch = ui.switch(
                "允许重复参与（忽略历史成功记录）", value=True
            ).classes("pt-6")
            rescue_switch = ui.switch(
                "启用历史补漏扫描",
                value=False,
                on_change=lambda: _apply_rescue_visibility(),
            ).classes("pt-6")

        def _range_inputs(
            title: str,
            *,
            low: int,
            high: int,
            max_value: int,
            low_cls: str = "w-24",
            high_cls: str = "w-24",
        ) -> tuple[ui.number, ui.number]:
            """上下限共用一个标签：左侧为下限、右侧为上限，用输入框前缀区分。"""
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.label(title).classes("text-sm text-gray-600 shrink-0")
                low_input = (
                    ui.number(value=low, min=0, max=max_value, format="%d")
                    .props('prefix="下限"')
                    .classes(low_cls)
                )
                ui.label("～").classes("text-gray-400 shrink-0")
                high_input = (
                    ui.number(value=high, min=0, max=max_value, format="%d")
                    .props('prefix="上限"')
                    .classes(high_cls)
                )
            return low_input, high_input

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            delay_min_input, delay_max_input = _range_inputs(
                "参与前随机延迟（秒）", low=3, high=10, max_value=600
            )

            rescue_interval_input = ui.number(
                label="补漏扫描间隔（分钟）",
                value=30,
                min=1,
                max=1440,
                format="%d",
            ).classes("w-52")
            rescue_limit_input = ui.number(
                label="补漏重拉条数/频道",
                value=10,
                min=1,
                max=100,
                format="%d",
            ).classes("w-44")

        with ui.expansion("限流保护（加入/解析节奏与配额）", icon="shield").classes(
            "w-full"
        ):
            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                join_pace_min_input, join_pace_max_input = _range_inputs(
                    "加入间隔（秒）",
                    low=int(JOIN_PACE_RANGE[0]),
                    high=int(JOIN_PACE_RANGE[1]),
                    max_value=3600,
                )
                join_hourly_cap_input = ui.number(
                    label="每账号 1 小时加入上限（0=不限）",
                    value=JOIN_HOURLY_CAP,
                    min=0,
                    max=1000,
                    format="%d",
                ).classes("w-60")
                join_daily_cap_input = ui.number(
                    label="每账号 24 小时加入上限（0=不限）",
                    value=JOIN_DAILY_CAP,
                    min=0,
                    max=10000,
                    format="%d",
                ).classes("w-64")
            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                resolve_pace_min_input, resolve_pace_max_input = _range_inputs(
                    "解析间隔（秒）",
                    low=int(RESOLVE_PACE_RANGE[0]),
                    high=int(RESOLVE_PACE_RANGE[1]),
                    max_value=600,
                )
                join_flood_buffer_input = ui.number(
                    label="限流额外缓冲（秒）",
                    value=JOIN_FLOOD_BUFFER,
                    min=0,
                    max=600,
                    format="%d",
                ).classes("w-48")
            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                global_qps_input = ui.number(
                    label="全局调用上限（次/秒，0=不限）",
                    value=int(DEFAULT_GLOBAL_QPS),
                    min=0,
                    max=100,
                    format="%d",
                ).classes("w-64")
                max_concurrent_accounts_input = ui.number(
                    label="同时扫描账号数上限（0=不限）",
                    value=DEFAULT_MAX_CONCURRENT_ACCOUNTS,
                    min=0,
                    max=100,
                    format="%d",
                ).classes("w-64")
            ui.label(
                "加入/解析节奏与滚动配额用于规避 Telegram 限流"
                "（FLOOD_WAIT 短暂停、PEER_FLOOD 会数小时到数天无法加群）；"
                "间隔在上下限之间随机取值，配额按账号统计且跨任务重启累计，"
                "0 表示不限制。"
            ).classes("text-xs text-gray-500")
            ui.label(
                "全局调用上限与同时扫描账号数按「整个任务进程内所有账号合计」生效："
                "429 的 Transport flood 按出口 IP 计数，账号越多越需要压低合计速率。"
            ).classes("text-xs text-gray-500")

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            win_switch = ui.switch(
                "中奖识别（检测到中奖消息自动提醒；转发目标与跳过关键字在右上角菜单「设置」）",
                value=True,
            )

        def _apply_rescue_visibility() -> None:
            visible = bool(rescue_switch.value)
            rescue_interval_input.visible = visible
            rescue_limit_input.visible = visible

        def _apply_source_visibility() -> None:
            """按任务类型切换表单项：机器人任务只保留机器人/关键词/回扫参数。"""
            is_bot = str(source_type_toggle.value or SOURCE_CHANNEL) == SOURCE_BOT
            bot_username_input.visible = is_bot
            bot_scan_limit_input.visible = is_bot
            channel_pick_row.visible = not is_bot
            auto_join_switch.visible = not is_bot
            if is_bot:
                default_kw = "，".join(DEFAULT_KEYWORDS)
                if not (keywords_input.value or "").strip() or (
                    keywords_input.value == default_kw
                ):
                    keywords_input.value = BAIBAOXIANG_KEYWORD
                auto_join_switch.value = False
            for el in (
                bot_username_input,
                bot_scan_limit_input,
                channel_pick_row,
                auto_join_switch,
                keywords_input,
            ):
                try:
                    el.update()
                except Exception:
                    logger.debug("切换任务类型表单项失败", exc_info=True)

        _apply_rescue_visibility()
        _apply_source_visibility()

        ui.label(
            "提示：参与 = 以账号身份向抽奖机器人发送 /start（等同手动点开链接）；"
            "机器人若弹出「确认参与」类按钮会自动点击；开启自动加入后，"
            "机器人回复消息中出现的频道/群组链接会被逐一加入（激进模式，可能误加）。"
            "「百宝箱机器人」类型直接扫描该 bot 私聊里的「您已获得抽奖资格（已达标）」"
            "消息并点击「点击参与抽奖」，无需监控频道、也不会自动加群。"
            "补漏扫描会按设定间隔重拉各频道最近若干条消息，"
            "捞回此前因网络异常/中断漏掉的抽奖帖，连续多轮拉取失败会自动暂停一段时间。"
            "中奖识别会在 bot 私聊出现中奖消息时转发提醒并写入消息中心，"
            "转发目标与微信推送（Server酱）在右上角消息中心统一设置，"
            "任务启动与每轮补漏时也会回扫各 bot 私聊，补捡停机期间的中奖通知。"
            "请确保账号已加入所监控的频道。"
        ).classes("text-xs text-amber-600")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=add_dlg.close).props("flat")
            ui.button("确定添加", color="primary", on_click=on_add_task)

    refresh_options()
    render_tasks()
    return refresh_options
