"""消息中心分区展示：未读全量、已读 5 条步进、未读上限保护。"""

from tg_signer.webui.messages import (
    READ_PAGE_SIZE,
    UNREAD_RENDER_LIMIT,
    partition_messages,
    visible_sections,
)


def _messages(unread: int, read: int) -> tuple[list[dict], set[str]]:
    """构造先未读、后已读的列表，返回 (消息, 已读键集合)。"""
    messages = [{"key": f"u{i}", "tag": "中奖"} for i in range(unread)]
    read_keys = set()
    for i in range(read):
        messages.append({"key": f"r{i}", "tag": "中奖"})
        read_keys.add(f"r{i}")
    return messages, read_keys


def test_unread_all_read_five_and_three_expands():
    messages, read_keys = _messages(60, 300)

    unread, unread_total, read, read_total = visible_sections(
        messages, read_keys, READ_PAGE_SIZE
    )
    assert unread_total == 60
    assert len(unread) == 60
    assert read_total == 300
    assert len(read) == 5

    shown = READ_PAGE_SIZE
    for _ in range(3):
        shown += READ_PAGE_SIZE
        _, _, read, _ = visible_sections(messages, read_keys, shown)
    assert len(read) == 20


def test_unread_over_limit_is_capped_but_counted():
    messages, read_keys = _messages(200, 0)

    unread, unread_total, read, read_total = visible_sections(messages, read_keys, 5)
    assert unread_total == 200
    assert len(unread) == UNREAD_RENDER_LIMIT == 100
    assert unread_total - len(unread) == 100
    assert read == [] and read_total == 0


def test_partition_keeps_existing_order():
    messages = [
        {"key": "a", "tag": "中奖"},
        {"key": "b", "tag": "中奖"},
        {"key": "c", "tag": "中奖"},
        {"key": "d", "tag": "中奖"},
    ]
    unread, read = partition_messages(messages, {"b", "d"})

    assert [m["key"] for m in unread] == ["a", "c"]
    assert [m["key"] for m in read] == ["b", "d"]
