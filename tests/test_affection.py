"""运行：python -m unittest discover -s plugins/affection/tests -v。

仅替换 Host 的 RPC 边界；配置、组件和能力代理使用已安装的 maibot_sdk。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maibot_sdk import ON_BOT_CONFIG_RELOAD, PluginContext
from maibot_sdk.context import PluginPaths

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugin.py"
SPEC = importlib.util.spec_from_file_location("affection_test_plugin", PLUGIN_FILE)
plugin_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin_module
SPEC.loader.exec_module(plugin_module)


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rpc_timeouts: list[tuple[str, int | None]] = []
        self.config = {
            "bot.nickname": "麦麦",
            "bot.alias_names": ["小麦"],
            "bot.qq_account": "12345",
            "personality.personality": "温柔，喜欢用生活里的小事表达感受。",
            "personality.reply_style": "轻松口语，短句。",
        }
        self.reply = "嘿，我们又熟了一点呀。现在是 1 分，我会记住你的好。"
        self.generate_started = asyncio.Event()
        self.generate_gate: asyncio.Event | None = None
        self.cancelled_generations = 0

    async def rpc(self, method, plugin_id, payload, **kwargs):
        assert method == "cap.call"
        capability, args = payload["capability"], payload["args"]
        self.calls.append((capability, args))
        self.rpc_timeouts.append((capability, kwargs.get("timeout_ms")))
        if capability == "config.get":
            return {"success": True, "value": self.config.get(args["key"], args["default"])}
        if capability == "llm.generate":
            self.generate_started.set()
            if self.generate_gate is not None:
                try:
                    await self.generate_gate.wait()
                except asyncio.CancelledError:
                    self.cancelled_generations += 1
                    raise
            return {"success": True, "response": self.reply, "reasoning": "", "model": "test"}
        if capability == "send.hybrid":
            return {"success": True, "sent": True, "message_id": "sent-message"}
        raise AssertionError(f"Unexpected capability: {capability}")

    def count(self, capability: str) -> int:
        return sum(name == capability for name, _ in self.calls)

    def arguments(self, capability: str) -> list[dict]:
        return [args for name, args in self.calls if name == capability]


def message(
    text: str,
    *,
    user: str = "u1",
    event: str = "m1",
    platform: str = "qq",
    group: bool = False,
    at: str | None = None,
    route: dict | None = None,
    segments: list[dict] | None = None,
    **extra,
) -> dict:
    raw = segments if segments is not None else [{"type": "text", "data": text}]
    if at is not None:
        raw = [{"type": "at", "data": {"target_user_id": at}}, *raw]
    return {
        "platform": platform,
        "session_id": "stream-1",
        "message_id": event,
        "raw_message": raw,
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user, "user_nickname": "测试用户"},
            "group_info": {"group_id": "g1"} if group else None,
            "additional_config": route or {},
        },
        **extra,
    }


def quoted(text: str, *, sender: str = "12345", event: str = "quoted-message") -> dict:
    """Host 1.3.3 的独立引用段；正文不属于当前发送者。"""
    return {
        "type": "reply",
        "data": {
            "target_message_id": event,
            "target_message_content": text,
            "target_message_sender_id": sender,
            "target_message_sender_nickname": "麦麦" if sender == "12345" else "群友",
            "target_message_sender_cardname": "",
        },
    }


def forwarded(text: str) -> dict:
    return {
        "type": "forward",
        "data": [
            {
                "user_id": "someone-else",
                "user_nickname": "群友",
                "user_cardname": "",
                "message_id": "forwarded-message",
                "content": [{"type": "text", "data": text}],
            }
        ],
    }


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "affection.sqlite3"
        self.store = plugin_module.AffectionStore(self.path)
        await self.store.open()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def record(
        self, event, delta, *, platform="qq", stream="s1", user="u1", cooldown=0, now=1000, kind="score", reason="感谢"
    ):
        return await self.store.run(
            self.store._record,
            platform,
            user,
            stream,
            event,
            kind,
            delta,
            0,
            cooldown,
            reason,
            now,
        )

    async def test_persistence_and_platform_identity(self) -> None:
        await self.record("one", 3)
        await self.record("one", -2, platform="telegram")
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        await self.store.open()
        self.assertEqual(await self.store.get("qq", "u1", 0), 3)
        self.assertEqual(await self.store.get("telegram", "u1", 0), -2)
        self.assertEqual(await self.store.get("qq", "new-user", 7), 7)

    async def test_duplicate_event_and_stream_scope(self) -> None:
        self.assertTrue((await self.record("one", 3))["accepted"])
        self.assertFalse((await self.record("one", 3))["accepted"])
        self.assertTrue((await self.record("one", 3, stream="s2"))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 6)

    async def test_cooldown_is_directional_and_persistent(self) -> None:
        self.assertTrue((await self.record("plus1", 3, cooldown=300, now=1000))["accepted"])
        self.assertFalse((await self.record("plus2", 3, cooldown=300, now=1100))["accepted"])
        self.assertTrue((await self.record("minus1", -2, cooldown=300, now=1100))["accepted"])
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        await self.store.open()
        self.assertFalse((await self.record("plus3", 3, cooldown=300, now=1200))["accepted"])
        self.assertTrue((await self.record("plus4", 3, cooldown=300, now=1300))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 4)

    async def test_basic_and_emotion_cooldowns_are_independent_and_persistent(self) -> None:
        self.assertTrue((await self.record("basic", 1, reason="基础交流", cooldown=300, now=1000))["accepted"])
        self.assertTrue((await self.record("praise", 2, reason="感谢", cooldown=300, now=1001))["accepted"])
        self.assertTrue((await self.record("negative", -2, reason="讨厌", cooldown=300, now=1002))["accepted"])
        for event, delta, reason in (
            ("basic-again", 1, "基础交流"),
            ("praise-again", 2, "感谢"),
            ("negative-again", -2, "讨厌"),
        ):
            with self.subTest(reason=reason):
                self.assertFalse((await self.record(event, delta, reason=reason, cooldown=300, now=1100))["accepted"])
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        await self.store.open()
        self.assertFalse((await self.record("basic-restart", 1, reason="基础交流", cooldown=300, now=1200))["accepted"])
        self.assertTrue((await self.record("basic-later", 1, reason="基础交流", cooldown=300, now=1300))["accepted"])
        self.assertTrue((await self.record("praise-later", 2, reason="感谢", cooldown=300, now=1301))["accepted"])
        self.assertTrue((await self.record("negative-later", -2, reason="讨厌", cooldown=300, now=1302))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 2)

    async def test_legacy_database_migration_preserves_score_and_cooldowns(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as database:
            database.executescript("""
                CREATE TABLE affection (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    score INTEGER NOT NULL CHECK(score BETWEEN -100 AND 100),
                    last_positive REAL NOT NULL DEFAULT 0,
                    last_negative REAL NOT NULL DEFAULT 0,
                    last_query REAL NOT NULL DEFAULT 0,
                    last_formal_query REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform, user_id)
                ) WITHOUT ROWID;
                CREATE TABLE events (
                    platform TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    delta INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(platform, stream_id, event_id, kind)
                ) WITHOUT ROWID;
                INSERT INTO affection VALUES('qq', 'u1', 42, 1000, 900, 800, 750);
                INSERT INTO events VALUES('qq', 's1', 'old', 'score', 'u1', 1000, 1, 42, '用户原始聊天内容');
            """)
        legacy = plugin_module.AffectionStore(legacy_path)
        with patch.object(plugin_module.time, "time", return_value=1100):
            await legacy.open()
        try:
            self.assertEqual(await legacy.get("qq", "u1", 0), 42)
            for event, delta, kind, cooldown, reason, now in (
                ("positive-too-early", 1, "score", 300, "感谢", 1100),
                ("negative-too-early", -1, "score", 300, "讨厌", 1100),
                ("query-too-early", 0, "query", 10, "", 805),
                ("formal-too-early", 0, "formal_query", 10, "", 755),
            ):
                result = await legacy.run(
                    legacy._record, "qq", "u1", "s1", event, kind, delta, 0, cooldown, reason, now
                )
                with self.subTest(event=event):
                    self.assertEqual(result, {"accepted": False, "score": 42})
            basic = await legacy.run(
                legacy._record, "qq", "u1", "s1", "first-basic", "score", 1, 0, 300, "基础交流", 1100
            )
            self.assertEqual(basic, {"accepted": True, "score": 43})
            reasons = await legacy.run(lambda: dict(legacy.db.execute("SELECT event_id,reason FROM events")))
            self.assertEqual(reasons["old"], "历史事件")
            self.assertEqual(reasons["first-basic"], "基础交流")
        finally:
            await legacy.close()
        legacy = plugin_module.AffectionStore(legacy_path)
        with patch.object(plugin_module.time, "time", return_value=1200):
            await legacy.open()
        try:
            self.assertEqual(await legacy.get("qq", "u1", 0), 43)
            basic = await legacy.run(
                legacy._record, "qq", "u1", "s1", "restart-basic", "score", 1, 0, 300, "基础交流", 1200
            )
            self.assertEqual(basic, {"accepted": False, "score": 43})
            reasons = await legacy.run(lambda: dict(legacy.db.execute("SELECT event_id,reason FROM events")))
            self.assertEqual(reasons["first-basic"], "基础交流")
        finally:
            await legacy.close()

    async def test_event_ttl_cleans_history_without_resetting_scores(self) -> None:
        ttl = 30 * 24 * 60 * 60
        start = 10_000_000
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        with patch.object(plugin_module.time, "time", return_value=start):
            await self.store.open()
        await self.record("old", 2, now=start)
        self.assertFalse((await self.record("old", 2, now=start + ttl - 1))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 2)
        await self.record("fresh", 1, now=start + ttl + 3600)
        events = await self.store.run(lambda: [row[0] for row in self.store.db.execute("SELECT event_id FROM events")])
        self.assertEqual(events, ["fresh"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 3)

    async def test_event_cleanup_runs_on_startup_and_then_hourly_writes(self) -> None:
        ttl = 30 * 24 * 60 * 60
        start = 10_000_000
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        with patch.object(plugin_module.time, "time", return_value=start - ttl - 1):
            await self.store.open()
        await self.record("expired-startup", 2, now=start - ttl - 1)
        await self.record("expires-later", 1, now=start - ttl + 100)
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        with patch.object(plugin_module.time, "time", return_value=start):
            await self.store.open()
        await self.record("before-hour", 1, now=start + 3599)
        events = await self.store.run(lambda: {row[0] for row in self.store.db.execute("SELECT event_id FROM events")})
        self.assertNotIn("expired-startup", events)
        self.assertIn("expires-later", events)
        await self.record("after-hour", 1, now=start + 3600)
        events = await self.store.run(lambda: {row[0] for row in self.store.db.execute("SELECT event_id FROM events")})
        self.assertEqual(events, {"before-hour", "after-hour"})
        self.assertEqual(await self.store.get("qq", "u1", 0), 5)

    async def test_rejected_event_cannot_be_replayed_after_cooldown(self) -> None:
        await self.record("one", 1, cooldown=300, now=1000)
        self.assertFalse((await self.record("two", 1, cooldown=300, now=1001))["accepted"])
        self.assertFalse((await self.record("two", 1, cooldown=300, now=1400))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 1)

    async def test_query_modes_have_independent_persistent_cooldowns_and_shared_score(self) -> None:
        await self.record("score", 5)
        for kind in ("query", "formal_query"):
            result = await self.record("first-query", 0, kind=kind, cooldown=10)
            self.assertEqual(result, {"accepted": True, "score": 5})
        await self.store.close()
        self.store = plugin_module.AffectionStore(self.path)
        await self.store.open()
        for kind in ("query", "formal_query"):
            result = await self.record("second-query", 0, kind=kind, cooldown=10, now=1001)
            self.assertEqual(result, {"accepted": False, "score": 5})
            result = await self.record("third-query", 0, kind=kind, cooldown=10, now=1010)
            self.assertEqual(result, {"accepted": True, "score": 5})

    async def test_concurrent_records_are_atomic(self) -> None:
        results = await asyncio.gather(*(self.record(f"m{i}", 1) for i in range(60)))
        self.assertEqual(sum(item["accepted"] for item in results), 60)
        self.assertEqual(await self.store.get("qq", "u1", 0), 60)
        duplicates = await asyncio.gather(*(self.record("same", 1) for _ in range(20)))
        self.assertEqual(sum(item["accepted"] for item in duplicates), 1)
        self.assertEqual(await self.store.get("qq", "u1", 0), 61)

    async def test_clamp_both_boundaries(self) -> None:
        await asyncio.gather(*(self.record(f"up{i}", 10) for i in range(15)))
        self.assertEqual(await self.store.get("qq", "u1", 0), 100)
        await asyncio.gather(*(self.record(f"down{i}", -10) for i in range(25)))
        self.assertEqual(await self.store.get("qq", "u1", 0), -100)

    async def test_failed_transaction_rolls_back(self) -> None:
        await self.store.run(
            self.store.db.execute,
            """
            CREATE TRIGGER test_failure BEFORE INSERT ON events
            WHEN NEW.event_id = 'bad'
            BEGIN SELECT RAISE(ABORT, 'test transaction failure'); END
        """,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.record("bad", 1)
        self.assertEqual(await self.store.get("qq", "u1", 7), 7)
        self.assertTrue((await self.record("good", 1))["accepted"])
        self.assertEqual(await self.store.get("qq", "u1", 0), 1)


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = plugin_module.IntentMatcher(["麦麦", "小麦"])

    def test_natural_query_examples_match_canonical_command_and_intent(self) -> None:
        cases = {
            "我的好感度是多少？": "score",
            "麦麦，查一下我的好感度": "score",
            "小麦你对我的好感度有多少分": "score",
            "请问我们的亲密度怎么样": "score",
            "@麦麦 你喜欢我吗？": "relationship",
            "你还爱我吗": "relationship",
            "我们现在是什么关系？": "relationship",
            "我在你心里有多重要": "relationship",
            "你对我的印象怎么样": "relationship",
            "你喜不喜欢我": "relationship",
            "你对我有没有好感": "relationship",
            "你把我当什么人": "relationship",
            "@麦麦 查询好感度": "formal",
            "麦麦，查看我的好感度": "formal",
            "查询你对我的好感度": "formal",
            "/affection": "formal",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                body, _, _ = self.matcher.body(text)
                pattern = plugin_module.FORMAL_COMMAND if expected == "formal" else plugin_module.QUERY_COMMAND
                other_pattern = plugin_module.QUERY_COMMAND if expected == "formal" else plugin_module.FORMAL_COMMAND
                self.assertIsNotNone(re.fullmatch(pattern, body))
                self.assertIsNone(re.fullmatch(other_pattern, body))
                self.assertEqual(self.matcher.query(body), expected)

    def test_unrelated_messages_are_not_queries(self) -> None:
        for text in (
            "这个游戏的好感度怎么计算",
            "他问你喜欢我吗",
            "你喜欢我做的饭吗",
            "你喜欢我的头像吗",
            "有人问我们的好感度是多少",
            "你的好感度是多少",
            "我和她是什么关系",
            "你对他的好感度是多少",
            "好感度数据库查询怎么写",
            "你喜欢我吗还是喜欢他",
            "@别人 我的好感度是多少",
            "今天心情怎么样",
        ):
            with self.subTest(text=text):
                body, _, _ = self.matcher.body(text)
                self.assertIsNone(self.matcher.query(body))

    def test_scoring_keeps_query_normalization_separate_from_attitude(self) -> None:
        body, named, punctuated = self.matcher.body("麦麦，谢谢你！")
        self.assertTrue(named)
        self.assertEqual(body, "谢谢你")
        self.assertEqual(punctuated, "麦麦，谢谢你！")
        for text, expected in (
            ("麦麦，谢谢你！", 1),
            ("我喜欢你", 2),
            ("我爱你", 3),
            ("我不喜欢你", -2),
            ("我讨厌你", -2),
            ("我恨你", -3),
            ("你是垃圾", -3),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.matcher.score(text, private=True, directed=True)[0], expected)

    def test_other_recipient_makes_implicit_you_ambiguous(self) -> None:
        self.assertEqual(self.matcher.score("你真好", private=False, directed=True, other_target=True)[0], 0)
        self.assertEqual(self.matcher.score("你真好", private=True, directed=True, other_target=True)[0], 0)
        self.assertEqual(self.matcher.score("麦麦，你真好", private=False, directed=True, other_target=True)[0], 1)

    def test_uncertain_sentiment_never_falls_back_to_basic_interaction(self) -> None:
        for text in (
            "你刚才那句话让我不舒服",
            "你可真聪明",
            "你真行啊",
            "他对我说谢谢你",
            "这个游戏真垃圾",
            "喜欢你的照片",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.matcher.score(text, private=True, directed=True, interaction=1)[0], 0)

    def test_greeting_uses_basic_category_and_does_not_cancel_rejection(self) -> None:
        self.assertEqual(self.matcher.score("你好", private=True, directed=True), (1, "基础交流"))
        self.assertEqual(self.matcher.score("你好", private=True, directed=True, interaction=0)[0], 0)
        self.assertEqual(self.matcher.score("你好，我讨厌你", private=True, directed=True)[0], -2)

    def test_unknown_complaint_or_threat_is_not_hidden_by_polite_words(self) -> None:
        for text in (
            "麦麦，我讨厌你，但是，别再敷衍我，谢谢你",
            "早安，小心我揍你",
            "麦麦，早安，这次你根本没听明白我说什么",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.matcher.score(text, private=True, directed=True)[0], 0)

    def test_nickname_as_subject_or_object_is_not_direct_address(self) -> None:
        for text in ("麦麦喜欢你", "你喜欢麦麦"):
            with self.subTest(text=text):
                self.assertEqual(self.matcher.score(text, private=True, directed=True)[0], 0)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.host = FakeHost()
        self.plugin = plugin_module.create_plugin()
        self.plugin.set_plugin_config({})
        self.plugin._set_context(
            PluginContext(
                "affection-tests",
                self.host.rpc,
                PluginPaths(Path(self.temp.name) / "data", Path(self.temp.name) / "runtime"),
            )
        )
        await self.plugin.on_load()
        self.unloaded = False

    async def asyncTearDown(self) -> None:
        if not self.unloaded:
            await self.plugin.on_unload()
        self.temp.cleanup()

    async def finish_replies(self) -> None:
        await asyncio.gather(*tuple(self.plugin._tasks))
        await asyncio.sleep(0)

    async def route_query(self, query: dict):
        """Host 的 after_process Hook → 命令匹配 → Command 顺序。"""
        hook = await self.plugin.record_interaction(query)
        routed = hook.get("modified_kwargs", {}).get("message", query)
        if re.fullmatch(plugin_module.FORMAL_COMMAND, routed["processed_plain_text"]):
            return await self.plugin.query_affection_formal(routed, routed["session_id"])
        if re.fullmatch(plugin_module.QUERY_COMMAND, routed["processed_plain_text"]):
            return await self.plugin.query_affection(routed, routed["session_id"])
        return None

    async def test_both_query_commands_are_registered_and_do_not_overlap(self) -> None:
        commands = {
            component["name"]: component for component in self.plugin.get_components() if component["type"] == "COMMAND"
        }
        self.assertEqual(set(commands), {"affection_query", "affection_query_formal"})
        for name, queries in (
            ("affection_query", ("你喜欢我吗", "我的好感度是多少", "查一下我的好感度")),
            ("affection_query_formal", ("查询好感度", "查看我的好感度", "查询你对我的好感度", "/affection")),
        ):
            for text in queries:
                with self.subTest(command=name, text=text):
                    matches = [
                        key
                        for key, component in commands.items()
                        if re.fullmatch(component["metadata"]["command_pattern"], text)
                    ]
                    self.assertEqual(matches, [name])

    async def test_formal_and_casual_queries_reply_with_same_score_inside_cooldown(self) -> None:
        await self.plugin.record_interaction(message("麦麦谢谢你", group=True, event="thanks"))
        with patch.object(plugin_module.time, "time", return_value=2000):
            for query in (
                message("查询好感度", group=True, at="12345", event="formal"),
                message("麦麦我的好感度是多少？", group=True, event="casual"),
            ):
                self.assertEqual(await self.route_query(query), (True, None, 2))
                await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 2)
        self.assertEqual(self.host.count("send.hybrid"), 2)
        self.assertEqual(
            [args["segments"][0] for args in self.host.arguments("send.hybrid")],
            [
                {"type": "reply", "data": {"target_message_id": "formal"}},
                {"type": "reply", "data": {"target_message_id": "casual"}},
            ],
        )
        formal, casual = [args["prompt"][0]["content"] for args in self.host.arguments("llm.generate")]
        for prompt in (formal, casual):
            self.assertIn("温柔", prompt)
            self.assertIn("轻松口语", prompt)
        self.assertIn("这是正式查询", formal)
        self.assertIn("当前好感度为 1 分", formal)
        self.assertIn("关系阶段为初识", formal)
        self.assertIn("严肃、清晰、克制", formal)
        self.assertIn("语气随意", casual)
        self.assertIn("真实好感度 1 分", casual)
        self.assertNotIn("这是正式查询", casual)
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 1)

    async def test_relationship_query_prompt_asks_for_everyday_personal_reply(self) -> None:
        query = message("麦麦，你喜不喜欢我？", group=True)
        self.assertEqual(await self.route_query(query), (True, None, 2))
        await self.finish_replies()
        args = self.host.arguments("llm.generate")[0]
        prompt = args["prompt"][0]["content"]
        self.assertIn("日常询问感情", prompt)
        self.assertIn("好感度 0", prompt)
        self.assertIn("日常感受", prompt)
        self.assertIn("语气随意", prompt)
        self.assertIn("温柔", prompt)
        self.assertNotIn("这是正式查询", prompt)
        self.assertEqual(args["prompt"][1]["content"], "说说你对我的感觉吧。")

    async def test_formal_group_query_requires_bot_address(self) -> None:
        for at in (None, "99999"):
            query = message("查询好感度", group=True, at=at)
            self.assertEqual(await self.route_query(query), (True, None, 0))
        for user, text, at in (
            ("at", "查询好感度", "12345"),
            ("named", "麦麦查询好感度", None),
            ("slash", "/affection", None),
        ):
            query = message(text, group=True, at=at, user=user, event=user)
            self.assertEqual(await self.route_query(query), (True, None, 2))
            await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 3)

    async def test_host_routing_normalizes_queries_before_command_matching(self) -> None:
        for i, text in enumerate(
            (
                "我的好感度是多少？",
                "麦麦，查一下我的好感度",
                "@麦麦 你喜欢我吗？",
                "小麦你对我的好感度有多少分",
                "请问我们的亲密度怎么样",
            )
        ):
            with self.subTest(text=text):
                query = message(text, user=f"u{i}", event=f"q{i}")
                self.assertEqual(await self.route_query(query), (True, None, 2))
                await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 5)

    async def test_host_routing_handles_trailing_at_quote_descriptions_and_voice(self) -> None:
        cases = (
            message(
                "你喜欢我吗？", group=True, at="12345", user="at", event="at", processed_plain_text="你喜欢我吗？ @麦麦"
            ),
            message(
                "我的好感度是多少",
                user="quote",
                event="quote",
                segments=[
                    quoted("之前说的其他话"),
                    {"type": "text", "data": "我的好感度是多少"},
                ],
                processed_plain_text="[回复之前说的其他话] 我的好感度是多少",
            ),
            message(
                "",
                user="voice",
                event="voice",
                segments=[{"type": "voice", "data": "[语音: 你喜欢我吗？]"}],
                processed_plain_text="[语音: 你喜欢我吗？]",
            ),
            message(
                "",
                group=True,
                at="12345",
                user="at-voice",
                event="at-voice",
                segments=[{"type": "voice", "data": "[语音: 你喜欢我吗？]"}],
                processed_plain_text="@麦麦 [语音: 你喜欢我吗？]",
            ),
            message(
                "",
                user="reply-voice",
                event="reply-voice",
                segments=[
                    quoted("以前的消息"),
                    {"type": "voice", "data": "[语音: 我的好感度是多少？]"},
                ],
                processed_plain_text="[回复以前的消息] [语音: 我的好感度是多少？]",
            ),
        )
        for query in cases:
            self.assertEqual(await self.route_query(query), (True, None, 2))
            await self.finish_replies()
        self.assertEqual(self.host.count("send.hybrid"), 5)

    async def test_host_routing_still_works_when_automatic_scoring_is_disabled(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["automatic"] = False
        self.plugin.set_plugin_config(data)
        query = message("麦麦，你喜欢我吗？", group=True)
        self.assertEqual(await self.route_query(query), (True, None, 2))
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)

    async def test_regular_messages_use_zero_model_calls(self) -> None:
        for i, (text, expected_score) in enumerate(
            (
                ("早上好", 1),
                ("谢谢你", 1),
                ("谢谢你", 1),
                ("你真好", 1),
                ("你是垃圾", -3),
                ("好感度系统怎么实现", 0),
            )
        ):
            self.assertEqual(
                await self.plugin.record_interaction(message(text, user=f"u{i}", event=f"m{i}")),
                {"action": "continue"},
            )
            self.assertEqual((await self.plugin.get_affection("qq", f"u{i}"))["score"], expected_score)
        self.assertEqual(self.host.count("llm.generate"), 0)
        self.assertEqual(self.host.count("send.hybrid"), 0)

    async def test_ambiguous_and_complex_complaints_do_not_get_basic_score(self) -> None:
        long_text = "今天做了好多事情，想跟你聊聊。" * 30
        self.assertGreater(len(long_text), 256)
        for i, text in enumerate(
            (
                "你刚才那句话让我不舒服",
                "你根本没听明白我说什么，算了",
                "我不是在夸你，你可真聪明",
                "你真行啊",
                "你看他，你说他是不是很烦",
                "这个游戏真垃圾",
                long_text,
            )
        ):
            user = f"uncertain-{i}"
            await self.plugin.record_interaction(message(text, group=True, at="12345", user=user, event=user))
            with self.subTest(text=text):
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], 0)
        event_count = await self.plugin.store.run(
            lambda: self.plugin.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        )
        self.assertEqual(event_count, 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_mentions_and_reply_sender_resolve_target_before_attitude(self) -> None:
        cases = (
            (
                message(
                    "你真好",
                    group=True,
                    at="12345",
                    user="other-at",
                    event="other-at",
                    segments=[{"type": "at", "data": {"target_user_id": "u2"}}, {"type": "text", "data": "你真好"}],
                ),
                0,
            ),
            (
                message(
                    "你真烦",
                    group=True,
                    at="12345",
                    user="other-quote",
                    event="other-quote",
                    segments=[quoted("麦麦，你真好", sender="u2"), {"type": "text", "data": "你真烦"}],
                ),
                0,
            ),
            (
                message(
                    "你真好",
                    user="private-quote",
                    event="private-quote",
                    segments=[quoted("谢谢你", sender="u2"), {"type": "text", "data": "你真好"}],
                ),
                0,
            ),
            (
                message(
                    "麦麦，你真好",
                    group=True,
                    at="12345",
                    user="explicit-target",
                    event="explicit-target",
                    segments=[quoted("你是垃圾", sender="u2"), {"type": "text", "data": "麦麦，你真好"}],
                ),
                1,
            ),
        )
        for interaction, expected in cases:
            user = interaction["message_info"]["user_info"]["user_id"]
            with self.subTest(user=user):
                await self.plugin.record_interaction(interaction)
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], expected)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_other_mention_starts_new_target_after_named_bot_clause(self) -> None:
        interaction = message(
            "",
            group=True,
            user="changed-target",
            event="changed-target",
            segments=[
                {"type": "text", "data": "麦麦谢谢你。"},
                {"type": "at", "data": {"target_user_id": "u2"}},
                {"type": "text", "data": "你真棒"},
            ],
            processed_plain_text="麦麦谢谢你。@群友 你真棒",
        )
        self.assertEqual(self.plugin._message_text(interaction), "麦麦谢谢你。\n你真棒")
        await self.plugin.record_interaction(interaction)
        self.assertEqual((await self.plugin.get_affection("qq", "changed-target"))["score"], 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_only_current_text_is_scored_and_nontext_does_not_join_words(self) -> None:
        extraction = message(
            "",
            segments=[
                quoted("旧的负面话语"),
                {"type": "text", "data": "麦麦，谢谢你！"},
                forwarded("历史表白"),
                {"type": "voice", "data": "[语音: 今天过得怎么样？]"},
            ],
        )
        self.assertEqual(self.plugin._message_text(extraction), "麦麦，谢谢你！\n今天过得怎么样？")
        cases = (
            (
                message(
                    "麦麦，谢谢你",
                    group=True,
                    user="quoted-current",
                    event="quoted-current",
                    segments=[quoted("麦麦，你是垃圾"), {"type": "text", "data": "麦麦，谢谢你"}],
                    processed_plain_text="[回复麦麦，你是垃圾] 麦麦，谢谢你",
                ),
                1,
            ),
            (
                message(
                    "早上好",
                    group=True,
                    at="12345",
                    user="forward-current",
                    event="forward-current",
                    segments=[forwarded("麦麦，我爱你"), {"type": "text", "data": "早上好"}],
                    processed_plain_text="[转发麦麦，我爱你] 早上好",
                ),
                1,
            ),
            (
                message(
                    "你真好",
                    group=True,
                    at="12345",
                    user="split-word",
                    event="split-word",
                    segments=[
                        {"type": "text", "data": "你真"},
                        {"type": "image", "data": "图片内容"},
                        {"type": "text", "data": "好"},
                    ],
                ),
                0,
            ),
        )
        for interaction, expected in cases:
            user = interaction["message_info"]["user_info"]["user_id"]
            with self.subTest(user=user):
                await self.plugin.record_interaction(interaction)
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], expected)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_multiple_clauses_are_capped_and_settled_once(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["score_cooldown_seconds"] = 0
        self.plugin.set_plugin_config(data)
        interaction = message(
            "麦麦，我爱你。麦麦，我爱你。",
            group=True,
            user="multi-clause",
            event="multi-clause",
        )
        await self.plugin.record_interaction(interaction)
        await self.plugin.record_interaction(interaction)
        self.assertEqual((await self.plugin.get_affection("qq", "multi-clause"))["score"], 3)
        event_count = await self.plugin.store.run(
            lambda: self.plugin.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        )
        self.assertEqual(event_count, 1)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_clear_addressed_and_private_basic_chat_scores_once(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["score_cooldown_seconds"] = 0
        self.plugin.set_plugin_config(data)
        cases = (
            message("今天过得怎么样", group=True, at="12345", user="at-basic", event="at-basic"),
            message("早上好", user="private-basic", event="private-basic"),
            message("麦麦我今天吃了火锅", group=True, user="named-basic", event="named-basic"),
            message(
                "",
                group=True,
                at="12345",
                user="voice-basic",
                event="voice-basic",
                segments=[{"type": "voice", "data": "[语音: 今天吃了火锅]"}],
                processed_plain_text="@麦麦 [语音: 今天吃了火锅]",
            ),
        )
        for interaction in cases:
            with self.subTest(user=interaction["message_info"]["user_info"]["user_id"]):
                await self.plugin.record_interaction(interaction)
                await self.plugin.record_interaction(interaction)
                user = interaction["message_info"]["user_info"]["user_id"]
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], 1)
        self.assertEqual(self.host.count("llm.generate"), 0)
        self.assertEqual(self.host.count("send.hybrid"), 0)

    async def test_basic_scoring_ignores_unaddressed_empty_quoted_and_command_messages(self) -> None:
        cases = (
            message("今天吃了火锅", group=True, user="unaddressed"),
            message("今天吃了火锅", group=True, at="99999", user="other-at"),
            message("", group=True, at="12345", user="empty-at"),
            message(" ，。 ", group=True, at="12345", user="punctuation-only"),
            message(
                "",
                group=True,
                at="12345",
                user="quoted-history",
                segments=[
                    quoted("谢谢你，麦麦"),
                ],
                processed_plain_text="@麦麦 [回复谢谢你，麦麦]",
            ),
            message(
                "",
                group=True,
                at="12345",
                user="forwarded-history",
                segments=[
                    forwarded("麦麦，我喜欢你"),
                ],
                processed_plain_text="@麦麦 [转发麦麦，我喜欢你]",
            ),
            message("/help", group=True, at="12345", user="group-command"),
            message("/other", user="private-command"),
        )
        for interaction in cases:
            await self.plugin.record_interaction(interaction)
            user = interaction["message_info"]["user_info"]["user_id"]
            with self.subTest(user=user):
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_explicit_negative_overrides_basic_interaction_score(self) -> None:
        for i, (text, expected) in enumerate(
            (("我不喜欢你", -2), ("我讨厌你", -2), ("我恨你", -3), ("你真烦", -1), ("别烦我", -1))
        ):
            await self.plugin.record_interaction(
                message(text, group=True, at="12345", user=f"negative-{i}", event=f"negative-{i}")
            )
            self.assertEqual((await self.plugin.get_affection("qq", f"negative-{i}"))["score"], expected)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_turning_clause_only_replaces_attitude_about_same_target(self) -> None:
        for i, (text, expected) in enumerate(
            (
                ("麦麦，你真好，但是这个游戏真垃圾", 1),
                ("麦麦，你真好，但是麦麦，你真烦", -1),
            )
        ):
            user = f"turning-{i}"
            await self.plugin.record_interaction(message(text, group=True, at="12345", user=user, event=user))
            with self.subTest(text=text):
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], expected)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_query_hook_does_not_add_basic_interaction_score(self) -> None:
        for i, text in enumerate(("你喜欢我吗", "我的好感度是多少", "查询好感度", "/affection")):
            await self.plugin.record_interaction(message(text, group=True, at="12345", user=f"query-{i}"))
            self.assertEqual((await self.plugin.get_affection("qq", f"query-{i}"))["score"], 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_zero_interaction_step_keeps_explicit_emotion_scoring(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["interaction_step"] = 0
        data["affection"]["positive_step"] = 3
        self.plugin.set_plugin_config(data)
        cases = (
            (message("今天吃了火锅", group=True, at="12345", user="basic"), 0),
            (message("这周有点忙", user="private"), 0),
            (message("麦麦，谢谢你", group=True, user="praise"), 3),
            (message("我讨厌你", group=True, at="12345", user="negative"), -2),
        )
        for interaction, expected_score in cases:
            user = interaction["message_info"]["user_info"]["user_id"]
            interaction["message_id"] = user
            await self.plugin.record_interaction(interaction)
            self.assertEqual((await self.plugin.get_affection("qq", user))["score"], expected_score)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_basic_chat_and_praise_have_independent_persistent_cooldowns(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["positive_step"] = 4
        self.plugin.set_plugin_config(data)
        with patch.object(plugin_module.time, "time", return_value=1000):
            await self.plugin.record_interaction(message("今天吃了火锅", group=True, at="12345", event="first-basic"))
        with patch.object(plugin_module.time, "time", return_value=1001):
            await self.plugin.record_interaction(message("谢谢你", group=True, at="12345", event="early-praise"))
            self.assertFalse((await self.plugin.adjust_affection("qq", "u1", "stream-1", "early-api", 3))["accepted"])
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 4)
        await self.plugin.on_unload()
        with patch.object(plugin_module.time, "time", return_value=1200):
            await self.plugin.on_load()
        with patch.object(plugin_module.time, "time", return_value=1200):
            await self.plugin.record_interaction(message("早上好", group=True, at="12345", event="after-restart"))
            await self.plugin.record_interaction(
                message("谢谢你", group=True, at="12345", event="praise-after-restart")
            )
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 4)
        with patch.object(plugin_module.time, "time", return_value=1300):
            await self.plugin.record_interaction(message("早上好", group=True, at="12345", event="later-basic"))
        with patch.object(plugin_module.time, "time", return_value=1301):
            await self.plugin.record_interaction(message("谢谢你", group=True, at="12345", event="later-praise"))
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 8)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_events_store_categories_instead_of_chat_or_api_reason_text(self) -> None:
        await self.plugin.record_interaction(message("麦麦，谢谢你！", user="thanks-user", event="thanks-label"))
        await self.plugin.record_interaction(message("麦麦，我讨厌你", user="negative-user", event="negative-label"))
        await self.plugin.adjust_affection(
            "qq", "api-user", "stream-1", "api-label", 1, reason="用户传来的详细原始聊天内容，不应写进好感度库"
        )
        reasons = await self.plugin.store.run(
            lambda: dict(self.plugin.store.db.execute("SELECT event_id,reason FROM events"))
        )
        self.assertEqual(reasons, {"thanks-label": "感谢", "negative-label": "反感", "api-label": "插件上报"})
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_public_adjust_api_preserves_limits_and_message_deduplication(self) -> None:
        for delta in (0, 11, -11, 1.5, True, "1"):
            with self.subTest(delta=delta):
                result = await self.plugin.adjust_affection("qq", "u1", "stream-1", "invalid", delta)
                self.assertFalse(result["accepted"])
        self.assertFalse((await self.plugin.adjust_affection("qq", "u1", "stream-1", "", 1))["accepted"])
        with patch.object(plugin_module.time, "time", return_value=1000):
            first = await self.plugin.adjust_affection("qq", "u1", "stream-1", "external", 10)
            duplicate = await self.plugin.adjust_affection("qq", "u1", "stream-1", "external", -10)
            await self.plugin.record_interaction(message("我爱你", event="external"))
        self.assertEqual(first, {"accepted": True, "score": 10})
        self.assertEqual(duplicate, {"accepted": False, "score": 10})
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 10)
        with patch.object(plugin_module.time, "time", return_value=1001):
            opposite = await self.plugin.adjust_affection("qq", "u1", "stream-1", "opposite", -10)
        self.assertEqual(opposite, {"accepted": True, "score": 0})
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_query_intercepts_and_generates_once(self) -> None:
        await self.plugin.record_interaction(message("谢谢你", event="thanks"))
        result = await self.plugin.query_affection(message("我的好感度是多少", event="query"), "stream-1")
        self.assertEqual(result, (True, None, 2))
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)
        self.assertEqual(self.host.count("send.hybrid"), 1)
        generate = self.host.arguments("llm.generate")[0]
        prompt = generate["prompt"][0]["content"]
        self.assertIn("温柔", prompt)
        self.assertIn("轻松口语", prompt)
        self.assertIn("真实好感度 1", prompt)
        self.assertEqual(generate["task_name"], "replyer")
        self.assertEqual(generate["max_tokens"], self.plugin.config.reply.max_tokens)
        send = self.host.arguments("send.hybrid")[0]
        self.assertEqual(
            send["segments"],
            [
                {"type": "reply", "data": {"target_message_id": "query"}},
                {"type": "text", "data": self.host.reply},
            ],
        )
        self.assertEqual(send["stream_id"], "stream-1")
        self.assertTrue(send["sync_to_maisaka_history"])

    async def test_model_rpc_uses_90_second_transport_timeout(self) -> None:
        await self.plugin.query_affection(message("你喜欢我吗", event="timeout-query"), "stream-1")
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)
        self.assertEqual([timeout for cap, timeout in self.host.rpc_timeouts if cap == "llm.generate"], [90000])
        self.assertNotIn("timeout_ms", self.host.arguments("llm.generate")[0])
        self.assertEqual(self.host.count("send.hybrid"), 1)

    async def test_query_cooldown_and_duplicates_still_intercept(self) -> None:
        for event in ("q1", "q1", "q2"):
            self.assertEqual(
                await self.plugin.query_affection(message("你喜欢我吗", event=event), "stream-1"), (True, None, 2)
            )
            await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)
        self.assertEqual(self.host.count("send.hybrid"), 1)

    async def test_group_needs_name_or_bot_mention(self) -> None:
        for event, kwargs in (
            ("unaddressed", {}),
            ("other-at", {"at": "99999"}),
        ):
            result = await self.plugin.query_affection(
                message("你喜欢我吗", group=True, event=event, **kwargs), "stream-1"
            )
            self.assertEqual(result, (True, None, 0))
        await self.plugin.record_interaction(message("谢谢你", group=True, event="unaddressed-score"))
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 0)
        for user, text, kwargs in (
            ("named", "麦麦你喜欢我吗", {}),
            ("mentioned", "你喜欢我吗", {"at": "12345"}),
            ("explicit", "/affection", {}),
        ):
            self.assertEqual(
                await self.plugin.query_affection(
                    message(text, group=True, user=user, event=user, **kwargs), "stream-1"
                ),
                (True, None, 2),
            )
        await self.finish_replies()
        self.assertEqual(self.host.count("send.hybrid"), 3)

    async def test_non_qq_group_uses_route_account(self) -> None:
        query = message(
            "我的好感度是多少", platform="telegram", group=True, at="bot-tg", route={"platform_io_account_id": "bot-tg"}
        )
        self.assertEqual(await self.plugin.query_affection(query, "stream-1"), (True, None, 2))
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)

    async def test_unrelated_and_quoted_content_pass_through(self) -> None:
        for i, query in enumerate(
            (
                message("他问你喜欢我吗"),
                message("", segments=[quoted("你喜欢我吗")]),
                message("", segments=[forwarded("我的好感度是多少")]),
            )
        ):
            query["message_id"] = f"not-query-{i}"
            self.assertEqual(await self.plugin.query_affection(query, "stream-1"), (True, None, 0))
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_voice_uses_host_transcription(self) -> None:
        query = message(
            "",
            segments=[{"type": "voice", "data": "[语音: 你喜欢我吗]"}],
            processed_plain_text="[语音: 你喜欢我吗]",
        )
        self.assertEqual(await self.plugin.query_affection(query, "stream-1"), (True, None, 2))
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)

    async def test_voice_only_unwraps_complete_host_transcription(self) -> None:
        for i, (data, expected_text, expected_route) in enumerate(
            (
                ("[语音: 你喜欢我吗？]", "你喜欢我吗？", (True, None, 2)),
                ("你喜欢我吗]", "你喜欢我吗]", None),
                ("[语音消息]", "", None),
                ("[语音消息，转录失败]", "", None),
            )
        ):
            with self.subTest(voice_data=data):
                query = message(
                    "",
                    user=f"voice-wrap-{i}",
                    event=f"voice-wrap-{i}",
                    segments=[{"type": "voice", "data": data}],
                    processed_plain_text=data,
                )
                self.assertEqual(self.plugin._message_text(query), expected_text)
                self.assertEqual(await self.route_query(query), expected_route)
                await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 1)

    async def test_voice_placeholders_do_not_add_basic_interaction_score(self) -> None:
        for i, data in enumerate(("[语音消息]", "[语音消息，转录失败]")):
            with self.subTest(voice_data=data):
                query = message(
                    "",
                    group=True,
                    at="12345",
                    user=f"placeholder-{i}",
                    event=f"placeholder-{i}",
                    segments=[{"type": "voice", "data": data}],
                    processed_plain_text=f"@麦麦 {data}",
                )
                await self.plugin.record_interaction(query)
                self.assertEqual((await self.plugin.get_affection("qq", f"placeholder-{i}"))["score"], 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_successful_host_asr_scores_current_utterance_without_model_call(self) -> None:
        for i, (text, expected) in enumerate(
            (("谢谢你", 1), ("我喜欢你", 2), ("我爱你", 3), ("我恨你", -3), ("你刚才那句话让我不舒服", 0))
        ):
            user = f"asr-score-{i}"
            interaction = message(
                "",
                group=True,
                at="12345",
                user=user,
                event=user,
                segments=[{"type": "voice", "data": f"[语音: {text}]"}],
                processed_plain_text=f"@麦麦 [语音: {text}]",
            )
            with self.subTest(text=text):
                await self.plugin.record_interaction(interaction)
                self.assertEqual((await self.plugin.get_affection("qq", user))["score"], expected)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_bot_config_reload_updates_name_and_persona(self) -> None:
        await self.plugin.on_config_update(
            ON_BOT_CONFIG_RELOAD,
            {
                "bot": {"nickname": "猫猫", "alias_names": ["小猫"], "qq_account": "777"},
                "personality": {"personality": "直率，爱说喵。", "reply_style": "俏皮短句。"},
            },
            "2",
        )
        self.assertEqual(
            await self.plugin.query_affection(message("麦麦你喜欢我吗", group=True), "stream-1"), (True, None, 0)
        )
        self.assertEqual(
            await self.plugin.query_affection(message("猫猫你喜欢我吗", group=True, event="new"), "stream-1"),
            (True, None, 2),
        )
        await self.finish_replies()
        prompt = self.host.arguments("llm.generate")[0]["prompt"][0]["content"]
        self.assertIn("你是猫猫", prompt)
        self.assertIn("爱说喵", prompt)
        self.assertNotIn("温柔", prompt)

    async def test_self_config_reload_changes_settings(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["affection"]["extra_names"] = ["阿麦"]
        data["affection"]["positive_step"] = 4
        data["reply"]["max_tokens"] = 72
        data["reply"]["timeout_seconds"] = 60
        self.plugin.set_plugin_config(data)
        await self.plugin.on_config_update("self", data, "2")
        await self.plugin.record_interaction(message("阿麦，谢谢你", group=True, event="new-score"))
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 3)
        await self.plugin.query_affection(message("阿麦你喜欢我吗", group=True, event="new-query"), "stream-1")
        await self.finish_replies()
        self.assertEqual(self.host.arguments("llm.generate")[0]["max_tokens"], 72)
        self.assertEqual([timeout for cap, timeout in self.host.rpc_timeouts if cap == "llm.generate"], [60000])

    async def test_disabled_plugin_passes_through_and_does_not_record(self) -> None:
        data = self.plugin.get_plugin_config_data()
        data["plugin"]["enabled"] = False
        self.plugin.set_plugin_config(data)
        await self.plugin.on_config_update("self", data, "2")
        self.assertEqual(await self.plugin.query_affection(message("你喜欢我吗"), "stream-1"), (True, None, 0))
        await self.plugin.record_interaction(message("谢谢你"))
        self.assertEqual((await self.plugin.get_affection("qq", "u1"))["score"], 0)
        self.assertEqual(self.host.count("llm.generate"), 0)

    async def test_concurrent_queries_respect_max_inflight(self) -> None:
        self.host.generate_gate = asyncio.Event()
        data = self.plugin.get_plugin_config_data()
        data["reply"]["max_inflight"] = 2
        self.plugin.set_plugin_config(data)
        results = await asyncio.gather(
            *(
                self.plugin.query_affection(message("你喜欢我吗", user=f"u{i}", event=f"q{i}"), "stream-1")
                for i in range(8)
            )
        )
        self.assertEqual(len(self.plugin._tasks), 2)
        await asyncio.wait_for(self.host.generate_started.wait(), timeout=1)
        self.assertTrue(all(result == (True, None, 2) for result in results))
        self.assertLessEqual(self.host.count("llm.generate"), 2)
        self.host.generate_gate.set()
        await self.finish_replies()
        self.assertEqual(self.host.count("llm.generate"), 2)
        self.assertEqual(self.host.count("send.hybrid"), 2)
        self.assertEqual(
            {args["segments"][0]["data"]["target_message_id"] for args in self.host.arguments("send.hybrid")},
            {"q0", "q1"},
        )

    async def test_unload_cancels_pending_generation_and_closes_store(self) -> None:
        self.host.generate_gate = asyncio.Event()
        await self.plugin.query_affection(message("你喜欢我吗"), "stream-1")
        await asyncio.wait_for(self.host.generate_started.wait(), timeout=1)
        await self.plugin.on_unload()
        self.unloaded = True
        self.assertEqual(self.host.cancelled_generations, 1)
        self.assertEqual(self.host.count("send.hybrid"), 0)
        self.assertEqual(len(self.plugin._tasks), 0)
        with self.assertRaises(RuntimeError):
            await self.plugin.store.get("qq", "u1", 0)


if __name__ == "__main__":
    unittest.main()
