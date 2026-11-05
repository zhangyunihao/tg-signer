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
    """子任务失败但网络探测正常：不退避不补跑，整体按失败退出。"""
    monkeypatch.setattr(combo_runner, "_run_with_timeout", lambda cmd, timeout=None: 1)
    monkeypatch.setattr(combo_runner, "_check_telegram_network", lambda: (True, "直连"))
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


def _net_fail_spec(count: int) -> str:
    subs = [{"label": f"抽奖 · {i}", "args": [f"sub{i}"]} for i in range(1, count + 1)]
    return json.dumps({"subs": subs}, ensure_ascii=False)


def test_combo_runner_failed_sub_healthy_network_no_backoff(monkeypatch):
    """子任务失败但网络探测正常：不算网络故障，不等待不补跑，直接下一条。"""
    codes = iter([4, 0])
    runs, waits = [], []
    monkeypatch.setattr(
        combo_runner,
        "_run_with_timeout",
        lambda cmd, timeout=None: runs.append(cmd[-1]) or next(codes),
    )
    monkeypatch.setattr(combo_runner, "_check_telegram_network", lambda: (True, "直连"))
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: waits.append((interval, retries)) or True,
    )
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", _net_fail_spec(2)])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    # 任一子任务非 0 整体失败，但网络正常时不触发等待与补跑
    assert exc.value.code == 1
    assert runs == ["sub1", "sub2"]
    assert waits == []


def test_combo_runner_net_fail_waits_then_reruns_failed_sub(monkeypatch, capsys):
    """失败后探测到网络不通：退避恢复后补跑同一条子任务，成功后继续剩余。"""
    # sub1 首跑失败、补跑成功；sub2/sub3 正常
    codes = iter([4, 0, 0, 0])
    runs, waits = [], []
    monkeypatch.setattr(
        combo_runner,
        "_run_with_timeout",
        lambda cmd, timeout=None: runs.append(cmd[-1]) or next(codes),
    )
    # 首次失败后的探测结果：网络不通（补跑成功后不再探测）
    probes = iter([(False, "直连连接失败")])
    monkeypatch.setattr(combo_runner, "_check_telegram_network", lambda: next(probes))
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: waits.append((interval, retries)) or True,
    )
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", _net_fail_spec(3)])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert runs == ["sub1", "sub1", "sub2", "sub3"]
    # 运行中退避用更短的默认间隔，而不是开跑前的 30 分钟
    assert waits == [
        (
            combo_runner.NETWORK_BACKOFF_INTERVAL,
            combo_runner.NETWORK_BACKOFF_MAX_RETRIES,
        )
    ]
    out = capsys.readouterr().out
    # 补跑不重复打开始/结束标记行（否则打乱 WebUI 明细与失败项重试的下标映射）
    assert out.count(">>> 子任务开始 [1/3]") == 1
    assert out.count(">>> 子任务结束 [1/3]") == 1
    assert "网络已恢复，补跑该子任务（第 1/2 次）" in out


def test_combo_runner_timeout_124_probes_network_then_reruns(monkeypatch):
    """超时强杀（124）与退出码 4 同样处理：立即探测，网络不通则补跑。"""
    codes = iter([124, 0])
    runs, waits = [], []
    monkeypatch.setattr(
        combo_runner,
        "_run_with_timeout",
        lambda cmd, timeout=None: runs.append(cmd[-1]) or next(codes),
    )
    monkeypatch.setattr(
        combo_runner, "_check_telegram_network", lambda: (False, "直连连接失败")
    )
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: waits.append((interval, retries)) or True,
    )
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", _net_fail_spec(1)])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 0
    assert runs == ["sub1", "sub1"]
    assert len(waits) == 1


def test_combo_runner_rerun_limit_gives_up_then_continues(monkeypatch, capsys):
    """补跑达到上限后放弃该子任务，不再无限重跑，剩余子任务照常执行。"""
    # sub1 首跑 + NETWORK_SUB_MAX_RETRIES 次补跑均失败；sub2 正常
    codes = iter([4, 4, 4, 0])
    runs, waits = [], []
    monkeypatch.setattr(
        combo_runner,
        "_run_with_timeout",
        lambda cmd, timeout=None: runs.append(cmd[-1]) or next(codes),
    )
    monkeypatch.setattr(
        combo_runner, "_check_telegram_network", lambda: (False, "直连连接失败")
    )
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: waits.append((interval, retries)) or True,
    )
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", _net_fail_spec(2)])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 1
    assert runs == ["sub1", "sub1", "sub1", "sub2"]
    assert len(waits) == combo_runner.NETWORK_SUB_MAX_RETRIES
    out = capsys.readouterr().out
    assert "放弃补跑该子任务" in out


def test_combo_runner_network_gives_up_skips_remaining(monkeypatch, capsys):
    """网络持续不通时中止：当前子任务补打结束行、剩余子任务补打开始/结束行。"""
    runs, waits = [], []
    monkeypatch.setattr(
        combo_runner,
        "_run_with_timeout",
        lambda cmd, timeout=None: runs.append(cmd[-1]) or 4,
    )
    monkeypatch.setattr(
        combo_runner, "_check_telegram_network", lambda: (False, "直连连接失败")
    )
    monkeypatch.setattr(
        combo_runner,
        "_wait_for_network",
        lambda interval, retries: waits.append((interval, retries)) or False,
    )
    monkeypatch.setattr(combo_runner.sys, "argv", ["combo_runner", _net_fail_spec(4)])
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 3
    # 第一条子任务失败并探测到网络不通后：退避失败，不再启动任何新进程
    assert runs == ["sub1"]
    assert len(waits) == 1
    out = capsys.readouterr().out
    # 当前子任务补打结束行，未执行的子任务补打标记行，明细不残留「进行中」
    assert ">>> 子任务结束 [1/4] 退出码 4" in out
    for i in (2, 3, 4):
        assert f">>> 子任务开始 [{i}/4]: 抽奖 · {i}" in out
        assert f">>> 子任务结束 [{i}/4] 退出码 4" in out
    assert "网络不可用，跳过该子任务" in out


def test_net_unfinished_exit_code_is_shared():
    """组合/抽奖/退频道子任务共用同一个「网络未完成」退出码定义。"""
    from tg_signer import NET_UNFINISHED_EXIT_CODE as shared
    from tg_signer import leave_runner
    from tg_signer.giveaway_runner import NET_UNFINISHED_EXIT_CODE as gcode

    assert combo_runner.NET_UNFINISHED_EXIT_CODE == shared
    assert leave_runner.NET_UNFINISHED_EXIT_CODE == shared
    assert gcode == shared
