"""GiveawayState 并发写入（SQLite，防覆盖）测试。"""

import time

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


def test_account_limits_visible_across_instances(tmp_path):
    """限流状态落盘后，其它实例（WebUI 进程）可读取到剩余时长。"""
    a = GiveawayState(tmp_path)
    a.set_account_limit("acct", "flood_wait", time.time() + 120, "@bot")
    a.set_account_limit("acct", "peer_flood", time.time() + 600, "强制限流")
    limits = GiveawayState(tmp_path).active_account_limits()
    kinds = {item["kind"] for item in limits["acct"]}
    assert kinds == {"flood_wait", "peer_flood"}
    flood = next(i for i in limits["acct"] if i["kind"] == "flood_wait")
    assert flood["note"] == "@bot"
    assert 100 < flood["until"] - time.time() <= 120


def test_account_limit_only_extends(tmp_path):
    """同类限流重复登记只延长不缩短，避免短等待覆盖长等待。"""
    a = GiveawayState(tmp_path)
    long_until = time.time() + 600
    a.set_account_limit("acct", "flood_wait", long_until)
    a.set_account_limit("acct", "flood_wait", time.time() + 10)
    until = a.active_account_limits()["acct"][0]["until"]
    assert until >= long_until - 1


def test_expired_account_limits_dropped(tmp_path):
    """到期记录不再返回，并顺带从数据库清理。"""
    a = GiveawayState(tmp_path)
    a.set_account_limit("acct", "flood_wait", time.time() - 1)
    assert a.active_account_limits() == {}
