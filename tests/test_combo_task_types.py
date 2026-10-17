"""组合任务新增子任务类型（自动抽奖/批量退频道）的 spec 构建与 runner 纯函数测试。"""

import json

from tg_signer.giveaway_runner import engine_kwargs_from_spec
from tg_signer.leave_runner import filter_candidates
from tg_signer.webui.sign import build_combo_sub_spec


def test_build_combo_sub_spec_leave(tmp_path):
    """批量退频道子任务：使用退频道 tab 全局配置 + 内置排除关键字。"""
    from tg_signer.webui.auto_leave import UNSELECT_KEYWORDS, save_config

    save_config(tmp_path, {"days": 14, "include_groups": True, "skip_pinned": False})
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
        build_combo_sub_spec("bob", {"type": "未知类型", "task": "x"}, tmp_path)
        is None
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
