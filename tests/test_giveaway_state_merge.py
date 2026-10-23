"""GiveawayState 并发写入（SQLite，防覆盖）测试。"""

from tg_signer.giveaway_state import GiveawayState


def test_concurrent_instances_do_not_overwrite_each_other(tmp_path):
    """并发场景：b 构造早于 a 落盘，b 的写入不应覆盖掉 a 新写的记录。"""
    a = GiveawayState(tmp_path)
    b = GiveawayState(tmp_path)  # 与 a 同一初始快照（磁盘上还没有记录）
    a.add("acct", "bot1", "p1")
    a.save()
    b.add("acct", "bot2", "p2")  # b 本地没有 a 刚写的 bot1 记录
    b.save()
    final = GiveawayState(tmp_path)
    assert final.has("acct", "bot1", "p1")
    assert final.has("acct", "bot2", "p2")


def test_same_key_last_write_wins(tmp_path):
    """同键重复写入以最后一次为准（数据库 upsert），其他实例立即可见。"""
    a = GiveawayState(tmp_path)
    a.add("acct", "bot1", "p1", note="joined")
    a.save()
    b = GiveawayState(tmp_path)
    assert b.has("acct", "bot1", "p1")
    b.add("acct", "bot1", "p1", note="ended")
    b.save()
    final = GiveawayState(tmp_path)
    assert final.is_ended("acct", "bot1", "p1")


def test_clear_still_empties_records(tmp_path):
    """clear 直接删除参与记录并即时提交，不受并发实例影响。"""
    a = GiveawayState(tmp_path)
    a.add("acct", "bot1", "p1")
    a.save()
    assert a.clear() == 1
    final = GiveawayState(tmp_path)
    assert final.count() == 0
