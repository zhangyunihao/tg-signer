import json
import logging
import threading
import time

import pytest

from tg_signer.locks import (
    InstanceLock,
    acquire_task_locks,
    lock_path_for,
    release_task_locks,
    task_lock_path,
)
from tg_signer.utils import atomic_write_text


def test_instance_lock_is_exclusive_and_reusable(tmp_path):
    path = tmp_path / "locks" / "a.lock"
    first = InstanceLock(path)
    assert first.acquire() is True

    second = InstanceLock(path)
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_same_account_and_task_is_single_instance(tmp_path):
    locks = acquire_task_locks(tmp_path, ["acct1"], "sign", "task-a")
    assert locks is not None

    # 同一账号 + 同一任务再次启动应被拒绝
    assert acquire_task_locks(tmp_path, ["acct1"], "sign", "task-a") is None
    # 不同任务 / 不同类型 / 不同账号互不影响
    assert acquire_task_locks(tmp_path, ["acct1"], "sign", "task-b") is not None
    assert acquire_task_locks(tmp_path, ["acct1"], "giveaway", "task-a") is not None
    assert acquire_task_locks(tmp_path, ["acct2"], "sign", "task-a") is not None

    release_task_locks(locks)
    assert acquire_task_locks(tmp_path, ["acct1"], "sign", "task-a") is not None


def test_partial_acquisition_rolls_back(tmp_path):
    held = acquire_task_locks(tmp_path, ["acct1"], "sign", "task-a")
    assert held is not None

    # acct1/task-a 被占用，acct2/task-a 应随失败一起回滚
    assert acquire_task_locks(tmp_path, ["acct1", "acct2"], "sign", "task-a") is None
    assert acquire_task_locks(tmp_path, ["acct2"], "sign", "task-a") is not None

    release_task_locks(held)


def test_task_lock_path_is_filesystem_safe(tmp_path):
    path = task_lock_path(tmp_path, "账号/1", "giveaway", "任务 A: 1")
    assert path.parent == tmp_path / "locks"
    assert "/" not in path.name and ":" not in path.name
    assert path.suffix == ".lock"


def test_file_lock_serializes_concurrent_read_modify_write(tmp_path):
    from tg_signer.locks import file_lock

    target = tmp_path / "state.json"
    target.write_text(json.dumps({"count": 0}), encoding="utf-8")
    barrier = threading.Barrier(8)

    def bump():
        barrier.wait()
        for _ in range(20):
            with file_lock(target):
                data = json.loads(target.read_text(encoding="utf-8"))
                time.sleep(0.0005)
                data["count"] += 1
                target.write_text(json.dumps(data), encoding="utf-8")

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert json.loads(target.read_text(encoding="utf-8"))["count"] == 160


def test_file_lock_times_out_when_held(tmp_path, monkeypatch):
    from tg_signer.locks import file_lock

    target = tmp_path / "state.json"
    held_ready = threading.Event()
    release_now = threading.Event()

    def holder():
        with file_lock(target):
            held_ready.set()
            release_now.wait(5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held_ready.wait(2)

    try:
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.2):
                pass
    finally:
        release_now.set()
        thread.join(5)

    # 释放后可以正常获取
    with file_lock(target, timeout=1):
        pass


def test_lock_path_for_sits_beside_target(tmp_path):
    assert lock_path_for(tmp_path / "a.json") == tmp_path / "a.json.lock"


def test_task_runner_refuses_duplicate_start(tmp_path, monkeypatch):
    from tg_signer import logger as logger_module
    from tg_signer import task_runner

    monkeypatch.setattr(
        logger_module, "configure_logger", lambda **_: logging.getLogger("test")
    )

    held = acquire_task_locks(tmp_path, ["acct"], "sign", "task-a")
    assert held is not None
    try:
        code = task_runner.main(
            ["-w", str(tmp_path), "-a", "acct", "run-once", "task-a"]
        )
    finally:
        release_task_locks(held)

    assert code == 3


def test_account_state_fields_survive_concurrent_saves(tmp_path):
    from tg_signer.webui.data import (
        load_disabled_accounts,
        load_two_way_accounts,
        set_account_enabled,
        set_account_two_way,
    )

    barrier = threading.Barrier(2)

    def disable():
        barrier.wait()
        for _ in range(20):
            set_account_enabled("acct-disabled", False, workdir=tmp_path)

    def two_way():
        barrier.wait()
        for _ in range(20):
            set_account_two_way("acct-two-way", True, workdir=tmp_path)

    threads = [threading.Thread(target=disable), threading.Thread(target=two_way)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 两个字段写同一个文件，必须都保留下来
    assert load_disabled_accounts(tmp_path) == {"acct-disabled"}
    assert load_two_way_accounts(tmp_path) == {"acct-two-way"}


def test_atomic_write_text_replaces_content(tmp_path):
    path = tmp_path / "sub" / "secret.txt"
    atomic_write_text(path, "first")
    atomic_write_text(path, "second")

    assert path.read_text(encoding="utf-8") == "second"
    # 临时文件不残留
    assert list(path.parent.glob("*.tmp")) == []
