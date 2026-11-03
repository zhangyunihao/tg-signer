"""组合任务失败项重试的状态机与结果回写测试。"""

import pytest

from tg_signer.webui import combo

SUBS = [
    {"type": "签到", "task": "A"},
    {"type": "自动化", "task": "B"},
]
LABEL_A = "签到 · A"
LABEL_B = "自动化 · B"


@pytest.fixture(autouse=True)
def _clean_retry_jobs():
    with combo._retry_lock:
        combo._retry_jobs.clear()
    yield
    with combo._retry_lock:
        combo._retry_jobs.clear()


def _detail(label, account, status, note=""):
    return {"chat": label, "account": account, "status": status, "note": note}


def test_failed_units_only_returns_failed_and_dedupes():
    details = [
        _detail(LABEL_A, "alice", "失败"),
        _detail(LABEL_A, "alice", "失败"),
        _detail(LABEL_A, "bob", "成功"),
        _detail(LABEL_B, "alice", "失败"),
        _detail("已删除的子任务 · C", "alice", "失败"),
        _detail(LABEL_A, "", "失败"),
    ]
    assert combo.combo_failed_units(details, SUBS) == [
        (LABEL_A, "alice"),
        (LABEL_B, "alice"),
    ]


def test_apply_retry_results_keeps_success_rows_untouched():
    details = [
        _detail(LABEL_A, "alice", "失败", "超时"),
        _detail(LABEL_A, "bob", "成功", "已完成"),
        _detail(LABEL_B, "alice", "成功", "已完成"),
    ]
    updates = {
        (LABEL_A, "alice"): ("成功", "重试成功"),
        (LABEL_A, "bob"): ("失败", "不应覆盖成功行"),
        (LABEL_B, "alice"): ("失败", "不应覆盖成功行"),
    }
    changed = combo.combo_apply_retry_results(details, updates)
    assert changed == 1
    assert details[0]["status"] == "成功"
    assert details[0]["note"] == "重试成功"
    assert details[1]["status"] == "成功"
    assert details[2]["status"] == "成功"


def test_record_status_flips_only_when_no_failure_remains():
    record = {
        "status": "失败",
        "result": "2 个失败",
        "details": [
            _detail(LABEL_A, "alice", "失败"),
            _detail(LABEL_B, "alice", "成功"),
        ],
    }
    combo.combo_refresh_record_status(record)
    assert record["status"] == "失败"

    record["details"][0]["status"] = "成功"
    combo.combo_refresh_record_status(record)
    assert record["status"] == "已完成"
    assert "失败项重试后全部成功" in record["result"]

    # 已经是已完成/已停止的记录不会被改动
    stopped = {"status": "已停止", "details": [_detail(LABEL_A, "alice", "成功")]}
    combo.combo_refresh_record_status(stopped)
    assert stopped["status"] == "已停止"


def test_failure_reason_prefers_error_line():
    lines = [
        "正在执行...",
        "[错误] TimeoutError: 连接超时",
        "统计：0 个",
    ]
    assert "TimeoutError" in combo.combo_failure_reason(lines)
    assert combo.combo_failure_reason(["最后一行"]) == "最后一行"
    assert combo.combo_failure_reason([]) == ""


def test_retry_state_registry_reports_queue_and_running():
    rid = "rid-1"
    with combo._retry_lock:
        combo._retry_jobs[rid] = {
            "units": {
                (LABEL_A, "alice"): {
                    "state": combo.COMBO_RETRY_QUEUED,
                    "account": "alice",
                },
                (LABEL_B, "alice"): {
                    "state": combo.COMBO_RETRY_RUNNING,
                    "account": "alice",
                },
            },
            "pending": [],
            "spawned": {},
            "thread": None,
            "client": None,
        }
    assert combo.combo_retry_unit_state(rid, LABEL_A, "alice") == "排队中"
    assert combo.combo_retry_unit_state(rid, LABEL_B, "alice") == "重试中…"
    assert combo.combo_retry_unit_state(rid, LABEL_A, "bob") == ""
    assert combo.combo_retry_active_count(rid) == 2
    assert combo.combo_retry_signature(rid) == (
        (LABEL_A, "alice", "排队中"),
        (LABEL_B, "alice", "重试中…"),
    )
    with combo._retry_lock:
        combo._retry_jobs[rid]["units"][(LABEL_A, "alice")]["state"] = "成功"
    assert combo.combo_retry_unit_state(rid, LABEL_A, "alice") == ""
    assert combo.combo_retry_active_count(rid) == 1
