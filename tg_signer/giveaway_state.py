"""自动抽奖参与状态存储：按账号持久化已参与记录与中奖回扫水位线。

多任务并发时 save() 先与磁盘合并，避免旧快照覆盖新写入（见 _merge_from_disk）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


STATE_FILE = "giveaway_state.json"


# 每个账号最多保留的去重记录数，超出丢弃最旧的
MAX_STATE_PER_ACCOUNT = 1000




# FLOOD_WAIT 失败记录的冷却时间（秒），冷却后允许重试
FLOOD_COOLDOWN = 3600




class GiveawayState:
    """按账号持久化已参与记录：{accounts: {account: {"bot|param": record}}}。

    另持久化中奖回扫水位线 {win_watermarks: {account: {bot_lower: msg_id}}}。
    """

    def __init__(self, workdir) -> None:
        self.path = Path(workdir) / STATE_FILE
        self._data: Dict[str, Dict[str, dict]] = {}
        self._win_wm: Dict[str, Dict[str, int]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts")
            if isinstance(accounts, dict):
                self._data = {
                    str(k): dict(v) for k, v in accounts.items() if isinstance(v, dict)
                }
            wm = data.get("win_watermarks")
            if isinstance(wm, dict):
                parsed: Dict[str, Dict[str, int]] = {}
                for acc, sub in wm.items():
                    if not isinstance(sub, dict):
                        continue
                    bot_map: Dict[str, int] = {}
                    for bot, mid in sub.items():
                        try:
                            bot_map[str(bot)] = int(mid)
                        except (TypeError, ValueError):
                            continue
                    if bot_map:
                        parsed[str(acc)] = bot_map
                self._win_wm = parsed
        except (OSError, json.JSONDecodeError):
            self._data = {}
            self._win_wm = {}

    def _merge_from_disk(self) -> None:
        """落盘前重读磁盘并合并，防止并发覆盖（多任务并发运行时各自持有
        独立的 GiveawayState 实例，save 整文件写入会让旧快照冲掉他处
        新写入的参与/去重记录）：

        - 本地没有的账号/键 → 从磁盘补进来
        - 同键都存在 → 保留 ts 较新的一条
        """
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return
        for account, bucket in accounts.items():
            if not isinstance(bucket, dict):
                continue
            local = self._data.setdefault(str(account), {})
            for key, rec in bucket.items():
                if not isinstance(rec, dict):
                    continue
                cur = local.get(key)
                if cur is None or str(rec.get("ts") or "") > str(cur.get("ts") or ""):
                    local[key] = rec

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        self._merge_from_disk()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"accounts": self._data, "win_watermarks": self._win_wm},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty = False

    @staticmethod
    def _key(username: str, param: str) -> str:
        return f"{username.lower()}|{param}"

    def has(self, account: str, username: str, param: str) -> bool:
        """已真正参与（非 flood_wait 失败记录）才视为重复"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        if rec is None:
            return False
        return not str(rec.get("note") or "").startswith("flood_wait")

    def is_ended(self, account: str, username: str, param: str) -> bool:
        """bot 曾回复「已结束」＝该链接终态，无论是否忽略历史都不再重试"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        return rec is not None and str(rec.get("note") or "") == "ended"

    def is_blocked(
        self, account: str, username: str, param: str, cooldown: int = FLOOD_COOLDOWN
    ) -> bool:
        """flood_wait 失败记录是否仍在冷却期内（冷却后允许重试）"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        if rec is None or not str(rec.get("note") or "").startswith("flood_wait"):
            return False
        try:
            ts = datetime.fromisoformat(str(rec.get("ts") or ""))
        except ValueError:
            return False
        return (datetime.now() - ts).total_seconds() < cooldown

    def add(
        self,
        account: str,
        username: str,
        param: str,
        *,
        channel: str = "",
        note: str = "joined",
    ) -> None:
        bucket = self._data.setdefault(account, {})
        bucket[self._key(username, param)] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "bot": username,
            "param": param,
            "channel": channel,
            "note": note,
        }
        if len(bucket) > MAX_STATE_PER_ACCOUNT:
            # 按时间戳丢弃最旧的若干条
            for old_key in sorted(bucket, key=lambda k: str(bucket[k].get("ts") or ""))[
                : len(bucket) - MAX_STATE_PER_ACCOUNT
            ]:
                bucket.pop(old_key, None)
        self._dirty = True

    def count(self, account: Optional[str] = None) -> int:
        if account is not None:
            return len(self._data.get(account, {}))
        return sum(len(v) for v in self._data.values())

    def clear(self) -> int:
        """清空所有账号的已参与记录（含中奖消息去重与回扫水位线）并立即落盘。

        返回清除的参与记录条数。win: 去重记录存于 _data 一并清除；
        回扫水位线（每账号 × bot 扫到的位置）同步重置，下轮回扫从
        新基线（回扫条数）重建。
        """
        total = self.count()
        self._data = {}
        self._win_wm = {}
        self._dirty = True
        # save 会从磁盘合并回来，先删文件确保清空真正生效
        try:
            self.path.unlink()
        except OSError:
            pass
        self.save(force=True)
        return total

    def bots_for(self, account: str) -> List[str]:
        """该账号有过交互记录的全部 bot 用户名（按最近交互时间倒序）。

        中奖回扫用：账号参与过的 bot 才可能发来中奖私信。
        """
        bucket = self._data.get(account, {})
        latest: Dict[str, tuple] = {}  # lower -> (ts, 原始用户名)
        for key, rec in bucket.items():
            bot = str((rec or {}).get("bot") or key.split("|", 1)[0]).strip()
            if not bot:
                continue
            lower = bot.lower()
            ts = str((rec or {}).get("ts") or "")
            if lower not in latest or ts > latest[lower][0]:
                latest[lower] = (ts, bot)
        return [
            bot
            for _, bot in sorted(
                latest.values(), key=lambda pair: pair[0], reverse=True
            )
        ]

    def win_watermark(self, account: str, bot: str) -> Optional[int]:
        """该账号 × bot 上次中奖回扫扫到的消息 id，从未扫过返回 None。"""
        return self._win_wm.get(account, {}).get(bot.lower())

    def set_win_watermark(self, account: str, bot: str, msg_id: int) -> None:
        """推进中奖回扫水位线到 msg_id（仅前进不后退）。"""
        bot_map = self._win_wm.setdefault(account, {})
        old = bot_map.get(bot.lower())
        if old is not None and int(msg_id) <= old:
            return
        bot_map[bot.lower()] = int(msg_id)
        self._dirty = True
