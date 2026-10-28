"""组合任务（combo）相关单元测试。"""

import json
from datetime import datetime

import pytest

from tg_signer import combo_runner


def test_combo_runner_missing_args_exits_2(monkeypatch, capsys):
    # 用 monkeypatch 注入并自动还原 argv，避免污染同一进程内后续测试
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner"])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 2


def test_combo_runner_runs_subs_sequentially(monkeypatch):
    """子任务按顺序执行，整体退出码取所有子任务的成败。"""
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd[-1])  # 最后一个参数代表子任务标识
        return 0

    monkeypatch.setattr(combo_runner, "_run_with_timeout", fake_run)
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        [
            "combo_runner",
            json.dumps(
                {
                    "subs": [
                        {"label": "签到 · A", "args": ["-w", ".", "run-once", "A"]},
                        {
                            "label": "自动化 · B",
                            "args": ["-w", ".", "automation", "run", "B"],
                        },
                    ]
                }
            ),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert calls == ["A", "B"]


def test_combo_runner_failed_sub_makes_exit_code_1(monkeypatch):
    monkeypatch.setattr(combo_runner, "_run_with_timeout", lambda cmd, timeout=None: 1)
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        ["combo_runner", json.dumps({"subs": [{"label": "x", "args": ["a"]}]})],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 1


def test_combo_runner_bad_json_exits_2(monkeypatch):
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", "{bad json"])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 2


def test_parse_start_at_accepts_datetime_formats():
    assert combo_runner._parse_start_at("2026-10-27 09:00:00") == datetime(
        2026, 10, 27, 9, 0
    )
    assert combo_runner._parse_start_at("2026-10-27 09:00") == datetime(
        2026, 10, 27, 9, 0
    )
    assert combo_runner._parse_start_at("") is None
    assert combo_runner._parse_start_at("09:00") is None


def test_combo_runner_start_at_waits_then_checks_network(monkeypatch):
    """有开始时间时先等待到点、再检查网络，通过后按顺序执行子任务。"""
    calls = []
    monkeypatch.setattr(
        combo_runner, "_wait_until_start", lambda start_at: calls.append("wait")
    )
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: calls.append("network") or True,
    )
    monkeypatch.setattr(combo_runner, "_run_with_timeout", lambda cmd, timeout=None: 0)
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        [
            "combo_runner",
            json.dumps(
                {
                    "start_at": "2026-10-27 09:00:00",
                    "subs": [{"label": "x", "args": ["a"]}],
                },
                ensure_ascii=False,
            ),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert calls == ["wait", "network"]


def test_combo_runner_network_unavailable_skips_subs(monkeypatch):
    """网络连续不通时整体失败退出（退出码 3），不执行任何子任务。"""
    runs = []
    monkeypatch.setattr(combo_runner, "_wait_until_start", lambda start_at: None)
    monkeypatch.setattr(
        combo_runner, "_wait_for_network", lambda interval, retries: False
    )
    monkeypatch.setattr(
        combo_runner, "_run_with_timeout", lambda cmd, timeout=None: runs.append(cmd)
    )
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        [
            "combo_runner",
            json.dumps(
                {
                    "start_at": "2026-10-27 09:00:00",
                    "subs": [{"label": "x", "args": ["a"]}],
                },
                ensure_ascii=False,
            ),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 3
    assert runs == []


def test_combo_runner_invalid_start_at_exits_2(monkeypatch):
    monkeypatch.setattr(combo_runner, "_run_with_timeout", lambda cmd, timeout=None: 0)
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        [
            "combo_runner",
            json.dumps({"start_at": "09:00", "subs": [{"label": "x", "args": ["a"]}]}),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 2


def test_parse_retry_settings_defaults_and_overrides():
    assert combo_runner._parse_retry_settings({}) == (
        combo_runner.NETWORK_RETRY_INTERVAL,
        combo_runner.NETWORK_MAX_RETRIES,
    )
    assert combo_runner._parse_retry_settings(
        {"network_retry_seconds": 600, "network_max_retries": 5}
    ) == (600, 5)
    # 非法/缺省值回落默认，间隔至少 1 秒、次数不为负
    assert combo_runner._parse_retry_settings(
        {"network_retry_seconds": "bad", "network_max_retries": -2}
    ) == (combo_runner.NETWORK_RETRY_INTERVAL, 0)
    assert combo_runner._parse_retry_settings(
        {"network_retry_seconds": 0, "network_max_retries": None}
    ) == (
        1,
        combo_runner.NETWORK_MAX_RETRIES,
    )


def test_fmt_interval():
    assert combo_runner._fmt_interval(1800) == "30 分钟"
    assert combo_runner._fmt_interval(90) == "90 秒"


def test_combo_runner_uses_spec_retry_settings(monkeypatch):
    """spec 里的重试间隔/次数会传给网络等待逻辑。"""
    seen = []
    monkeypatch.setattr(combo_runner, "_wait_until_start", lambda start_at: None)
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: seen.append((interval, retries)) or True,
    )
    monkeypatch.setattr(combo_runner, "_run_with_timeout", lambda cmd, timeout=None: 0)
    monkeypatch.setattr(
        combo_runner.sys,
        "argv",
        [
            "combo_runner",
            json.dumps(
                {
                    "start_at": "2026-10-27 09:00:00",
                    "network_retry_seconds": 600,
                    "network_max_retries": 5,
                    "subs": [{"label": "x", "args": ["a"]}],
                },
                ensure_ascii=False,
            ),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert seen == [(600, 5)]


def test_wait_for_network_retries_every_30_minutes(monkeypatch):
    probes = iter([(False, "直连连接失败"), (False, "直连连接失败"), (True, "直连")])
    monkeypatch.setattr(combo_runner, "_check_telegram_network", lambda: next(probes))
    sleeps = []
    monkeypatch.setattr(combo_runner.time, "sleep", sleeps.append)
    assert (
        combo_runner._wait_for_network(
            combo_runner.NETWORK_RETRY_INTERVAL, combo_runner.NETWORK_MAX_RETRIES
        )
        is True
    )
    assert sleeps == [combo_runner.NETWORK_RETRY_INTERVAL] * 2


def test_wait_for_network_uses_custom_interval_and_retries(monkeypatch):
    probes = iter([(False, "直连连接失败"), (True, "直连")])
    monkeypatch.setattr(combo_runner, "_check_telegram_network", lambda: next(probes))
    sleeps = []
    monkeypatch.setattr(combo_runner.time, "sleep", sleeps.append)
    assert combo_runner._wait_for_network(600, 2) is True
    assert sleeps == [600]


def test_wait_for_network_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(
        combo_runner, "_check_telegram_network", lambda: (False, "直连连接失败")
    )
    sleeps = []
    monkeypatch.setattr(combo_runner.time, "sleep", sleeps.append)
    assert (
        combo_runner._wait_for_network(
            combo_runner.NETWORK_RETRY_INTERVAL, combo_runner.NETWORK_MAX_RETRIES
        )
        is False
    )
    assert sleeps == [combo_runner.NETWORK_RETRY_INTERVAL] * (
        combo_runner.NETWORK_MAX_RETRIES
    )


def test_combo_runner_module_sub(monkeypatch):
    """module 型子任务（自动抽奖/批量退频道）以 python -m <module> <spec> 执行。"""
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(combo_runner, "_run_with_timeout", fake_run)
    spec = {
        "subs": [
            {
                "label": "抽奖 · 1",
                "module": "tg_signer.giveaway_runner",
                "spec": {"account": "a", "workdir": "."},
            }
        ]
    }
    monkeypatch.setattr(
        combo_runner.sys, "argv", ["combo_runner", json.dumps(spec, ensure_ascii=False)]
    )
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1:3] == ["-m", "tg_signer.giveaway_runner"]
    assert json.loads(cmd[3])["account"] == "a"
