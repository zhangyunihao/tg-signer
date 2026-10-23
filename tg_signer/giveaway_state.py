"""自动抽奖参与状态存储（SQLite）：按账号持久化已参与记录、中奖回扫水位线
与加入频道时间日志。

并发：多任务（子进程）各自持有独立连接，靠 WAL + busy_timeout 安全读写，
不再有「整文件重写导致旧快照覆盖新记录」的问题。旧版 giveaway_state.json
在首次打开数据库时自动迁移，迁移完成后旧文件改名为 .json.migrated。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


STATE_DB_FILE = "giveaway_state.db"


# 旧版 JSON 状态文件（仅用于一次性迁移）
LEGACY_STATE_FILE = "giveaway_state.json"


# 每个账号最多保留的去重记录数，超出丢弃最旧的
MAX_STATE_PER_ACCOUNT = 1000


# FLOOD_WAIT 失败记录的冷却时间（秒），冷却后允许重试
FLOOD_COOLDOWN = 3600


# 加入频道时间日志的保留窗口（秒）：用于滚动 24 小时加入配额
JOIN_LOG_WINDOW = 48 * 3600


# 加入时间日志每账号保留上限（只为滚动配额服务）
MAX_JOIN_EVENTS_PER_ACCOUNT = 200


# 中奖消息去重记录的前缀（参与记录键为 bot|win:<msg_id>）
WIN_PARAM_PREFIX = "win:"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS participations (
    account TEXT NOT NULL,
    bot_key TEXT NOT NULL,
    param   TEXT NOT NULL,
    bot     TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    note    TEXT NOT NULL DEFAULT 'joined',
    ts      TEXT NOT NULL,
    PRIMARY KEY (account, bot_key, param)
);
CREATE INDEX IF NOT EXISTS idx_participations_account_ts
    ON participations (account, ts);
CREATE TABLE IF NOT EXISTS win_watermarks (
    account TEXT NOT NULL,
    bot_key TEXT NOT NULL,
    msg_id  INTEGER NOT NULL,
    PRIMARY KEY (account, bot_key)
);
CREATE TABLE IF NOT EXISTS join_log (
    account TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_join_log_account_ts
    ON join_log (account, ts);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class GiveawayState:
    """按账号持久化已参与记录（表 participations，主键 account+bot_key+param）。

    另持久化中奖回扫水位线（win_watermarks）与加入时间日志（join_log）。
    """

    def __init__(self, workdir) -> None:
        self.path = Path(workdir) / STATE_DB_FILE
        self._legacy_path = Path(workdir) / LEGACY_STATE_FILE
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self.load()

    # ---- 连接与迁移 ----

    def load(self) -> None:
        """打开数据库并确保表结构就绪（旧版 JSON 在此完成一次性迁移）。"""
        self._db()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __del__(self) -> None:  # 释放句柄，避免 Windows 下占用文件
        try:
            self.close()
        except Exception:
            pass

    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
            conn.isolation_level = None  # 自动提交；多语句用显式事务
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                logger.debug("WAL 不可用，回退默认日志模式", exc_info=True)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            self._conn = conn
            self._migrate_legacy_json(conn)
            return conn

    def _migrate_legacy_json(self, conn: sqlite3.Connection) -> None:
        """把旧版 giveaway_state.json 一次性导入数据库。

        已迁移（meta 标记存在）或旧文件不存在时直接返回；读取/写入失败则
        保留旧文件不动，下次启动再试，不影响正常使用。
        """
        if not self._legacy_path.is_file():
            return
        if conn.execute(
            "SELECT 1 FROM meta WHERE key = 'legacy_json_migrated'"
        ).fetchone():
            return
        try:
            data = json.loads(self._legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("旧版 %s 读取失败，跳过迁移", self._legacy_path.name)
            return
        if not isinstance(data, dict):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._import_accounts(conn, data.get("accounts"))
            self._import_watermarks(conn, data.get("win_watermarks"))
            self._import_join_log(conn, data.get("join_log"))
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value)"
                " VALUES ('legacy_json_migrated', '1')"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            logger.exception("旧版状态迁移失败，保留 %s", self._legacy_path.name)
            return
        migrated = self._legacy_path.with_suffix(".json.migrated")
        try:
            self._legacy_path.replace(migrated)
        except OSError:
            logger.debug("旧版状态文件改名失败", exc_info=True)
        logger.info("已把旧版 %s 迁移到 %s", LEGACY_STATE_FILE, STATE_DB_FILE)

    @staticmethod
    def _import_accounts(conn: sqlite3.Connection, accounts) -> None:
        if not isinstance(accounts, dict):
            return
        for account, bucket in accounts.items():
            if not isinstance(bucket, dict):
                continue
            for key, rec in bucket.items():
                if not isinstance(rec, dict):
                    continue
                bot = str(rec.get("bot") or key.split("|", 1)[0]).strip()
                param = str(rec.get("param") or key.split("|", 1)[-1])
                if not account or not bot or not param:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO participations"
                    " (account, bot_key, param, bot, channel, note, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(account),
                        bot.lower(),
                        param,
                        bot,
                        str(rec.get("channel") or ""),
                        str(rec.get("note") or "joined"),
                        str(
                            rec.get("ts")
                            or datetime.now().isoformat(timespec="seconds")
                        ),
                    ),
                )

    @staticmethod
    def _import_watermarks(conn: sqlite3.Connection, watermarks) -> None:
        if not isinstance(watermarks, dict):
            return
        for account, bots in watermarks.items():
            if not isinstance(bots, dict):
                continue
            for bot, msg_id in bots.items():
                try:
                    mid = int(msg_id)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO win_watermarks"
                    " (account, bot_key, msg_id) VALUES (?, ?, ?)",
                    (str(account), str(bot).lower(), mid),
                )

    @staticmethod
    def _import_join_log(conn: sqlite3.Connection, join_log) -> None:
        if not isinstance(join_log, dict):
            return
        for account, times in join_log.items():
            if not isinstance(times, list):
                continue
            for ts in sorted({float(t) for t in times if isinstance(t, (int, float))}):
                conn.execute(
                    "INSERT INTO join_log (account, ts) VALUES (?, ?)",
                    (str(account), ts),
                )

    def save(self, force: bool = False) -> None:
        """兼容旧接口：每条记录写入时已即时提交，这里只需确保库已就绪。"""
        if self._conn is None:
            if not (force or self._legacy_path.is_file()):
                return
            self._db()

    # ---- 参与记录 ----

    @staticmethod
    def _key(username: str, param: str) -> str:
        return f"{username.lower()}|{param}"

    def _fetch(self, account: str, username: str, param: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return (
                self._db()
                .execute(
                    "SELECT bot, param, channel, note, ts FROM participations"
                    " WHERE account = ? AND bot_key = ? AND param = ?",
                    (account, username.lower(), param),
                )
                .fetchone()
            )

    def has(self, account: str, username: str, param: str) -> bool:
        """已真正参与（非 flood_wait 失败记录）才视为重复"""
        row = self._fetch(account, username, param)
        if row is None:
            return False
        return not str(row["note"] or "").startswith("flood_wait")

    def is_ended(self, account: str, username: str, param: str) -> bool:
        """bot 曾回复「已结束」＝该链接终态，无论是否忽略历史都不再重试"""
        row = self._fetch(account, username, param)
        return row is not None and str(row["note"] or "") == "ended"

    def is_blocked(
        self, account: str, username: str, param: str, cooldown: int = FLOOD_COOLDOWN
    ) -> bool:
        """flood_wait 失败记录是否仍在冷却期内（冷却后允许重试）"""
        row = self._fetch(account, username, param)
        if row is None or not str(row["note"] or "").startswith("flood_wait"):
            return False
        try:
            ts = datetime.fromisoformat(str(row["ts"] or ""))
        except ValueError:
            return False
        return (datetime.now() - ts).total_seconds() < cooldown

    def record(
        self, account: str, username: str, param: str
    ) -> Optional[Dict[str, str]]:
        """读取单条参与记录（含 bot/channel/note/ts），不存在返回 None。"""
        row = self._fetch(account, username, param)
        if row is None:
            return None
        return {key: str(row[key]) for key in row.keys()}

    def add(
        self,
        account: str,
        username: str,
        param: str,
        *,
        channel: str = "",
        note: str = "joined",
    ) -> None:
        """写入/覆盖一条参与记录，并裁剪该账号超出上限的最旧记录。"""
        conn = self._db()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO participations"
                    " (account, bot_key, param, bot, channel, note, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(account, bot_key, param) DO UPDATE SET"
                    " bot = excluded.bot,"
                    " channel = excluded.channel,"
                    " note = excluded.note,"
                    " ts = excluded.ts",
                    (
                        account,
                        username.lower(),
                        param,
                        username,
                        channel,
                        note,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                conn.execute(
                    "DELETE FROM participations WHERE account = ? AND rowid NOT IN ("
                    " SELECT rowid FROM participations WHERE account = ?"
                    " ORDER BY ts DESC, rowid DESC LIMIT ?)",
                    (account, account, MAX_STATE_PER_ACCOUNT),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def count(self, account: Optional[str] = None) -> int:
        """已参与抽奖记录条数，不含中奖消息去重记录（win:）。"""
        sql = (
            "SELECT COUNT(*) FROM participations"
            " WHERE param NOT LIKE '" + WIN_PARAM_PREFIX + "%'"
        )
        params: tuple = ()
        if account is not None:
            sql += " AND account = ?"
            params = (account,)
        with self._lock:
            return int(self._db().execute(sql, params).fetchone()[0])

    def clear(self) -> int:
        """清空所有账号的已参与抽奖记录并立即提交。

        只清除参与记录；中奖消息去重记录（win:<msg_id>）与回扫水位线、
        加入时间日志保留，避免清除后回扫把已提醒过的旧中奖消息再推送一次。
        返回清除的参与记录条数。
        """
        conn = self._db()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "DELETE FROM participations WHERE param NOT LIKE '"
                    + WIN_PARAM_PREFIX
                    + "%'"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return max(0, cur.rowcount or 0)

    def bots_for(self, account: str) -> List[str]:
        """该账号有过交互记录的全部 bot 用户名（按最近交互时间倒序）。

        中奖回扫用：账号参与过的 bot 才可能发来中奖私信。
        """
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT bot, MAX(ts) AS last_ts, MAX(rowid) AS last_row"
                    " FROM participations WHERE account = ?"
                    " GROUP BY bot_key"
                    " ORDER BY last_ts DESC, last_row DESC",
                    (account,),
                )
                .fetchall()
            )
        return [str(row["bot"]) for row in rows if str(row["bot"] or "").strip()]

    # ---- 中奖回扫水位线 ----

    def win_watermark(self, account: str, bot: str) -> Optional[int]:
        """该账号 × bot 上次中奖回扫扫到的消息 id，从未扫过返回 None。"""
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT msg_id FROM win_watermarks WHERE account = ? AND bot_key = ?",
                    (account, bot.lower()),
                )
                .fetchone()
            )
        return int(row["msg_id"]) if row is not None else None

    def set_win_watermark(self, account: str, bot: str, msg_id: int) -> None:
        """推进中奖回扫水位线到 msg_id（仅前进不后退）。"""
        with self._lock:
            self._db().execute(
                "INSERT INTO win_watermarks (account, bot_key, msg_id)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(account, bot_key) DO UPDATE SET"
                " msg_id = MAX(msg_id, excluded.msg_id)",
                (account, bot.lower(), int(msg_id)),
            )

    # ---- 加入频道时间日志（滚动配额） ----

    def join_events(self, account: str, window: int = JOIN_LOG_WINDOW) -> List[float]:
        """该账号最近 window 秒内的加入时间（epoch 秒，升序），用于滚动配额。"""
        cutoff = time.time() - window
        conn = self._db()
        with self._lock:
            rows = conn.execute(
                "SELECT ts FROM join_log WHERE account = ? AND ts >= ? ORDER BY ts",
                (account, cutoff),
            ).fetchall()
            conn.execute(
                "DELETE FROM join_log WHERE account = ? AND ts < ?", (account, cutoff)
            )
        return [float(row["ts"]) for row in rows]

    def add_join_event(self, account: str, ts: Optional[float] = None) -> None:
        """记录一次加入尝试的时间（配额按尝试计，失败也算）。"""
        conn = self._db()
        cutoff = time.time() - JOIN_LOG_WINDOW
        with self._lock:
            conn.execute(
                "INSERT INTO join_log (account, ts) VALUES (?, ?)",
                (account, float(ts or time.time())),
            )
            conn.execute(
                "DELETE FROM join_log WHERE account = ? AND ts < ?", (account, cutoff)
            )
            conn.execute(
                "DELETE FROM join_log WHERE account = ? AND rowid NOT IN ("
                " SELECT rowid FROM join_log WHERE account = ?"
                " ORDER BY ts DESC, rowid DESC LIMIT ?)",
                (account, account, MAX_JOIN_EVENTS_PER_ACCOUNT),
            )
