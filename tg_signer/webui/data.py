import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from tg_signer.config import BaseJSONConfig, MonitorConfig, SignConfigV3
from tg_signer.sign_record_store import SignRecordStore

ConfigKind = Literal["signer", "monitor"]

CONFIG_META: dict[ConfigKind, Tuple[str, type[BaseJSONConfig]]] = {
    "signer": ("signs", SignConfigV3),
    "monitor": ("monitors", MonitorConfig),
}

DEFAULT_WORKDIR = Path(os.environ.get("TG_SIGNER_WORKDIR", ".signer"))
LOG_DIR = Path("logs")
DEFAULT_LOG_FILE = LOG_DIR / "tg-signer.log"


@dataclass
class ConfigEntry:
    name: str
    path: Path
    updated_from_old: bool
    payload: Dict[str, Any]
    cfg: BaseJSONConfig


@dataclass
class UserInfo:
    user_id: str
    data: Dict[str, Any]
    path: Path
    latest_chats: List[Dict[str, Any]] = None


@dataclass
class SignRecord:
    task: str
    user_id: Optional[str]
    records: List[Tuple[str, str]]
    path: Path


def get_workdir(workdir: Optional[Path | str] = None) -> Path:
    base = Path(workdir) if workdir else DEFAULT_WORKDIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def _config_root(kind: ConfigKind, workdir: Optional[Path | str]) -> Path:
    base = get_workdir(workdir)
    dir_name, _ = CONFIG_META[kind]
    return base / dir_name


def _config_path(kind: ConfigKind, name: str, workdir: Optional[Path | str]) -> Path:
    return _config_root(kind, workdir) / name / "config.json"


def list_task_names(
    kind: ConfigKind, workdir: Optional[Path | str] = None
) -> List[str]:
    root = _config_root(kind, workdir)
    if not root.is_dir():
        return []
    return sorted([p.name for p in root.iterdir() if p.is_dir()])


def load_config(
    kind: ConfigKind, name: str, workdir: Optional[Path | str] = None
) -> ConfigEntry:
    config_file = _config_path(kind, name, workdir)
    if not config_file.is_file():
        raise FileNotFoundError(f"配置不存在: {config_file}")
    cfg_cls = CONFIG_META[kind][1]
    with open(config_file, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    loaded = cfg_cls.load(raw)
    if loaded is None:
        raise ValueError(f"无法解析配置: {config_file}")
    cfg, from_old = loaded
    if from_old:
        # keep the latest structure aligned with current schema
        save_config(kind, name, cfg, workdir=workdir)
    payload = cfg.to_jsonable()
    return ConfigEntry(
        name=name, path=config_file, updated_from_old=from_old, payload=payload, cfg=cfg
    )


def save_config(
    kind: ConfigKind,
    name: str,
    content: Dict[str, Any] | str | BaseJSONConfig,
    workdir: Optional[Path | str] = None,
) -> Path:
    cfg_cls = CONFIG_META[kind][1]
    if isinstance(content, BaseJSONConfig):
        cfg = content
    else:
        data = json.loads(content) if isinstance(content, str) else content
        loaded = cfg_cls.load(data)
        if loaded is None:
            raise ValueError("配置校验失败")
        cfg, _ = loaded
    config_file = _config_path(kind, name, workdir)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as fp:
        json.dump(cfg.to_jsonable(), fp, ensure_ascii=False, indent=2)
    return config_file


def delete_config(
    kind: ConfigKind, name: str, workdir: Optional[Path | str] = None
) -> Path:
    config_file = _config_path(kind, name, workdir)
    if not config_file.exists():
        raise FileNotFoundError(f"配置不存在: {config_file}")
    config_file.unlink()
    parent = config_file.parent
    # remove empty directories only; keep records if present
    try:
        next(parent.iterdir())
    except StopIteration:
        parent.rmdir()
    return config_file


def load_user_infos(workdir: Optional[Path | str] = None) -> List[UserInfo]:
    base = get_workdir(workdir)
    users_dir = base / "users"
    if not users_dir.is_dir():
        return []
    entries: List[UserInfo] = []
    for user_dir in sorted(
        [p for p in users_dir.iterdir() if p.is_dir()], key=lambda p: p.name
    ):
        me_file = user_dir / "me.json"
        if not me_file.is_file():
            continue
        with open(me_file, "r", encoding="utf-8") as fp:
            try:
                data = json.load(fp)
            except json.JSONDecodeError:
                continue

        latest_chats = []
        chats_file = user_dir / "latest_chats.json"
        if chats_file.is_file():
            with open(chats_file, "r", encoding="utf-8") as fp:
                try:
                    latest_chats = json.load(fp)
                except json.JSONDecodeError:
                    pass

        entries.append(
            UserInfo(
                user_id=user_dir.name,
                data=data,
                path=me_file,
                latest_chats=latest_chats,
            )
        )
    return entries


def _accounts_state_path(workdir: Optional[Path | str] = None) -> Path:
    return get_workdir(workdir) / "webui_accounts.json"


def _read_accounts_state(path: Path) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_accounts_state(path: Path, state: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)


def load_disabled_accounts(workdir: Optional[Path | str] = None) -> set:
    """读取被停用的账户名集合（webui_accounts.json）。"""
    disabled = _read_accounts_state(_accounts_state_path(workdir)).get("disabled")
    return set(disabled) if isinstance(disabled, list) else set()


def set_account_enabled(
    name: str, enabled: bool, workdir: Optional[Path | str] = None
) -> None:
    """启用/停用账户，停用名单持久化到 webui_accounts.json（保留其他字段）。"""
    path = _accounts_state_path(workdir)
    state = _read_accounts_state(path)
    disabled = set(state.get("disabled") or [])
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    state["disabled"] = sorted(disabled)
    _write_accounts_state(path, state)


def rename_account_state(
    old: str, new: str, workdir: Optional[Path | str] = None
) -> None:
    """账户重命名后，同步更新 webui_accounts.json 中的 disabled 与 order 记录。"""
    path = _accounts_state_path(workdir)
    state = _read_accounts_state(path)
    for field in ("disabled", "order"):
        values = state.get(field)
        if isinstance(values, list) and old in values:
            values[values.index(old)] = new
            state[field] = values
    _write_accounts_state(path, state)


def load_account_order(workdir: Optional[Path | str] = None) -> List[str]:
    """读取账户展示顺序（webui_accounts.json 的 order 字段）。"""
    order = _read_accounts_state(_accounts_state_path(workdir)).get("order")
    return list(order) if isinstance(order, list) else []


def set_account_order(
    order: List[str], workdir: Optional[Path | str] = None
) -> None:
    """保存账户展示顺序。"""
    path = _accounts_state_path(workdir)
    state = _read_accounts_state(path)
    state["order"] = list(order)
    _write_accounts_state(path, state)


def apply_account_order(
    names: List[str], workdir: Optional[Path | str] = None
) -> List[str]:
    """按已保存的顺序排列账户名，未登记的账户按字母序排在后面。"""
    order = load_account_order(workdir)
    names = list(names)
    known = [n for n in order if n in names]
    rest = sorted(n for n in names if n not in order)
    return known + rest


def account_identities(
    names: List[str],
    workdir: Optional[Path | str] = None,
    session_dir: Path | str = ".",
) -> Dict[str, Dict[str, object]]:
    """读取每个账户 .session 内的 user_id，并从 users/<id>/me.json 提取 Telegram 身份。

    返回 {账户名: {"user_id": int, "name": str, "username": str}}，
    只包含能解析出 user_id 且 me.json 存在的账户。
    """
    import json as _json
    import sqlite3

    base = Path(session_dir)
    users_root = get_workdir(workdir) / "users"
    mapping: Dict[str, Dict[str, object]] = {}
    for name in names:
        session_file = base / f"{name}.session"
        user_id = None
        try:
            con = sqlite3.connect(f"file:{session_file.as_posix()}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
                user_id = row[0] if row else None
            finally:
                con.close()
        except Exception:
            continue
        if user_id is None:
            continue
        me_file = users_root / str(user_id) / "me.json"
        try:
            data = _json.loads(me_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        name_full = (data.get("first_name") or "").strip()
        if data.get("last_name"):
            name_full = f"{name_full} {data['last_name']}".strip()
        mapping[name] = {
            "user_id": user_id,
            "name": name_full,
            "username": data.get("username") or "",
        }
    return mapping


EXPORT_FORMAT_VERSION = 1


def export_all_configs(workdir: Optional[Path | str] = None) -> bytes:
    """导出全部 WebUI 可迁移配置（签到/监控配置、账户状态）为 JSON 字节。

    任务历史（随机发言任务列表）不参与导出：历史任务绑定具体设备，无需随备份迁移。
    """
    base = get_workdir(workdir)
    payload: Dict[str, Any] = {
        "format": "tg-signer-plus-configs",
        "version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signer": {},
        "monitor": {},
    }
    for kind in ("signer", "monitor"):
        for name in list_task_names(kind, base):
            try:
                entry = load_config(kind, name, workdir=base)
            except Exception:
                continue
            payload[kind][name] = entry.payload

    # 任务历史（随机发言任务列表）不导出：绑定具体设备，无需迁移

    # 账户状态（停用名单 + 顺序）
    state = _read_accounts_state(_accounts_state_path(base))
    account_state = {}
    if isinstance(state.get("disabled"), list):
        account_state["disabled"] = state["disabled"]
    if isinstance(state.get("order"), list):
        account_state["order"] = state["order"]
    if account_state:
        payload["account_state"] = account_state

    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def import_all_configs(
    raw: bytes | str, workdir: Optional[Path | str] = None
) -> Dict[str, int]:
    """导入导出的配置 JSON，与现有配置合并（重名覆盖），返回各类导入数量。

    任务历史（随机发言任务列表）不参与导入，即使备份文件中携带也直接忽略。
    """
    base = get_workdir(workdir)
    data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("format") != "tg-signer-plus-configs":
        raise ValueError("文件格式不正确：不是 tg-signer-plus 导出的配置文件")

    counts: Dict[str, int] = {"signer": 0, "monitor": 0, "random_chat_tasks": 0}
    for kind in ("signer", "monitor"):
        items = data.get(kind)
        if not isinstance(items, dict):
            continue
        for name, content in items.items():
            save_config(kind, str(name), content, workdir=base)
            counts[kind] += 1

    # 任务历史（随机发言任务列表）不导入：绑定具体设备，无需迁移

    account_state = data.get("account_state")
    if isinstance(account_state, dict):
        state = _read_accounts_state(_accounts_state_path(base))
        if isinstance(account_state.get("disabled"), list):
            state["disabled"] = account_state["disabled"]
        if isinstance(account_state.get("order"), list):
            state["order"] = account_state["order"]
        _write_accounts_state(_accounts_state_path(base), state)

    return counts


def export_backup(workdir: Optional[Path | str] = None) -> bytes:
    """导出备份 ZIP（不含 session 登录凭据，迁移账号需另行复制 session 文件）。

    - configs.json：签到/监控配置、随机发言任务、账户状态（导入时走合并逻辑）
    - signer_data/：users 缓存（me.json / latest_chats.json）+ 批量退频道配置
    """
    import io
    import zipfile

    buf = io.BytesIO()
    base = get_workdir(workdir)

    def _add(p: Path) -> None:
        try:
            zf.write(p, f"signer_data/{p.relative_to(base).as_posix()}")
        except OSError:
            pass

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("configs.json", export_all_configs(workdir))
        # users 缓存（最近聊天列表、账户身份资料）
        users_dir = base / "users"
        if users_dir.is_dir():
            for p in sorted(users_dir.rglob("*")):
                if p.is_file() and p.name in ("me.json", "latest_chats.json"):
                    _add(p)
        # 批量退频道排除关键字配置
        auto_leave_cfg = base / "auto_leave_config.json"
        if auto_leave_cfg.is_file():
            _add(auto_leave_cfg)
    return buf.getvalue()


def import_backup(raw: bytes, workdir: Optional[Path | str] = None) -> Dict[str, int]:
    """导入备份（ZIP 或纯配置 JSON），返回各类导入/还原数量。"""
    if raw[:2] != b"PK":
        counts = import_all_configs(raw, workdir)
        counts["session_files"] = 0
        counts["signer_data_files"] = 0
        return counts

    import io
    import zipfile

    counts: Dict[str, int] = {
        "signer": 0,
        "monitor": 0,
        "random_chat_tasks": 0,
        "session_files": 0,
        "signer_data_files": 0,
    }
    base = get_workdir(workdir)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        # 1) 先还原 .signer 数据镜像（users 缓存、批量退频道配置等）
        for name in names:
            if not name.startswith("signer_data/"):
                continue
            rel = name[len("signer_data/"):]
            if not rel or rel.endswith("/"):
                continue
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
            counts["signer_data_files"] += 1
        # 2) 再走配置合并（随机发言任务冲突重编号、账户状态恢复）
        if "configs.json" in names:
            curated = import_all_configs(zf.read("configs.json"), workdir)
            counts.update({k: v for k, v in curated.items() if k in counts})
        # 兼容旧备份：忽略 sessions/ 目录（不再还原 session 凭据）
    return counts


def _record_target(path: Path, signs_root: Path) -> Tuple[str, Optional[str]]:
    relative_parts = path.relative_to(signs_root).parts
    task = relative_parts[0]
    user_id = None
    if len(relative_parts) > 2:
        user_id = relative_parts[1]
    return task, user_id


def load_sign_records(workdir: Optional[Path | str] = None) -> List[SignRecord]:
    base = get_workdir(workdir)
    signs_dir = base / "signs"
    records: List[SignRecord] = []
    existing_keys: set[tuple[str, Optional[str]]] = set()

    store = SignRecordStore(base)
    if store.db_path.is_file():
        for group in store.list_record_groups():
            key = (group.task_name, group.user_id)
            existing_keys.add(key)
            records.append(
                SignRecord(
                    task=group.task_name,
                    user_id=group.user_id,
                    records=group.records,
                    path=store.db_path,
                )
            )

    if not signs_dir.is_dir():
        return records

    for record_file in sorted(signs_dir.rglob("sign_record.json")):
        try:
            with open(record_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        task, user_id = _record_target(record_file, signs_dir)
        key = (task, user_id)
        # Prefer the migrated SQLite rows when both sources exist so the same
        # task/user pair does not appear twice in the UI.
        if key in existing_keys:
            continue
        items: Iterable[Tuple[str, str]] = (
            data.items() if isinstance(data, dict) else []
        )
        sorted_items = sorted(items, key=lambda kv: kv[0], reverse=True)
        records.append(
            SignRecord(
                task=task, user_id=user_id, records=sorted_items, path=record_file
            )
        )
    return records


def tail_file(path: Path, limit: int = 200) -> List[str]:
    if not path.is_file():
        return []
    if limit <= 0:
        return []

    buffer: deque[str] = deque()
    chunk_size = 8192

    # Read from the end in chunks to avoid loading large files entirely.
    with open(path, "rb") as fp:
        fp.seek(0, os.SEEK_END)
        position = fp.tell()
        leftover = b""
        while position > 0 and len(buffer) < limit:
            read_size = min(chunk_size, position)
            position -= read_size
            fp.seek(position)
            chunk = fp.read(read_size)
            data = chunk + leftover
            lines = data.split(b"\n")
            leftover = lines[0]
            for line in reversed(lines[1:]):
                buffer.appendleft(line.decode("utf-8", errors="ignore").rstrip("\r"))
                if len(buffer) >= limit:
                    break

        if len(buffer) < limit and leftover:
            buffer.appendleft(leftover.decode("utf-8", errors="ignore").rstrip("\r"))

    return list(buffer)


def list_log_files(log_dir: Optional[Path | str] = None) -> List[Path]:
    base = Path(log_dir) if log_dir else LOG_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.log") if p.is_file())


def _resolve_log_path(log_path: Optional[Path | str] = None) -> Path:
    if log_path:
        path = Path(log_path).expanduser()
        if not path.is_absolute() and path.parent == Path("."):
            return LOG_DIR / path
        return path
    return DEFAULT_LOG_FILE


def load_logs(
    limit: int = 200, log_path: Optional[Path | str] = None
) -> Tuple[Path, List[str]]:
    path = _resolve_log_path(log_path)
    return path, tail_file(path, limit=limit)
