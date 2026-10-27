"""组合任务新增子任务类型（自动抽奖/批量退频道）的 spec 构建与 runner 纯函数测试。"""

import json
from pathlib import Path

from tg_signer.giveaway_runner import engine_kwargs_from_spec
from tg_signer.leave_runner import filter_candidates
from tg_signer.webui import task_panel as sign_mod
from tg_signer.webui.sign import TaskRunnerBlock, build_combo_sub_spec


def test_build_combo_sub_spec_leave(tmp_path):
    """批量退频道子任务：优先按任务名读取任务配置；「默认配置」回退全局配置。"""
    from tg_signer.webui.auto_leave import (
        UNSELECT_KEYWORDS,
        save_config,
        save_leave_tasks,
    )

    save_config(tmp_path, {"days": 14, "include_groups": True, "skip_pinned": False})
    # 无同名任务 → 回退全局配置
    spec = build_combo_sub_spec(
        "alice",
        {"type": "批量退频道 (auto leave)", "task": "默认配置"},
        tmp_path,
    )
    assert spec is not None
    assert spec["module"] == "tg_signer.leave_runner"
    assert spec["label"] == "批量退频道 (auto leave) · 默认配置"
    inner = spec["spec"]
    assert inner["account"] == "alice"
    assert inner["session_dir"] == str(tmp_path)
    assert inner["days"] == 14
    assert inner["include_groups"] is True
    assert inner["skip_pinned"] is False
    assert inner["exclude_keywords"] == list(UNSELECT_KEYWORDS)

    # 按任务名命中 → 使用该任务自己的配置
    save_leave_tasks(
        tmp_path,
        [
            {
                "id": "leave_test1",
                "name": "夜世轩任务",
                "accounts": ["alice"],
                "days": 30,
                "include_groups": False,
                "skip_pinned": True,
                "keywords": ["抽奖", "榜"],
                "auto_leave": False,
            }
        ],
    )
    spec = build_combo_sub_spec(
        "alice",
        {"type": "批量退频道 (auto leave)", "task": "夜世轩任务"},
        tmp_path,
    )
    assert spec is not None
    inner = spec["spec"]
    assert inner["days"] == 30
    assert inner["include_groups"] is False
    assert inner["scan_types"] == "channel"  # 旧任务 include_groups=False → 仅频道
    assert inner["include_keywords"] == []
    assert inner["skip_pinned"] is True
    assert inner["exclude_keywords"] == ["抽奖", "榜"]
    assert inner["auto_leave"] is False  # 仅扫描，不自动退出


def test_scan_types_of_mapping():
    """扫描范围推导：显式 scan_types 优先；旧 include_groups 布尔回退推导。"""
    from tg_signer.webui.auto_leave import _scan_types_of

    assert _scan_types_of({"scan_types": "group"}) == "group"
    assert _scan_types_of({"scan_types": "both", "include_groups": False}) == "both"
    assert _scan_types_of({"include_groups": True}) == "both"
    assert _scan_types_of({}) == "channel"
    assert _scan_types_of({"scan_types": "非法值"}) == "channel"


def test_build_combo_sub_spec_giveaway(tmp_path):
    """自动抽奖子任务：按任务名加载抽奖任务配置。"""
    store_path = tmp_path / "giveaway_tasks.json"
    store_path.write_text(
        json.dumps(
            {
                "seq": 2,
                "tasks": {
                    "5": {
                        "accounts": ["alice"],
                        "chats": ["-100123", "@chan"],
                        "session_dir": ".",
                        "keywords": ["上海"],
                        "auto_scan": True,
                        "auto_join": True,
                        "ignore_history": True,
                        "max_joins": None,
                        "delay_min": 3,
                        "delay_max": 10,
                        "confirm_wait": 8,
                        "rescue_interval_min": 0,
                        "rescue_limit": 10,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    spec = build_combo_sub_spec(
        "alice", {"type": "自动抽奖 (giveaway run)", "task": "5"}, tmp_path
    )
    assert spec is not None
    assert spec["module"] == "tg_signer.giveaway_runner"
    inner = spec["spec"]
    assert inner["account"] == "alice"
    assert inner["workdir"] == str(tmp_path)
    assert inner["task"] == "5"
    assert inner["chats"] == ["-100123", "@chan"]
    assert inner["keywords"] == ["上海"]
    assert inner["delay_min"] == 3 and inner["delay_max"] == 10

    # 任务不存在 → None
    assert (
        build_combo_sub_spec(
            "alice", {"type": "自动抽奖 (giveaway run)", "task": "999"}, tmp_path
        )
        is None
    )


def test_build_combo_sub_spec_cli_types(tmp_path):
    """原有 CLI 型子任务的参数构建不变。"""
    spec = build_combo_sub_spec(
        "bob", {"type": "签到 (run-once)", "task": "任务A"}, tmp_path
    )
    assert spec == {
        "label": "签到 (run-once) · 任务A",
        "args": ["-w", str(tmp_path), "-a", "bob", "run-once", "任务A"],
    }
    assert (
        build_combo_sub_spec("bob", {"type": "未知类型", "task": "x"}, tmp_path) is None
    )


def test_leave_runner_filter_candidates():
    rows = [
        {"title": "普通频道A", "key": "1"},
        {"title": "某抽奖频道", "key": "2"},
        {"title": "某某榜", "key": "3"},
        {"title": "普通群B", "key": "4"},
    ]
    out = filter_candidates(rows, ["抽奖", "榜"])
    assert [r["key"] for r in out] == ["1", "4"]
    assert filter_candidates(rows, []) == rows
    assert filter_candidates(rows, ["不存在词"])[0]["key"] in {"1", "2", "3", "4"}


def test_combo_runonce_lists_history_tasks(monkeypatch):
    """签到 (run-once) 的组合任务下拉应列出 signs/ 下所有签到任务（含从未执行过的），
    并合并历史执行过的任务名，附账号/频道摘要。"""
    monkeypatch.setattr(
        sign_mod,
        "load_task_history",
        lambda: [
            {
                "task_type": "签到 (run-once)",
                "task_name": "旧任务",
                "accounts": "",
            },
            {"task_type": "签到 (run 定时)", "task_name": "定时任务"},
            {"task_type": "自动抽奖 (giveaway run)", "task_name": "3"},
            {"task_type": "签到 (run-once)", "task_name": "my-sign", "accounts": ""},
            {
                "task_type": "签到 (run-once)",
                "task_name": "旧任务",
                "accounts": "alice, bob",
            },
        ],
    )
    monkeypatch.setattr(sign_mod, "load_task_accounts", dict)
    monkeypatch.setattr(
        sign_mod, "list_task_names", lambda kind, wd: ["my-sign", "new-task"]
    )

    class _FakeEntry:
        payload = {
            "chats": [
                {"name": "频道A"},
                {"name": None, "chat_id": -100123},
            ]
        }

    monkeypatch.setattr(sign_mod, "load_config", lambda *a, **k: _FakeEntry())

    # 跳过 __init__（不建 UI），仅用方法逻辑
    blk = TaskRunnerBlock.__new__(TaskRunnerBlock)

    names = blk._get_combo_task_names("签到 (run-once)")
    assert names == {
        "my-sign": "my-sign｜账号：未绑定｜频道：频道A、-100123",
        "new-task": "new-task｜账号：未绑定｜频道：频道A、-100123",
        "旧任务": "旧任务｜账号：alice, bob｜频道：频道A、-100123",
    }  # 配置里的任务按名称排序在前，仅出现在历史中的任务排后

    # 有账号绑定的任务优先显示绑定账号（而非历史执行账号）
    monkeypatch.setattr(
        sign_mod,
        "load_task_accounts",
        lambda: {"签到 (run-once)|my-sign": ["alice"]},
    )
    names = blk._get_combo_task_names("签到 (run-once)")
    assert names["my-sign"].startswith("my-sign｜账号：alice")

    # 配置读取失败时频道显示「未设置」，不影响任务列出
    def _boom(*a, **k):
        raise FileNotFoundError("x")

    monkeypatch.setattr(sign_mod, "load_config", _boom)
    names = blk._get_combo_task_names("签到 (run-once)")
    assert names["my-sign"].endswith("频道：未设置")

    # 退频道类型：任务列表模式，仅列出「自动退出」任务（排除仅扫描任务）
    monkeypatch.setattr(
        sign_mod,
        "_load_leave_tasks",
        lambda _wd: [
            {
                "id": "leave_t1",
                "name": "夜世轩任务",
                "accounts": ["alice"],
                "days": 30,
                "auto_leave": False,
            },
            {
                "id": "leave_t2",
                "name": "自动退任务",
                "accounts": ["bob"],
                "days": 14,
                "auto_leave": True,
            },
        ],
    )
    opts = blk._get_combo_task_names("批量退频道 (auto leave)")
    assert opts == {
        # 序号与「批量退频道」tab 一致：按创建顺序编号（leave_t2 是第 2 个任务），
        # 与自动抽奖风格一致，不带井号
        "自动退任务": "2｜自动退任务（14天｜自动退出｜账号：bob）",
    }  # 仅自动退出任务列出，仅扫描任务被排除
    # 无任务时返回空（无兜底项）
    monkeypatch.setattr(sign_mod, "_load_leave_tasks", lambda _wd: [])
    assert blk._get_combo_task_names("批量退频道 (auto leave)") == {}


def test_combo_giveaway_options_show_accounts_chats(monkeypatch):
    """自动抽奖的任务下拉应显示「编号｜账号｜监控对象（优先名称）」，选中值仍是任务编号。"""
    monkeypatch.setattr(
        sign_mod,
        "_load_giveaway_store",
        lambda _wd: {
            "tasks": {
                "3": {"accounts": ["alice", "bob"], "chats": ["@c1", "-100x"]},
                "7": {"accounts": [], "chats": []},
            }
        },
    )
    # 已缓存的 chat_id 显示名称，未缓存的保留原 id
    monkeypatch.setattr(
        sign_mod,
        "_chat_title_map",
        lambda _wd: {"-100x": "鲸鱼群组"},
    )

    blk = TaskRunnerBlock.__new__(TaskRunnerBlock)

    opts = blk._get_combo_task_names("自动抽奖 (giveaway run)")
    assert opts["3"] == "3｜账号：alice、bob｜监控：@c1、鲸鱼群组"
    assert opts["7"] == "7｜账号：未设置｜监控：未设置"
    # 超过 5 个监控对象截断显示
    monkeypatch.setattr(
        sign_mod,
        "_load_giveaway_store",
        lambda _wd: {
            "tasks": {"2": {"accounts": ["a"], "chats": [f"@c{i}" for i in range(7)]}}
        },
    )
    opts = blk._get_combo_task_names("自动抽奖 (giveaway run)")
    assert opts["2"].endswith("等7个")


def test_combo_sub_accounts_resolution(monkeypatch):
    """组合子任务使用自身绑定的账号：抽奖取任务配置、退频道取任务账号、
    签到 run-once 取任务绑定（无绑定时回退最近一次执行记录）。"""
    monkeypatch.setattr(
        sign_mod,
        "_load_giveaway_store",
        lambda _wd: {"tasks": {"6": {"accounts": ["鲸鱼cinq"]}, "8": {}}},
    )
    monkeypatch.setattr(
        sign_mod,
        "_load_leave_tasks",
        lambda _wd: [
            {"name": "仅扫描任务", "accounts": ["alice"], "auto_leave": False},
            {"name": "退频道任务", "accounts": ["bob"], "auto_leave": True},
        ],
    )
    monkeypatch.setattr(
        sign_mod, "_load_leave_config", lambda _wd: {"accounts": ["legacy"]}
    )
    monkeypatch.setattr(
        sign_mod,
        "load_task_accounts",
        lambda: {"签到 (run-once)|任务A": ["alice", "bob"]},
    )
    monkeypatch.setattr(
        sign_mod,
        "load_task_history",
        lambda: [
            {"task_type": "签到 (run-once)", "task_name": "任务B", "accounts": ""},
            {
                "task_type": "签到 (run-once)",
                "task_name": "任务B",
                "accounts": "carol, dave",
            },
        ],
    )

    # 抽奖：取任务配置中的账号
    assert sign_mod._combo_sub_accounts(
        {"type": "自动抽奖 (giveaway run)", "task": "6"}, None
    ) == ["鲸鱼cinq"]
    # 抽奖任务未设置账号 → 空
    assert (
        sign_mod._combo_sub_accounts(
            {"type": "自动抽奖 (giveaway run)", "task": "8"}, None
        )
        == []
    )
    # 退频道：按任务名取账号
    assert sign_mod._combo_sub_accounts(
        {"type": "批量退频道 (auto leave)", "task": "退频道任务"}, None
    ) == ["bob"]
    # 退频道任务不存在 → 回退旧版全局配置
    assert sign_mod._combo_sub_accounts(
        {"type": "批量退频道 (auto leave)", "task": "不存在"}, None
    ) == ["legacy"]
    # 签到 run-once：优先任务绑定账号
    assert sign_mod._combo_sub_accounts(
        {"type": "签到 (run-once)", "task": "任务A"}, None
    ) == ["alice", "bob"]
    # 无绑定时回退最近一次执行记录（最新记录在前，取第一个命中的）
    assert sign_mod._combo_sub_accounts(
        {"type": "签到 (run-once)", "task": "任务B"}, None
    ) == ["carol", "dave"]
    # 完全无来源 → 空
    assert (
        sign_mod._combo_sub_accounts({"type": "签到 (run-once)", "task": "任务C"}, None)
        == []
    )


def test_giveaway_runner_engine_kwargs():
    kwargs = engine_kwargs_from_spec(
        {
            "account": "alice",
            "chats": ["-100123"],
            "keywords": ["上海", "红包"],
            "delay_min": 10,
            "delay_max": 3,
            "max_joins": "",
            "confirm_wait": 0,
            "rescue_interval_min": 30,
            "rescue_limit": 0,
        }
    )
    assert kwargs["accounts"] == ["alice"]
    assert kwargs["delay_range"] == (3.0, 10.0)  # 上下限自动归序
    assert kwargs["max_joins"] is None  # 空值 → 无上限
    assert kwargs["confirm_wait"] == 0
    assert kwargs["rescue_interval"] == 1800
    assert kwargs["rescue_limit"] == 1  # 最小 1
    # 旧任务未配置限流保护：保持 None，由引擎使用默认常量
    assert kwargs["join_pace_range"] is None
    assert kwargs["resolve_pace_range"] is None
    assert kwargs["join_hourly_cap"] is None
    assert kwargs["join_daily_cap"] is None
    assert kwargs["join_flood_buffer"] is None


def test_giveaway_runner_limits_from_spec():
    """任务级限流保护参数（WebUI 配置）要透传到引擎。"""
    kwargs = engine_kwargs_from_spec(
        {
            "account": "alice",
            "join_pace_min": 60,
            "join_pace_max": 30,
            "resolve_pace_min": 1,
            "resolve_pace_max": 3,
            "join_hourly_cap": 4,
            "join_daily_cap": 12,
            "join_flood_buffer": 0,
        }
    )
    assert kwargs["join_pace_range"] == (60.0, 30.0)  # 引擎内部再归序
    assert kwargs["resolve_pace_range"] == (1.0, 3.0)
    assert kwargs["join_hourly_cap"] == 4
    assert kwargs["join_daily_cap"] == 12
    assert kwargs["join_flood_buffer"] == 0


def test_giveaway_runner_engine_kwargs_multi_accounts():
    """WebUI 抽奖任务把账号列表传给子进程（而非单个 account）。"""
    kwargs = engine_kwargs_from_spec(
        {"accounts": ["alice", " bob ", ""], "chats": ["-100123"]}
    )
    assert kwargs["accounts"] == ["alice", "bob"]


def test_giveaway_process_spec_and_stats(tmp_path):
    """子进程 spec 构建与统计回报解析：WebUI 只持有展示所需的快照。"""
    from tg_signer.webui.giveaway_process import GiveawayProcess

    proc = GiveawayProcess("t1", tmp_path)
    spec = proc._build_spec(
        {
            "accounts": ["alice"],
            "chats": [123],
            "keywords": ["上海"],
            "session_dir": ".sessions",
            "max_joins": "5",
            "delay_min": 1,
            "delay_max": 2,
            "auto_scan": False,
            "join_pace_min": 20,
            "join_pace_max": 60,
            "join_hourly_cap": 8,
            "join_daily_cap": 20,
            "join_flood_buffer": 15,
            "resolve_pace_min": 2,
            "resolve_pace_max": 5,
        }
    )
    assert spec["accounts"] == ["alice"]
    assert spec["chats"] == ["123"]
    assert spec["task"] == "t1"
    assert spec["session_dir"] == str(Path(".sessions"))
    assert spec["auto_scan"] is False
    assert spec["max_joins"] == "5"
    # 限流保护配置原样透传（缺省 None → 引擎用默认值）
    assert spec["join_pace_min"] == 20 and spec["join_pace_max"] == 60
    assert spec["join_hourly_cap"] == 8 and spec["join_daily_cap"] == 20
    assert spec["join_flood_buffer"] == 15
    assert spec["resolve_pace_min"] == 2 and spec["resolve_pace_max"] == 5

    proc._apply_stats(
        {
            "joined": 3,
            "by_account": {"alice": 3},
            "by_chat": {"alice": {"-100123": 2}},
            "progress": "扫描中",
        }
    )
    assert proc.joined == 3
    assert proc.joined_by_account == {"alice": 3}
    assert proc.joined_by_account_chat == {"alice": {"-100123": 2}}
    assert proc.progress_text() == "扫描中"
    assert not proc.running


def test_giveaway_process_reports_stats_to_caller(tmp_path):
    """统计回报会回调上层，便于参与明细边跑边落盘（不依赖运行结束）。"""
    import asyncio

    from tg_signer.webui.giveaway_process import GiveawayProcess

    async def _run() -> list:
        proc = GiveawayProcess("t1", tmp_path)
        seen: list = []
        proc._on_stats = seen.append
        proc._loop = asyncio.get_running_loop()
        proc._apply_stats(
            {"joined": 2, "by_account": {"alice": 2}, "by_chat": {}, "progress": ""}
        )
        await asyncio.sleep(0)
        return seen

    seen = asyncio.run(_run())
    assert seen and seen[0]["joined"] == 2


def test_giveaway_process_finalizes_without_events_file(tmp_path):
    """子进程没来得及写 events 文件就退出时，也要触发结束回调。"""

    class _DeadProc:
        def poll(self):
            return 0

    from tg_signer.webui.giveaway_process import GiveawayProcess

    proc = GiveawayProcess("t1", tmp_path)
    proc.proc = _DeadProc()
    proc._events_file = tmp_path / "missing.jsonl"
    called = []
    proc._on_finish = lambda: called.append(True)
    proc._read_events()
    assert called == [True]


def test_make_log_rotates_instead_of_dropping(tmp_path):
    """日志超过上限时滚动为 .1，而不是静默停止写入（标准库 RotatingFileHandler）。"""
    from tg_signer.logger import make_task_logger

    log = make_task_logger(
        str(tmp_path),
        task_name="1",
        log_file="giveaway.log",
        max_bytes=10,
        backup_count=1,
    )
    log("第一条")
    log_file = tmp_path / "logs" / "giveaway.log"
    assert log_file.exists() and log_file.stat().st_size >= 10

    log("第二条")
    backup = tmp_path / "logs" / "giveaway.log.1"
    assert backup.exists()
    assert "第二条" in log_file.read_text(encoding="utf-8")


def test_drain_output_collects_logs_without_dashboard_polling():
    """组合任务在非签到 tab 运行、主控台已关闭时，输出仍要转存到共享日志缓冲，
    否则进度页只能显示「暂无日志输出」。"""
    import queue

    saved_runs = dict(TaskRunnerBlock.runs)
    saved_lines = list(TaskRunnerBlock.output_lines)
    saved_seq = TaskRunnerBlock._line_seq
    try:
        TaskRunnerBlock.runs.clear()
        TaskRunnerBlock.output_lines.clear()
        q: queue.Queue = queue.Queue()
        q.put("[组合A:alice] 开始签到")
        q.put("[组合A:alice] 签到完成")
        TaskRunnerBlock.runs["rid"] = {"queue": q}

        TaskRunnerBlock.drain_output()

        assert list(TaskRunnerBlock.output_lines) == [
            "[组合A:alice] 开始签到",
            "[组合A:alice] 签到完成",
        ]
        assert q.empty()
    finally:
        TaskRunnerBlock.runs.clear()
        TaskRunnerBlock.runs.update(saved_runs)
        TaskRunnerBlock.output_lines.clear()
        TaskRunnerBlock.output_lines.extend(saved_lines)
        TaskRunnerBlock._line_seq = saved_seq
