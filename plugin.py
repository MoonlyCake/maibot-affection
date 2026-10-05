"""MaiBot 好感度：本地记分、自然语言接管、人设回复。"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from maibot_sdk import (
    API,
    ON_BOT_CONFIG_RELOAD,
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
)
from maibot_sdk.types import HookMode, HookOrder

SCORE_PHRASES = (
    r"(?:你对我的|我的?|我们(?:的)?|咱俩(?:的)?)?(?:好感度|好感|亲密度)"
    r"(?:现在|目前)?(?:是|有|还剩)?(?:多少(?:分)?|几分|多高|怎么样|如何|咋样)",
    r"(?:查|查询|查看|看看|查查)(?:一下|下)?(?:你对我的|我的|我们(?:的)?|咱俩(?:的)?)?"
    r"(?:好感度|好感|亲密度)",
    r"你对我(?:现在|目前)?(?:有)?(?:多少|几分)(?:好感度|好感)",
)
RELATION_PHRASES = (
    r"你(?:现在|还|到底)?(?:喜欢|爱|在乎|讨厌|嫌弃)我(?:吗|么|嘛|不)",
    r"你(?:现在|还)?(?:喜不喜欢|爱不爱|在不在乎|讨不讨厌|嫌不嫌弃)我",
    r"你(?:是否|是不是|还会不会)(?:喜欢|爱|在乎|讨厌|嫌弃)我(?:吗|呢)?",
    r"你(?:有多|多么)(?:喜欢|爱|在乎|讨厌)我",
    r"你(?:现在|还)?对我(?:有感觉|有好感|满意)(?:吗|么|不)",
    r"你对我(?:有没有|还有没有)(?:好感|感觉)",
    r"你(?:现在|到底)?对我(?:的)?(?:印象|感觉|看法|评价)(?:是)?(?:怎么样|如何|咋样|是什么)",
    r"你(?:觉得|认为)我(?:这个人)?(?:怎么样|如何|咋样)",
    r"我在你(?:的)?心里(?:到底|现在)?(?:算什么|算啥|是什么|算什么关系|是什么位置|有多重要)",
    r"(?:我们|咱俩|我和你|你和我)(?:现在|到底)?(?:算|是)?(?:什么|啥)(?:关系)",
    r"你(?:有没有|是否有|是不是有)把我当(?:朋友|好朋友|自己人)(?:吗|呢)?",
    r"你(?:现在|到底)?把我当(?:什么|啥)(?:人)?",
)
SCORE_RE = re.compile("(?:" + "|".join(SCORE_PHRASES) + ")")
RELATION_RE = re.compile("(?:" + "|".join(RELATION_PHRASES) + ")")
QUERY_BODY = "(?:" + "|".join((*SCORE_PHRASES, *RELATION_PHRASES)) + ")"
FORMAL_BODY = r"(?:查询|查看)(?:我的|你对我的)?好感度"
FORMAL_RE = re.compile(r"(?:/affection|" + FORMAL_BODY + r")")
# Hook 先整理真正的查询正文，Command 在 Host 注册会话后接管。
FORMAL_COMMAND = r"^(?:/affection|" + FORMAL_BODY + r")$"
QUERY_COMMAND = r"^(?!" + FORMAL_RE.pattern + r"$)" + QUERY_BODY + r"$"
POSITIVE_RE = re.compile(
    r"(?:谢谢(?:你)?|多谢(?:你)?|感谢(?:你)?|辛苦(?:你)?了|你真棒|你太棒了|你真好|"
    r"你真可爱|你很可爱|喜欢你|我喜欢你|我很喜欢你|我爱你|你帮了我大忙)"
    r"(?:谢谢(?:你)?|辛苦(?:你)?了|你真好)*"
)
NEGATIVE_RE = re.compile(r"(?:你(?:真是|就是|是)?(?:垃圾|废物|傻逼|蠢货)|滚(?:开|吧)?|闭嘴)")
SPACE_PUNCT_RE = re.compile(r"[\s，,：:。！？?!~～]")


class PluginSettings(PluginConfigBase):
    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="启用好感度插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class AffectionSettings(PluginConfigBase):
    __ui_label__ = "好感度"

    initial_score: int = Field(default=0, ge=-100, le=100, description="新用户初始值；0 表示初识")
    automatic: bool = Field(default=True, description="按明确的感谢、赞美、辱骂记录好感变化")
    positive_step: int = Field(default=1, ge=1, le=10, description="正向互动加分")
    negative_step: int = Field(default=2, ge=1, le=10, description="负向互动扣分")
    score_cooldown_seconds: int = Field(default=300, ge=0, description="同一用户同方向记分间隔，重启后保留")
    group_requires_address: bool = Field(default=True, description="群聊查询与记分需 @ 麦麦或以麦麦称呼开头")
    extra_names: list[str] = Field(default_factory=list, description="额外称呼；自动读取麦麦昵称与别名")


class ReplySettings(PluginConfigBase):
    __ui_label__ = "查询回复"

    cooldown_seconds: int = Field(default=10, ge=0, description="同一用户查询间隔，重复查询静默接管")
    max_inflight: int = Field(default=4, ge=1, le=32, description="最多同时生成多少条查询回复")
    max_tokens: int = Field(default=160, ge=32, le=2048, description="单条回复的最大输出 Token")
    timeout_seconds: int = Field(default=90, ge=1, le=120, description="回复任务与模型 RPC 的超时秒数")
    persona_max_chars: int = Field(default=1200, ge=100, le=8000, description="人设与说话风格总字符预算")


class AffectionConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    affection: AffectionSettings = Field(default_factory=AffectionSettings)
    reply: ReplySettings = Field(default_factory=ReplySettings)


class IntentMatcher:
    def __init__(self, names: list[str]) -> None:
        self.names = sorted({name.strip() for name in names if name.strip()}, key=len, reverse=True)

    def body(self, text: str) -> tuple[str, bool]:
        text = text.strip()
        named = False
        for name in self.names:
            for prefix in ("@" + name, name):
                if text.startswith(prefix):
                    text = text[len(prefix) :].lstrip(" ，,：:")
                    named = True
                    break
            if named:
                break
        text = SPACE_PUNCT_RE.sub("", text)
        for prefix in ("请问", "问一下", "我想知道", "告诉我", "能告诉我"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        return text.rstrip("呀呢啊哦啦"), named

    def query(self, body: str) -> str | None:
        if FORMAL_RE.fullmatch(body):
            return "formal"
        if SCORE_RE.fullmatch(body):
            return "score"
        if RELATION_RE.fullmatch(body):
            return "relationship"
        return None

    def delta(self, body: str, positive: int, negative: int) -> int:
        if POSITIVE_RE.fullmatch(body):
            return positive
        if NEGATIVE_RE.fullmatch(body):
            return -negative
        return 0


def relationship(score: int) -> str:
    for boundary, label in (
        (-40, "排斥"),
        (-10, "疏远"),
        (20, "初识"),
        (50, "熟悉"),
        (80, "亲近"),
    ):
        if score < boundary:
            return label
    return "信任"


class AffectionStore:
    """连接与 SQL 全部在同一工作线程执行；事务内不等待模型。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="affection-db")

    async def run(self, operation: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self.worker, operation, *args)

    async def open(self) -> None:
        try:
            await self.run(self._open)
        except Exception:
            if hasattr(self, "db"):
                await self.run(self.db.close)
            self.worker.shutdown(wait=False)
            raise

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=3)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS affection (
                platform TEXT NOT NULL,
                user_id TEXT NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN -100 AND 100),
                last_positive REAL NOT NULL DEFAULT 0,
                last_negative REAL NOT NULL DEFAULT 0,
                last_query REAL NOT NULL DEFAULT 0,
                last_formal_query REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(platform, user_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS events (
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
        """)

    def _ensure_user(self, platform: str, user_id: str, initial: int) -> sqlite3.Row:
        self.db.execute(
            "INSERT OR IGNORE INTO affection(platform,user_id,score) VALUES(?,?,?)",
            (platform, user_id, initial),
        )
        return self.db.execute(
            "SELECT * FROM affection WHERE platform=? AND user_id=?",
            (platform, user_id),
        ).fetchone()

    def _record(
        self,
        platform: str,
        user_id: str,
        stream_id: str,
        event_id: str,
        kind: str,
        delta: int,
        initial: int,
        cooldown: int,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._ensure_user(platform, user_id, initial)
            if kind == "formal_query":
                clock = "last_formal_query"
            elif kind == "query":
                clock = "last_query"
            else:
                clock = "last_positive" if delta > 0 else "last_negative"
            allowed = now >= row[clock] + cooldown
            score = max(-100, min(100, row["score"] + delta)) if allowed else row["score"]
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    platform,
                    stream_id,
                    event_id,
                    kind,
                    user_id,
                    now,
                    score - row["score"],
                    score,
                    reason[:80],
                ),
            ).rowcount
            if not inserted or not allowed:
                return {"accepted": False, "score": row["score"]}
            self.db.execute(
                f"UPDATE affection SET score=?, {clock}=? WHERE platform=? AND user_id=?",
                (score, now, platform, user_id),
            )
            return {"accepted": True, "score": score}

    async def record(
        self,
        platform: str,
        user_id: str,
        stream_id: str,
        event_id: str,
        kind: str,
        delta: int,
        initial: int,
        cooldown: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return await self.run(
            self._record,
            platform,
            user_id,
            stream_id,
            event_id,
            kind,
            delta,
            initial,
            cooldown,
            reason,
            time.time(),
        )

    def _get(self, platform: str, user_id: str, initial: int) -> int:
        row = self.db.execute(
            "SELECT score FROM affection WHERE platform=? AND user_id=?",
            (platform, user_id),
        ).fetchone()
        return row[0] if row else initial

    async def get(self, platform: str, user_id: str, initial: int) -> int:
        return await self.run(self._get, platform, user_id, initial)

    async def close(self) -> None:
        await self.run(self.db.close)
        self.worker.shutdown(wait=False)


class AffectionPlugin(MaiBotPlugin):
    config_model = AffectionConfig
    config_reload_subscriptions = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        super().__init__()
        self.store: AffectionStore | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_load(self) -> None:
        nickname, aliases, persona, style, account = await asyncio.gather(
            self.ctx.config.get("bot.nickname", "麦麦"),
            self.ctx.config.get("bot.alias_names", []),
            self.ctx.config.get("personality.personality", ""),
            self.ctx.config.get("personality.reply_style", ""),
            self.ctx.config.get("bot.qq_account", ""),
        )
        self._bot = {
            "nickname": nickname,
            "alias_names": aliases,
            "qq_account": account,
        }
        self._personality = {"personality": persona, "reply_style": style}
        self._refresh_persona()
        store = AffectionStore(self.ctx.paths.data_dir / "affection.sqlite3")
        await store.open()
        self.store = store
        self.ctx.logger.info("好感度插件已加载：本地记分 + 自然语言查询")

    def _refresh_persona(self) -> None:
        self.matcher = IntentMatcher(
            [
                self._bot["nickname"],
                *self._bot["alias_names"],
                *self.config.affection.extra_names,
            ]
        )
        self._persona = (
            f"你是{self._bot['nickname']}。{self._personality['personality']}\n"
            f"说话风格：{self._personality['reply_style']}"
        )[: self.config.reply.persona_max_chars]

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == ON_BOT_CONFIG_RELOAD:
            self._bot = config_data["bot"]
            self._personality = config_data["personality"]
        self._refresh_persona()

    async def on_unload(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.store is not None:
            await self.store.close()

    def _message_text(self, message: dict[str, Any]) -> str:
        # voice.data 是 Host 完成的转写；不用带 @、引用描述的整体 processed_plain_text。
        parts = []
        for segment in message["raw_message"]:
            if segment["type"] not in ("text", "voice"):
                continue
            text = segment["data"]
            if segment["type"] == "voice" and text.startswith("[语音: ") and text.endswith("]"):
                text = text[5:-1].strip()
            parts.append(text)
        return "".join(parts)

    def _addressed(self, message: dict[str, Any], named: bool) -> bool:
        info = message["message_info"]
        if not info.get("group_info") or named:
            return True
        route = info.get("additional_config") or {}
        account = next(
            (
                str(route[key])
                for key in (
                    "platform_io_account_id",
                    "account_id",
                    "self_id",
                    "bot_account",
                )
                if route.get(key)
            ),
            str(self._bot["qq_account"]) if message["platform"] == "qq" else "",
        )
        return bool(account) and any(
            segment["type"] == "at" and str(segment["data"]["target_user_id"]) == account
            for segment in message["raw_message"]
        )

    @HookHandler(
        "chat.receive.after_process",
        name="affection_record",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=4000,
        description="只对明确指向麦麦的互动本地记分",
    )
    async def record_interaction(self, message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        settings = self.config.affection
        if not self.config.plugin.enabled or message.get("is_notify"):
            return {"action": "continue"}
        text = self._message_text(message)
        if len(text) > 256:
            return {"action": "continue"}
        body, named = self.matcher.body(text)
        if settings.group_requires_address and not self._addressed(message, named) and body != "/affection":
            return {"action": "continue"}
        if self.matcher.query(body):
            # 排除 Host 的引用描述、@ 展示位置和 ASR 包装，保留用户当前这句话。
            message["processed_plain_text"] = body
            return {"action": "continue", "modified_kwargs": {"message": message}}
        if not settings.automatic:
            return {"action": "continue"}
        delta = self.matcher.delta(body, settings.positive_step, settings.negative_step)
        if delta:
            await self.store.record(
                message["platform"],
                str(message["message_info"]["user_info"]["user_id"]),
                message["session_id"],
                str(message["message_id"]),
                "score",
                delta,
                settings.initial_score,
                settings.score_cooldown_seconds,
                body,
            )
        return {"action": "continue"}

    @Command(
        "affection_query",
        pattern=QUERY_COMMAND,
        description="自然地询问关系或好感度，按人设随意回复",
    )
    async def query_affection(
        self,
        message: dict[str, Any],
        stream_id: str,
        **kwargs: Any,
    ) -> tuple[bool, None, int]:
        return await self._start_query(message, stream_id)

    @Command(
        "affection_query_formal",
        pattern=FORMAL_COMMAND,
        description="@ 麦麦 查询好感度，或 /affection；严肃说明分数与关系",
    )
    async def query_affection_formal(
        self,
        message: dict[str, Any],
        stream_id: str,
        **kwargs: Any,
    ) -> tuple[bool, None, int]:
        return await self._start_query(message, stream_id)

    async def _start_query(self, message: dict[str, Any], stream_id: str) -> tuple[bool, None, int]:
        if not self.config.plugin.enabled:
            return True, None, 0
        text = self._message_text(message)
        if len(text) > 180:
            return True, None, 0
        body, named = self.matcher.body(text)
        kind = self.matcher.query(body)
        if kind is None:
            return True, None, 0
        if (
            self.config.affection.group_requires_address
            and not self._addressed(message, named)
            and body != "/affection"
        ):
            return True, None, 0
        if len(self._tasks) >= self.config.reply.max_inflight:
            return True, None, 2
        # 在首次 await 之前占用任务名额，并发查询不会越过 max_inflight。
        task = asyncio.create_task(self._reply(message, stream_id, kind))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True, None, 2

    async def _reply(self, message: dict[str, Any], stream_id: str, kind: str) -> None:
        try:
            async with asyncio.timeout(self.config.reply.timeout_seconds):
                info = message["message_info"]["user_info"]
                result = await self.store.record(
                    message["platform"],
                    str(info["user_id"]),
                    stream_id,
                    str(message["message_id"]),
                    "formal_query" if kind == "formal" else "query",
                    0,
                    self.config.affection.initial_score,
                    self.config.reply.cooldown_seconds,
                )
                if not result["accepted"]:
                    return
                score = result["score"]
                if kind == "formal":
                    direction = (
                        f"这是正式查询。认真说明当前好感度为 {score} 分（范围 -100～100），"
                        f"关系阶段为{relationship(score)}。语气严肃、清晰、克制。"
                    )
                elif kind == "score":
                    direction = (
                        f"日常查询。当前关系：{relationship(score)}；真实好感度 {score} 分（-100～100）。"
                        "自然提到分数，语气随意。"
                    )
                else:
                    direction = (
                        f"日常询问感情。当前关系：{relationship(score)}；好感度 {score}。"
                        "用日常感受表达关系亲疏，语气随意。"
                    )
                prompt = (
                    f"{self._persona}\n{direction}"
                    "用第一人称回复提问者，保持人物性格，1～2句，80字以内，仅输出聊天正文。"
                )
                result = await self.ctx.llm.generate(
                    prompt=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "查询好感度。" if kind == "formal" else "说说你对我的感觉吧。"},
                    ],
                    task_name="replyer",
                    max_tokens=self.config.reply.max_tokens,
                    timeout_ms=self.config.reply.timeout_seconds * 1000,
                )
                if not result["success"] or not result["response"].strip():
                    self.ctx.logger.warning("好感度回复生成失败，stream_id=%s", stream_id)
                    return
                sent = await self.ctx.send.hybrid(
                    [
                        {"type": "reply", "data": {"target_message_id": str(message["message_id"])}},
                        {"type": "text", "data": result["response"].strip()},
                    ],
                    stream_id,
                    sync_to_maisaka_history=True,
                )
                if not sent:
                    self.ctx.logger.warning("好感度回复发送失败，stream_id=%s", stream_id)
        except Exception:
            self.ctx.logger.exception("好感度回复任务失败，stream_id=%s", stream_id)

    @API("get_affection", description="按平台、用户读取好感度", public=True)
    async def get_affection(self, platform: str, user_id: str, **kwargs: Any) -> dict[str, Any]:
        score = await self.store.get(platform, user_id, self.config.affection.initial_score)
        return {"score": score, "relationship": relationship(score)}

    @API(
        "adjust_affection",
        description="其他插件按真实事件增减好感度；相同 event_id 只记录一次",
        public=True,
    )
    async def adjust_affection(
        self,
        platform: str,
        user_id: str,
        stream_id: str,
        event_id: str,
        delta: int,
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"accepted": False, "reason": "插件已禁用"}
        if not event_id or type(delta) is not int or not -10 <= delta <= 10 or delta == 0:
            return {
                "accepted": False,
                "reason": "event_id 必填，delta 为 -10～10 的非零整数",
            }
        return await self.store.record(
            platform,
            user_id,
            stream_id,
            event_id,
            "score",
            delta,
            self.config.affection.initial_score,
            self.config.affection.score_cooldown_seconds,
            reason,
        )


def create_plugin() -> AffectionPlugin:
    return AffectionPlugin()
