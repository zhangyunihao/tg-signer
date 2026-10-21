"""GiveawayState 并发落盘合并（防覆盖）测试。"""

from tg_signer.giveaway_state import GiveawayState


def test_save_merges_records_written_by_other_instance(tmp_path):
    """并发场景：b 构造早于 a 落盘，b.save 不应覆盖掉 a 新写的记录。"""
    a = GiveawayState(tmp_path)
    b = GiveawayState(tmp_path)  # 与 a 同一初始快照（磁盘上还没有记录）
    a.add("acct", "bot1", "p1")
    a.save()
    b.add("acct", "bot2", "p2")  # b 本地没有 a 刚写的 bot1 记录
    b.save()
    final = GiveawayState(tmp_path)
    assert final.has("acct", "bot1", "p1")
    assert final.has("acct", "bot2", "p2")


def test_save_keeps_newer_record_on_same_key(tmp_path):
    """同键冲突：磁盘上的记录 ts 更新时，旧快照的覆盖应被保留值顶回。"""
    a = GiveawayState(tmp_path)
    a.add("acct", "bot1", "p1", note="ended")
    a.save()
    b = GiveawayState(tmp_path)
    assert b.is_ended("acct", "bot1", "p1")
    # 模拟 b 持有的旧快照（ts 更早的 joined 记录）重新落盘
    b._data["acct"]["bot1|p1"] = {
        "ts": "2000-01-01T00:00:00",
        "bot": "bot1",
        "param": "p1",
        "channel": "",
        "note": "joined",
    }
    b._dirty = True
    b.save()
    final = GiveawayState(tmp_path)
    assert final.is_ended("acct", "bot1", "p1")


def test_clear_still_empties_records(tmp_path):
    """合并逻辑不应让「清空记录」失效（clear 先删文件再落盘）。"""
    a = GiveawayState(tmp_path)
    a.add("acct", "bot1", "p1")
    a.save()
    assert a.clear() == 1
    final = GiveawayState(tmp_path)
    assert final.count() == 0
