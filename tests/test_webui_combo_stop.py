"""组合任务停止：停止在线程里执行，界面落地必须回到事件循环。"""

import asyncio

from tg_signer.webui import combo, task_panel


def test_stop_run_in_thread_does_not_touch_ui(monkeypatch):
    """_stop_run 由 asyncio.to_thread 调用时不得刷新界面。

    后台线程没有 NiceGUI 的 slot 上下文，调用 ui.notify 会抛 RuntimeError 并
    吞掉停止结果（组合任务停止后界面报「页面回调异常」、快照未收尾）。
    """
    monkeypatch.setattr(
        task_panel,
        "stop_run_core",
        lambda record_id: ("stopped", {"task_name": "demo"}),
    )

    def boom(*args, **kwargs):
        raise AssertionError("后台线程不应刷新界面")

    monkeypatch.setattr(task_panel.TaskRunnerBlock, "_apply_stop_result", boom)

    result = asyncio.run(
        asyncio.to_thread(task_panel.TaskRunnerBlock._stop_run, None, "rid")
    )
    assert result == ("stopped", {"task_name": "demo"})


def test_combo_stop_applies_ui_result_on_event_loop(tmp_path, monkeypatch):
    """组合任务停止后：线程内只停止，界面落地回到事件循环执行一次。"""

    class FakeRunner:
        runs: dict = {}

        def _stop_run(self, run_id):
            return "stopped", {"task_name": "demo"}

        def _apply_stop_result(self, run_id, outcome, record):
            calls["ui"] = (run_id, outcome, record)
            return outcome, record

    calls: dict = {}
    view = combo.ComboTaskBlock.headless(tmp_path)
    view.runner = FakeRunner()
    monkeypatch.setattr(
        view, "_finalize_snapshot", lambda rid: calls.setdefault("finalized", rid)
    )

    outcome, record = asyncio.run(view._stop_shared("rid"))
    assert outcome == "stopped"
    assert record == {"task_name": "demo"}
    assert calls["ui"] == ("rid", "stopped", {"task_name": "demo"})
    assert calls["finalized"] == "rid"


def test_combo_stop_without_ui_hook_still_works(tmp_path):
    """独立进度页使用 _SharedRunner（无界面落地钩子）时停止流程不受影响。"""

    class PlainRunner:
        runs: dict = {}

        @staticmethod
        def _stop_run(run_id):
            return "missing", None

    view = combo.ComboTaskBlock.headless(tmp_path)
    view.runner = PlainRunner()

    outcome, record = asyncio.run(view._stop_shared("rid"))
    assert outcome == "missing"
    assert record is None
