"""组合任务（combo）相关单元测试。"""

import json

import pytest

from tg_signer import combo_runner


def test_combo_runner_missing_args_exits_2(capsys):
    combo_runner.main.__globals__["sys"].argv = ["combo_runner"]
    with pytest.raises(SystemExit) as exc:
        combo_runner.main()
    assert exc.value.code == 2


def test_combo_runner_runs_subs_sequentially(monkeypatch):
    """子任务按顺序执行，整体退出码取所有子任务的成败。"""
    calls = []

    def fake_call(cmd):
        calls.append(cmd[-1])  # 最后一个参数代表子任务标识
        return 0

    monkeypatch.setattr(combo_runner.subprocess, "call", fake_call)
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
    monkeypatch.setattr(combo_runner.subprocess, "call", lambda cmd: 1)
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
