"""消息中心：同账号 + 同频道的「发言数未达标」提醒合并后一次点击即可全部已读。"""

from tg_signer.webui import giveaway
from tg_signer.webui.messages import (
    collect_messages,
    is_message_unread,
    message_keys,
)


def _speech_alert(title, required, time_str):
    return {
        "account": "alice",
        "bot": "giveaway_bot",
        "group": "上海艺术展览会交流群",
        "title": title,
        "required": required,
        "time": time_str,
    }


def _speech_messages(tmp_path):
    return [m for m in collect_messages(tmp_path) if m["tag"] == "发言数未达标"]


def test_speech_alerts_merge_and_read_at_once(tmp_path):
    giveaway.save_speech_alerts(
        tmp_path,
        [
            _speech_alert("抽奖A", 3, "11-02 03:03:28"),
            _speech_alert("抽奖B", 5, "11-02 02:03:28"),
        ],
    )

    msgs = _speech_messages(tmp_path)
    assert len(msgs) == 1
    merged = msgs[0]
    assert len(message_keys(merged)) == 2
    assert "抽奖B" in merged["text"]

    read_keys = set()
    assert is_message_unread(merged, read_keys)
    # 模拟点击合并消息：一次把合并里的全部原始提醒标为已读
    read_keys.update(message_keys(merged))
    assert not is_message_unread(merged, read_keys)
    # 全部已读后仍保持合并为一条，不炸开成多条已读消息
    assert len(_speech_messages(tmp_path)) == 1


def test_speech_alerts_in_different_groups_stay_separate(tmp_path):
    giveaway.save_speech_alerts(
        tmp_path,
        [
            _speech_alert("抽奖A", 3, "11-02 03:03:28"),
            {
                **_speech_alert("抽奖B", 5, "11-02 02:03:28"),
                "group": "另一个群",
            },
        ],
    )

    msgs = _speech_messages(tmp_path)
    assert len(msgs) == 2
    assert all(len(message_keys(m)) == 1 for m in msgs)
