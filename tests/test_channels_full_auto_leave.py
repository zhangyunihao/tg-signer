"""账号频道/群组已满（CHANNELS_TOO_MUCH）→ 消息提醒 + 自动退频道的回归测试。"""

import asyncio
from datetime import datetime, timedelta, timezone

from tg_signer.giveaway_core import GiveawayEngine
from tg_signer.webui import auto_leave, giveaway
from tg_signer.webui.messages import collect_messages


class _FullClient:
    """加入任何频道都回报频道数已满的假客户端。"""

    def __init__(self):
        self.calls = 0

    async def join_chat(self, target):
        self.calls += 1
        raise RuntimeError(
            "[400 CHANNELS_TOO_MUCH] You have joined too many channels/groups"
        )


def _engine(events, logs):
    engine = GiveawayEngine(state=None)
    engine.on_event = events.append
    engine.log = logs.append
    engine.join_pace_range = (0.0, 0.0)
    engine.resolve_pace_range = (0.0, 0.0)
    return engine


def test_join_reports_channels_full_once():
    events, logs = [], []
    engine = _engine(events, logs)
    client = _FullClient()
    assert asyncio.run(
        engine._join_single_target("alice", client, "@chan", "测试")
    ) == (False, False)
    assert client.calls == 1
    full = [e for e in events if e.get("type") == "channels_full"]
    assert len(full) == 1
    assert full[0]["account"] == "alice"
    assert "@chan" in full[0]["detail"]
    # 同一账号本轮不再重复回报，也不重复触发自动退频道
    assert asyncio.run(
        engine._join_single_target("alice", _FullClient(), "@chan2", "测试")
    ) == (False, False)
    assert len([e for e in events if e.get("type") == "channels_full"]) == 1


def test_monitor_join_channels_full_stops_retry():
    events, logs = [], []
    engine = _engine(events, logs)
    exc = RuntimeError("[400 CHANNELS_TOO_MUCH] You have joined too many channels")
    assert engine._monitor_join_error_handled("bob", "@chan", "bob|@chan", exc) is True
    assert "bob|@chan" in engine._join_cache
    assert [e["type"] for e in events] == ["channels_full"]


def test_create_auto_leave_task_defaults_and_dedupe(tmp_path):
    task = auto_leave.create_auto_leave_task(tmp_path, "alice", detail="加入 @chan")
    assert task is not None
    assert task["days"] == 1
    assert task["scan_types"] == "channel"
    assert task["auto_leave"] is True
    assert task["skip_pinned"] is True
    assert task["keywords"] == list(auto_leave.UNSELECT_KEYWORDS)
    assert task["accounts"] == ["alice"]
    assert task["trigger"] == "channels_full"
    assert task["status"] == "待执行"
    # 同账号已有排队/运行中的自动任务：不重复创建
    assert auto_leave.create_auto_leave_task(tmp_path, "alice") is None
    # 上个任务进入终态后可再次创建
    auto_leave._patch_leave_task(
        tmp_path, task["id"], status="完成：自动退出 2 个，待确认 0 个"
    )
    assert auto_leave.create_auto_leave_task(tmp_path, "alice") is not None
    assert len(auto_leave.load_leave_tasks(tmp_path)) == 2


def test_run_auto_leave_task_leaves_unprotected(tmp_path, monkeypatch):
    task = auto_leave.create_auto_leave_task(tmp_path, "alice", status="待执行")
    found = [
        {
            "key": "alice:1",
            "account": "alice",
            "chat_id": 1,
            "title": "抽奖频道",
            "type": "CHANNEL",
            "age_days": 3,
        },
        {
            "key": "alice:2",
            "account": "alice",
            "chat_id": 2,
            "title": "普通频道",
            "type": "CHANNEL",
            "age_days": 4,
        },
    ]
    calls = {}

    async def fake_scan(account, session_dir, days, scan_types, skip_pinned, log):
        calls.update(
            account=account,
            days=days,
            scan_types=scan_types,
            skip_pinned=skip_pinned,
        )
        return [dict(r) for r in found]

    async def fake_leave(account, session_dir, rows, log, should_stop=None):
        calls["left"] = [r["title"] for r in rows]
        return {r["key"] for r in rows}

    monkeypatch.setattr(auto_leave, "scan_stale_chats", fake_scan)
    monkeypatch.setattr(auto_leave, "_leave_rows", fake_leave)

    asyncio.run(auto_leave.run_auto_leave_task(tmp_path, tmp_path, task["id"]))

    saved = auto_leave.load_leave_tasks(tmp_path)[0]
    assert saved["status"] == "完成：自动退出 1 个，待确认 1 个"
    assert [r["title"] for r in saved["rows"]] == ["抽奖频道"]
    assert saved["start_time"]
    assert calls["days"] == 1
    assert calls["scan_types"] == "channel"
    assert calls["left"] == ["普通频道"]


def test_days_threshold_supports_fractional_values(tmp_path, monkeypatch):
    task = auto_leave.create_auto_leave_task(tmp_path, "alice", days=0.5)
    assert task["days"] == 0.5
    calls = {}

    async def fake_scan(account, session_dir, days, scan_types, skip_pinned, log):
        calls["days"] = days
        return []

    monkeypatch.setattr(auto_leave, "scan_stale_chats", fake_scan)
    asyncio.run(auto_leave.run_auto_leave_task(tmp_path, tmp_path, task["id"]))
    assert calls["days"] == 0.5

    assert auto_leave._to_days("0.5", 7) == 0.5
    assert auto_leave._to_days(None, 7) == 7
    assert auto_leave._fmt_days(1.5) == "1.5"
    assert auto_leave._fmt_days(2.0) == "2"


def test_as_utc_handles_pyrogram_message_dates():
    # Pyrogram 2.x 的 message.date 由 datetime.fromtimestamp(ts) 生成（本地时区 naive）
    ts = 1792000000
    assert auto_leave._as_utc(datetime.fromtimestamp(ts)) == datetime.fromtimestamp(
        ts, timezone.utc
    )
    # aware datetime 也能直接换算，不受本地时区影响
    aware = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
    assert auto_leave._as_utc(aware) == datetime.fromtimestamp(ts, timezone.utc)


def test_channels_full_alert_message_deduped(tmp_path):
    # client=None：顶部通知在无浏览器上下文时静默失败，不影响持久化
    giveaway._handle_channels_full_event(
        None, tmp_path, {"account": "alice", "detail": "加入 @a"}
    )
    giveaway._handle_channels_full_event(
        None, tmp_path, {"account": "alice", "detail": "加入 @b"}
    )
    alerts = giveaway.load_channels_full_alerts(tmp_path)
    assert len(alerts) == 1
    assert alerts[0]["detail"] == "加入 @b"

    msgs = [m for m in collect_messages(tmp_path) if m["tag"] == "频道已满"]
    assert len(msgs) == 1
    assert "alice" in msgs[0]["text"]
    assert "退频道任务" in msgs[0]["text"]
