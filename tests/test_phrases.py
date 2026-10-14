"""任务级自定义语库的解析/展示，以及 RandomChatEngine 语库选择逻辑。"""

import random
from pathlib import Path

import pytest

from tg_signer.phrases import PHRASES, parse_custom_phrases, phrase_pool_label


class TestParseCustomPhrases:
    def test_none_and_empty(self):
        assert parse_custom_phrases(None) == []
        assert parse_custom_phrases("") == []
        # 纯空白行同样视为未配置
        assert parse_custom_phrases("  \n\t\n  \n") == []

    def test_split_lines_and_strip(self):
        assert parse_custom_phrases("今天天气不错\n群里挺热闹啊\n学到了") == [
            "今天天气不错",
            "群里挺热闹啊",
            "学到了",
        ]

    def test_drops_blank_and_trims_surrounding_space(self):
        raw = "  第一句  \n\n   \n第二句\n"
        assert parse_custom_phrases(raw) == ["第一句", "第二句"]

    def test_keeps_duplicate_lines(self):
        # 每行一条，重复内容不做去重（用户可能有意重复以提高抽中概率）
        assert parse_custom_phrases("打卡\n打卡\n签到") == ["打卡", "打卡", "签到"]


class TestPhrasePoolLabel:
    def test_default_builtin_for_old_config(self):
        # 老任务配置没有 custom_phrases 字段时，显示内置语库
        assert phrase_pool_label({}) == f"内置 {len(PHRASES)} 条"
        assert phrase_pool_label({"custom_phrases": []}) == f"内置 {len(PHRASES)} 条"

    def test_custom_pool(self):
        cfg = {"custom_phrases": ["一句", "二句", "三句"]}
        assert phrase_pool_label(cfg) == "自定义 3 条"


class TestRandomChatEnginePool:
    """nicegui 属于可选 gui 依赖，无该环境时跳过引擎测试。

    传入空账号列表：_loop 不会建立任何 Telegram 连接，只验证 start() 的语库选择。
    """

    @pytest.mark.asyncio
    async def test_engine_picks_custom_pool(self):
        pytest.importorskip("nicegui")
        from tg_signer.webui.random_chat import RandomChatEngine

        custom = ["专属甲", "专属乙"]
        engine = RandomChatEngine()
        assert engine.pool == list(PHRASES)
        ok = await engine.start(
            accounts=[],
            chat_id=-100,
            interval=1,
            session_dir=Path("."),
            log=lambda _msg: None,
            phrases=custom,
        )
        assert ok is True
        assert engine.using_custom_pool is True
        assert engine.pool == custom
        # 大量抽样全部落在自定义语库内，不会抽到内置语库
        random.seed(7)
        picks = {random.choice(engine.pool) for _ in range(200)}
        assert picks <= set(custom)
        await engine._task

    @pytest.mark.asyncio
    async def test_engine_falls_back_to_builtin(self):
        pytest.importorskip("nicegui")
        from tg_signer.webui.random_chat import RandomChatEngine

        engine = RandomChatEngine()
        ok = await engine.start(
            accounts=[],
            chat_id=-100,
            interval=1,
            session_dir=Path("."),
            log=lambda _msg: None,
            phrases=[],
        )
        assert ok is True
        assert engine.using_custom_pool is False
        assert engine.pool == list(PHRASES)
        await engine._task
