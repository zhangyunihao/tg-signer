import asyncio

import pytest

from tg_signer.webui import account_queue as aq


@pytest.fixture(autouse=True)
def _clean_queue():
    aq.reset()
    yield
    aq.reset()


def test_first_task_gets_lease_and_second_waits_in_fifo():
    granted, queued = aq.request("t1", "任务A", "签到", ["acct"])
    assert granted == ["acct"] and queued == []

    granted2, queued2 = aq.request("t2", "任务B", "抽奖", ["acct"])
    assert granted2 == [] and queued2 == ["acct"]
    granted3, queued3 = aq.request("t3", "任务C", "发言", ["acct"])
    assert granted3 == [] and queued3 == ["acct"]

    aq.release("t1", ["acct"])
    assert aq.poll("t3", "acct") is False  # 非队首不能抢先
    assert aq.poll("t2", "acct") is True
    assert aq.blocked_by("acct") == "任务B（抽奖）"

    aq.release("t2", ["acct"])
    assert aq.poll("t3", "acct") is True
    assert aq.blocked_by("acct") == "任务C（发言）"


def test_queue_limit_rejects_without_partial_acquire():
    aq.request("t1", "任务A", "签到", ["a", "b"], max_entries=0)
    with pytest.raises(aq.QueueLimitError):
        aq.request("t2", "任务B", "抽奖", ["a", "b", "c"], max_entries=1)
    # 拒绝后不能留下半授予的租约
    assert aq.blocked_by("c") is None
    assert aq.waiting_count() == 0


def test_zero_limit_means_unlimited():
    aq.request("t1", "任务A", "签到", ["a"], max_entries=0)
    granted, queued = aq.request("t2", "任务B", "抽奖", ["a"], max_entries=0)
    assert granted == [] and queued == ["a"]
    assert aq.waiting_count() == 1


def test_cancel_and_release_clear_all_task_entries():
    aq.request("t1", "任务A", "签到", ["a"])
    aq.request("t2", "任务B", "抽奖", ["a", "b"])
    aq.release_all("t2")
    assert aq.blocked_by("a") == "任务A（签到）"
    assert aq.waiting_count() == 0
    assert aq.blocked_by("b") is None


def _write_settings(tmp_path, **fields) -> None:
    import json

    (tmp_path / "webui_win_settings.json").write_text(
        json.dumps(fields), encoding="utf-8"
    )


def test_ensure_available_ignores_overlap_when_setting_off(tmp_path):
    """默认关闭：账号被占用时保持排队语义，不主动报错。"""
    aq.request("t1", "任务A", "签到", ["a"])
    aq.ensure_available(tmp_path, ["a"])


def test_ensure_available_raises_when_setting_on(tmp_path):
    """开启后：选中账号与运行中任务重叠时直接报错，且不登记排队条目。"""
    _write_settings(tmp_path, reject_account_overlap=True)
    aq.request("t1", "任务A", "签到", ["a"])

    with pytest.raises(aq.AccountBusyError) as exc:
        aq.ensure_available(tmp_path, ["a", "b"])
    message = str(exc.value)
    assert "账号 a" in message and "任务A（签到）" in message
    assert aq.waiting_count() == 0

    # 未重叠的账号照常可用
    aq.ensure_available(tmp_path, ["b"])


def test_giveaway_runner_rejects_overlap_when_setting_on(tmp_path, monkeypatch):
    """抽奖任务开启该设置后，账号重叠直接启动失败而不是排队。"""
    import tg_signer.webui.giveaway_process as gp

    _install_fake_runner_engine(monkeypatch)
    _write_settings(tmp_path, reject_account_overlap=True)
    aq.request("other", "其它任务", "签到", ["a"])

    async def _run():
        runner = gp.GiveawayTaskRunner("抽奖3", tmp_path)
        started = runner.start(
            {"accounts": ["a", "b"]},
            log=lambda _line: None,
            on_event=lambda _ev: None,
            on_finish=lambda: None,
        )
        assert started is False
        assert "账号 a" in runner.last_error
        assert aq.blocked_by("a") == "其它任务（签到）"
        assert aq.task_state(runner.queue_id)["queued"] == []

    asyncio.run(_run())


def test_snapshot_reports_holder_and_position():
    aq.request("t1", "任务A", "签到", ["a"])
    aq.request("t2", "任务B", "抽奖", ["a"])
    aq.request("t3", "任务C", "发言", ["a"])
    snap = aq.snapshot()
    assert snap["running"] == 1
    assert snap["waiting"] == 2
    queued = [e for e in snap["entries"] if e["kind"] == "queued"]
    assert [e["position"] for e in queued] == [1, 2]
    assert all(e["holder"] == "任务A（签到）" for e in queued)

    state = aq.task_state("t3")
    assert state["queued"][0]["position"] == 2


def test_acquire_wait_returns_after_release():
    aq.request("t1", "任务A", "签到", ["a"])

    async def run() -> bool:
        return await aq.acquire_wait(
            "t2",
            "任务B",
            "更新统计",
            "a",
            max_entries=0,
            poll_interval=0.05,
        )

    async def driver() -> bool:
        handle = asyncio.create_task(run())
        await asyncio.sleep(0.1)
        aq.release("t1", ["a"])
        return await asyncio.wait_for(handle, timeout=2)

    assert asyncio.run(driver()) is True
    assert aq.blocked_by("a") == "任务B（更新统计）"


def test_acquire_wait_stops_and_cancels_waiter():
    aq.request("t1", "任务A", "签到", ["a"])
    stopped = {"v": False}

    async def run() -> bool:
        return await aq.acquire_wait(
            "t2",
            "任务B",
            "发言",
            "a",
            max_entries=0,
            should_stop=lambda: stopped["v"],
            poll_interval=0.05,
        )

    async def driver() -> bool:
        handle = asyncio.create_task(run())
        await asyncio.sleep(0.1)
        stopped["v"] = True
        return await asyncio.wait_for(handle, timeout=2)

    assert asyncio.run(driver()) is False
    assert aq.waiting_count() == 0
    assert aq.blocked_by("a") == "任务A（签到）"


def test_request_respects_fifo_when_lease_becomes_free():
    """账号空出来后，后来申请的任务不能插队，仍按等待顺序放行。"""
    aq.request("t1", "任务A", "签到", ["a"])
    aq.request("t2", "任务B", "抽奖", ["a"])
    aq.release("t1", ["a"])

    granted3, queued3 = aq.request("t3", "任务C", "发言", ["a"])
    assert granted3 == [] and queued3 == ["a"]
    assert aq.waiting_count() == 2

    granted2, queued2 = aq.request("t2", "任务B", "抽奖", ["a"])
    assert granted2 == ["a"] and queued2 == []
    # 队首放行后，本任务的等待条目一并清除
    assert aq.waiting_count() == 1
    assert aq.blocked_by("a") == "任务B（抽奖）"

    aq.release("t2", ["a"])
    granted3b, _ = aq.request("t3", "任务C", "发言", ["a"])
    assert granted3b == ["a"]


class _FakeGiveawayProcess:
    """替代真实子进程：只记录启动参数与状态，便于验证 Runner 的调度。"""

    def __init__(self, name, workdir, task_name=None):
        self.name = name
        self.task_name = task_name
        self.workdir = workdir
        self.joined = 0
        self.joined_by_account = {}
        self.joined_by_account_chat = {}
        self.win_sweep_text = ""
        self.running = False
        self.cfg = None
        self.progress_text = lambda: ""
        self.on_finish = None

    def start(self, cfg, *, log, on_event, on_finish, on_stats=None):
        self.cfg = cfg
        self.running = True
        self.on_finish = on_finish
        return True

    def stop(self):
        self.running = False
        if self.on_finish is not None:
            self.on_finish()


def _install_fake_runner_engine(monkeypatch) -> dict:
    """把 Runner 内部的 GiveawayProcess 换成假实现，返回 {name: 实例}。"""
    import tg_signer.webui.giveaway_process as gp

    created: dict = {}

    class _Factory(_FakeGiveawayProcess):
        def __init__(self, name, workdir, task_name=None):
            super().__init__(name, workdir, task_name)
            created[name] = self

    monkeypatch.setattr(gp, "GiveawayProcess", _Factory)
    return created


def test_giveaway_runner_starts_idle_accounts_and_queues_busy_only(
    tmp_path, monkeypatch
):
    """抽奖任务按账号拆分：忙账号排队，空闲账号立即启动。"""
    import tg_signer.webui.giveaway_process as gp

    created = _install_fake_runner_engine(monkeypatch)
    # 账号 a 被其它任务占用，b 空闲
    aq.request("other", "其它任务", "签到", ["a"])

    async def _run():
        runner = gp.GiveawayTaskRunner("抽奖1", tmp_path)
        started = runner.start(
            {"accounts": ["a", "b"]},
            log=lambda _line: None,
            on_event=lambda _ev: None,
            on_finish=lambda: None,
        )
        assert started is True
        for _ in range(5):
            await asyncio.sleep(0)
        # b 立刻拿到租约并启动，a 仍在排队
        assert "抽奖1#b" in created and created["抽奖1#b"].running
        assert created["抽奖1#b"].cfg["accounts"] == ["b"]
        assert "抽奖1#a" not in created
        assert aq.blocked_by("b") == "抽奖1（自动抽奖）"
        assert aq.task_state(runner.queue_id)["queued"]

        # 释放 a 后，最多等一个轮询周期即可启动
        aq.release("other", ["a"])
        for _ in range(30):
            if "抽奖1#a" in created:
                break
            await asyncio.sleep(0.1)
        assert "抽奖1#a" in created and created["抽奖1#a"].running
        assert not aq.task_state(runner.queue_id)["queued"]

        runner.stop()

    asyncio.run(_run())


def test_giveaway_runner_stop_cancels_waiters_and_releases_leases(
    tmp_path, monkeypatch
):
    """停止任务：取消排队账号、释放租约，并只补发一次结束回调。"""
    import tg_signer.webui.giveaway_process as gp

    _install_fake_runner_engine(monkeypatch)
    aq.request("other", "其它任务", "签到", ["a"])
    finished: list = []

    async def _run():
        runner = gp.GiveawayTaskRunner("抽奖2", tmp_path)
        runner.start(
            {"accounts": ["a"]},
            log=lambda _line: None,
            on_event=lambda _ev: None,
            on_finish=lambda: finished.append(True),
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert runner.running is True
        assert "排队中" in runner.progress_text()
        runner.stop()
        for _ in range(5):
            await asyncio.sleep(0)
        assert runner.running is False
        assert runner.progress_text() == ""
        assert aq.task_state(runner.queue_id)["queued"] == []
        assert finished == [True]

    asyncio.run(_run())
    assert aq.blocked_by("a") == "其它任务（签到）"
