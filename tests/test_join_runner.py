import json

from tg_signer.config import SendTextAction, SignChatV3, SignConfigV3
from tg_signer.join_runner import (
    _label_of,
    _username_from_label,
    load_sign_chats,
    resolve_target,
)


def _write_config(workdir, task, chats):
    path = workdir / "signs" / task / "config.json"
    path.parent.mkdir(parents=True)
    cfg = SignConfigV3(chats=chats, sign_at="06:00:00", random_seconds=0, sign_delay=0)
    path.write_text(
        json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )


def _chat(chat_id, name=""):
    return SignChatV3(chat_id=chat_id, name=name, actions=[SendTextAction(text="hi")])


def test_load_sign_chats_reads_config(tmp_path):
    _write_config(tmp_path, "t1", [_chat("@mygroup", "群A")])

    chats = load_sign_chats(tmp_path, "t1")

    assert len(chats) == 1
    assert str(chats[0].chat_id) == "@mygroup"


def test_load_sign_chats_missing_returns_empty(tmp_path):
    assert load_sign_chats(tmp_path, "not-exist") == []


def test_resolve_target_uses_config_chat_id(tmp_path):
    _write_config(tmp_path, "t1", [_chat("@mygroup", "群A (@mygroup)")])
    chats = load_sign_chats(tmp_path, "t1")

    # 明细里记录的是配置中的 chat_id，重跑时应拿回原值
    target, label = resolve_target(
        {"token": "@mygroup", "label": label_of(chats[0])}, chats
    )

    assert target == "@mygroup"
    assert label == label_of(chats[0])


def test_resolve_target_falls_back_to_numeric_id(tmp_path):
    _write_config(tmp_path, "t1", [_chat(-1001234567890, "公开C")])
    chats = load_sign_chats(tmp_path, "t1")

    target, _ = resolve_target(
        {"token": "-1001234567890#7", "label": "公开C(-1001234567890)"}, chats
    )

    assert target == -1001234567890


def test_username_from_label():
    assert _username_from_label("公开C (@pubc2024)") == "pubc2024"
    assert _username_from_label("摘要") == ""
    assert _username_from_label("邮箱 a@b.com") == ""


def label_of(chat):
    return _label_of(chat)
