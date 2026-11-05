"""消息中心：同账号 + 同频道的「发言数未达标」提醒合并后一次点击即可全部已读。"""

from tg_signer.webui import giveaway
from tg_signer.webui.messages import (
    collect_messages,
    is_message_unread,
    message_html,
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


def test_group_reference_bolded_in_message_html(tmp_path):
    """所有消息渲染 HTML 时，群组引用整体加重展示。"""
    giveaway.save_speech_alerts(tmp_path, [_speech_alert("抽奖A", 3, "11-02 03:03:28")])

    (msg,) = _speech_messages(tmp_path)
    html = message_html(msg)
    assert "<b>群组「上海艺术展览会交流群」</b>" in html
    # 通知/推送用的纯文本正文不含任何 HTML 标记
    assert "<b>" not in msg["text"]


def test_win_prize_bolded_in_message_html(tmp_path):
    """中奖消息渲染 HTML 时，奖品行加重展示，其余正文原样保留。"""
    giveaway.save_win_alerts(
        tmp_path,
        [
            {
                "account": "alice",
                "bot": "giveaway_bot",
                "text": "💐 中奖提醒 💐\n恭喜您中奖了！\n中奖奖品：18.8\n防伪编号：H123",
                "time": "11-04 10:00:00",
            }
        ],
    )

    (msg,) = [m for m in collect_messages(tmp_path) if m["tag"] == "中奖"]
    assert "<b>中奖奖品：18.8</b>" in message_html(msg)
    assert "防伪编号：H123" in msg["text"]
    assert "<b>" not in msg["text"]


def test_win_alert_shows_forward_failures(tmp_path):
    """中奖提醒展示转发失败原因；旧提醒（无该字段）展示保持原样。"""
    giveaway.save_win_alerts(
        tmp_path,
        [
            {
                "account": "8",
                "bot": "LotteryBestBot",
                "text": "恭喜您中奖了",
                "time": "11-05 03:47:00",
                "fwd_failures": ["-1004324986564：CHAT_WRITE_FORBIDDEN"],
            },
            {
                "account": "alice",
                "bot": "giveaway_bot",
                "text": "恭喜您中奖了",
                "time": "11-04 10:00:00",
            },
        ],
    )

    msgs = [m for m in collect_messages(tmp_path) if m["tag"] == "中奖"]
    assert len(msgs) == 2
    texts = [m["text"] for m in msgs]
    assert any(
        "转发失败：-1004324986564：CHAT_WRITE_FORBIDDEN" in text for text in texts
    )
    # 无 fwd_failures 字段的旧提醒不出现「转发失败」
    assert any("转发失败" not in text for text in texts)


def test_win_event_records_forward_failures(tmp_path):
    """win 事件里的 fwd_failures 落盘到提醒并出现在格式化文案中。"""
    giveaway._handle_win_event(
        None,
        tmp_path,
        {
            "type": "win",
            "account": "8",
            "bot": "LotteryBestBot",
            "text": "恭喜您中奖了",
            "fwd_failures": ["-1004324986564：CHAT_WRITE_FORBIDDEN"],
        },
    )
    alerts = giveaway.load_win_alerts(tmp_path)
    assert alerts[0]["fwd_failures"] == ["-1004324986564：CHAT_WRITE_FORBIDDEN"]
    assert (
        "转发失败：-1004324986564：CHAT_WRITE_FORBIDDEN"
        in giveaway.format_win_alert(alerts[0])
    )


def test_message_html_escapes_untrusted_text(tmp_path):
    """正文来自 bot 消息/异常信息：HTML 必须先转义，不允许注入标签。"""
    giveaway.save_speech_alerts(
        tmp_path,
        [
            {
                **_speech_alert("抽奖A", 3, "11-02 03:03:28"),
                "group": "<img src=x>",
            }
        ],
    )

    (msg,) = _speech_messages(tmp_path)
    html = message_html(msg)
    assert "<img" not in html
    assert "&lt;img src=x&gt;" in html
    assert "<b>群组「&lt;img src=x&gt;」</b>" in html
