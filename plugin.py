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
SPACE_PUNCT_RE = re.compile(r"[\s，,：:。！？?!~～]")
TURN_WORDS = {"但是", "不过", "只是", "而是", "可是", "然而", "但"}
CLAUSE_RE = re.compile(r"(但是|不过|只是|而是|可是|然而|但|\n|[^\S\n]+|[，,。.!！?？；;：:]+)")
UNCERTAIN_RE = re.compile(r'["“”「」『』]|可真|真行|真有你的|才怪|开玩笑|[他她]说|有人说')
MEME_RE = re.compile(r"无语|(?<!衣)服了|离谱|麻了|急了|栓[qQ]|对对对|呵呵|绝了|孝死|笑死|依托答辩|一坨答辩")
NEGATION_RE = re.compile(r"不|没|(?<!特)别|未(?:曾|能|必)|莫(?:要|再)")
REFERENCE_RE = re.compile(r"(?:刚才|刚刚|上次|上一条|刚说).{0,32}(?:那句|那条|回复|回答|说的话)")
OTHER_ADDRESS_RE = re.compile(r"(?:小|老|阿)[\u4e00-\u9fff]{1,3}|[他她它]")
TEXT_RE = re.compile(r"[^\W\d_]", re.UNICODE)
GREETING_RE = re.compile(r"你好|早安|早上好|晚上好|晚安")
DEPENDENT_RE = re.compile(r"吗|么|嘛|呢|啊|呀|吧|哇")
TAIL_RE = re.compile(r"(?:死了|[了啊呀呢哦啦吧哇~～]|[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f])+$")
DEGREES = r"(?:越来越|真的|非常|特别|超级|极其|有点|稍微|真|很|挺|好|太|最)"
MEME_CLAUSE_RE = re.compile(rf"(?:你)?(?:{DEGREES}){{0,2}}(?:绷|典|6+|行吧|算了|好{{2,}})(?:了|啊|呀|呢)?")
DEGREE_GROUP = rf"(?P<degree>(?:{DEGREES}){{0,2}})"
STRONG_RE = re.compile(r"非常|特别|超级|极其|最")
WEAK_RE = re.compile(r"有点|稍微")
AFFECTION_TOPIC_RE = re.compile(r"好感|亲密度")
# 词根提供档位；模板确认语法和对象。完整聊天样例不进入词表。
EVALUATIONS = {
    **dict.fromkeys(("棒", "好", "可爱", "温柔", "聪明", "厉害", "贴心", "靠谱", "优秀", "说得对"), (1, "夸奖")),
    **dict.fromkeys(("烦", "敷衍", "下头", "失望", "蠢", "傻", "笨"), (-1, "不满")),
    **dict.fromkeys(("讨厌", "恶心"), (-2, "反感")),
    **dict.fromkeys(("垃圾", "废物", "傻逼", "蠢货"), (-3, "辱骂")),
}
ATTITUDES = {
    **dict.fromkeys(("喜欢", "爱", "信任", "想念", "想"), (2, "喜欢")),
    **dict.fromkeys(("讨厌", "不喜欢"), (-2, "反感")),
    "恨": (-3, "辱骂"),
}
THANKS = dict.fromkeys(("谢谢", "多谢", "感谢", "辛苦", "谢", "thx"), (1, "感谢"))
HELP = dict.fromkeys(("帮", "帮助", "支持"), (2, "感谢"))
FEELINGS = dict.fromkeys(("不舒服", "难过", "失望", "生气"), (-1, "不满"))
IMPERATIVES = {"滚": (-2, "反感"), "滚开": (-2, "反感"), "闭嘴": (-2, "反感"), "别烦我": (-1, "不满")}
EMOTION_WORD_RE = re.compile(
    "|".join(
        sorted(
            {word for word in (*EVALUATIONS, *ATTITUDES, *THANKS, *IMPERATIVES) if len(word) > 1}
            | {
                "开心",
                "难过",
                "生气",
                "抱怨",
                "失望",
                "不舒服",
                "胡说",
                "答错",
                "插嘴",
                "揍",
                "打死",
                "弄死",
                "人机",
                "yyds",
                "天才",
                "算错",
                "大忙",
                "帮助",
                "傻瓜",
                "笨蛋",
                "滚蛋",
                "难受",
                "抽象",
                "唠叨",
                "啰嗦",
                "啰唆",
                "话多",
                "废话",
                "难听",
                "不好听",
                "走开",
                "闪开",
                "搞砸",
                "弄砸",
                "搞错",
                "弄错",
                "失误",
                "失败",
                "翻车",
                "说了算",
                "最有道理",
            },
            key=len,
            reverse=True,
        )
    )
)
SINGLE_EMOTION_RE = re.compile(
    r"(?<!可)爱(?!好|尔兰|迪生|因斯坦)|(?<!积)累(?!加|计|积)"
    r"|(?<![打翻])滚(?!动|筒|轮|烫|珠)|恨|傻|笨|蠢|(?<!麻)烦|麻烦(?!你|您|帮|问|请)"
    r"|(?<![棒球])棒(?!球|棒糖)"
    rf"|你(?:这样|那样|(?:刚才|刚刚|上次).{{0,12}})(?:{DEGREES}){{0,2}}好(?:的)?$"
)
FAILURE_RE = re.compile(r"(?:又|再次|还是).{0,16}错(?!过|觉|落|位)")
RESULT_RE = re.compile(
    r"[这那](?:(?:道|个|份|条)?(?:题|答案|回答|回复|结果|代码|计算|输出|方案)|次(?=又|再次|还是|失误))"
)
DISMISSIVE_RE = re.compile(r"随(?:你|您)(?:便|意)|(?:走|滚|躲)远(?:点|些)|一边去")
EVENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
OTHER_EVALUATION_RE = re.compile(
    rf"(?:[他她它]|(?:你|我|他|她)的[\w]{{1,12}}|[这那][\w]{{1,12}})(?:{DEGREES}){{0,2}}"
    r"(?:傻|笨|蠢|烦|累|棒)(?:吗|么|嘛)?"
)


class PluginSettings(PluginConfigBase):
    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="启用好感度插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class AffectionSettings(PluginConfigBase):
    __ui_label__ = "好感度"

    initial_score: int = Field(default=0, ge=-100, le=100, description="新用户初始值；0 表示初识")
    automatic: bool = Field(default=True, description="按指向麦麦的日常交流和明确情绪记录好感变化")
    interaction_step: int = Field(default=1, ge=0, le=10, description="明确中性交流加分，最终最多 3；0 关闭基础分")
    positive_step: int = Field(default=1, ge=1, le=10, description="正向最低档基数；更强档加 1/2，最终最多 3")
    negative_step: int = Field(default=2, ge=1, le=10, description="负向中档基数；轻/强档减/加 1，最终最多 3")
    score_cooldown_seconds: int = Field(default=300, ge=0, description="同一用户同方向记分间隔，重启后保留")
    group_requires_address: bool = Field(default=True, description="群聊查询需 @ 或昵称开头；评分始终确认对象")
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
        names_pattern = "|".join(map(re.escape, self.names)) or r"(?!)"
        self.name_re = re.compile(r"@?(?:" + names_pattern + ")")
        target = rf"(?:你|@?(?:{names_pattern}))"
        self.vocative_re = re.compile(
            rf"^@?(?:{names_pattern})(?=$|[\s，,。.!！?？；;：:]|你|我(?!们)|请(?:你|问|帮我)|帮我|麻烦|早安|早上好|晚安|晚上好|在吗|谢谢|多谢|感谢|辛苦|谢|thx)"
        )
        self.fragment_re = re.compile(rf"(?:(?:@?(?:{names_pattern}))?你|@?(?:{names_pattern}))?(?:{DEGREES}){{0,2}}")
        self.other_re = re.compile(
            rf"(?:[他她它这那]|作者|小[^\W\d_]).+|我(?:{DEGREES}){{0,2}}"
            rf"(?:喜欢|爱|讨厌|不喜欢|恨)(?!{target}).+"
        )
        self.insult_re = re.compile(
            rf"{target}(?:真是|就是|是|像)(?:一)?(?:个|条|只|头)?(?:狗|猪(?:头)?|驴|畜生|牲口)(?:东西)?"
            r"(?=$|[吗么嘛吧啊呀呢啦了]|一样|似的|还|又|就|也)"
        )
        thanks_target = rf"(?:你(?:[了啊呀啦呢哦]{{0,2}}@?(?:{names_pattern}))?|@?(?:{names_pattern}))"
        self.emotion_rules = tuple(
            (re.compile(pattern), lexicon)
            for pattern, lexicon in (
                (
                    rf"(?P<target>{target})让我{DEGREE_GROUP}(?P<word>{'|'.join(FEELINGS)})",
                    FEELINGS,
                ),
                (
                    rf"(?P<target>{target}){DEGREE_GROUP}(?P<word>{'|'.join(HELP)})"
                    r"了我(?:很多|不少|大忙)",
                    HELP,
                ),
                (
                    rf"(?P<target>{target})(?:真是|就是|是)?(?:个)?(?:也(?=太))?{DEGREE_GROUP}"
                    rf"(?P<word>{'|'.join(sorted(EVALUATIONS, key=len, reverse=True))})",
                    EVALUATIONS,
                ),
                (
                    rf"(?:我(?:是|还是|一直都)?)?{DEGREE_GROUP}"
                    rf"(?P<word>{'|'.join(sorted(ATTITUDES, key=len, reverse=True))})(?P<target>{target})",
                    ATTITUDES,
                ),
                (
                    rf"{DEGREE_GROUP}(?P<word>{'|'.join(sorted(THANKS, key=len, reverse=True))})"
                    rf"[了啊呀啦呢哦]{{0,2}}(?P<target>{thanks_target})?",
                    THANKS,
                ),
                (
                    rf"(?P<degree>)(?P<word>{'|'.join(sorted(IMPERATIVES, key=len, reverse=True))})"
                    rf"(?P<target>{target})?",
                    IMPERATIVES,
                ),
            )
        )

    def body(self, text: str) -> tuple[str, bool, str]:
        text = text.strip()
        punctuated = text
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
        return text.rstrip("呀呢啊哦啦"), named, punctuated

    def query(self, body: str) -> str | None:
        if FORMAL_RE.fullmatch(body):
            return "formal"
        if SCORE_RE.fullmatch(body):
            return "score"
        if RELATION_RE.fullmatch(body):
            return "relationship"
        return None

    def _clauses(self, text: str) -> list[str]:
        clauses: list[str] = []
        spaced = adjacent = False
        for part in CLAUSE_RE.split(text):
            if not part:
                continue
            if part != "\n" and part.isspace():
                spaced = adjacent
                continue
            if part in TURN_WORDS or part == "\n":
                clauses.append(part)
            elif not TEXT_RE.search(part) and not part.isdigit():
                # 标点和换行是硬边界；孤立称呼只跨空格合并。
                spaced = adjacent = False
                continue
            elif (
                spaced
                and clauses
                and clauses[-1] not in TURN_WORDS
                and clauses[-1] != "\n"
                and (
                    self.fragment_re.fullmatch(clauses[-1])
                    or self.fragment_re.fullmatch(part)
                    or DEPENDENT_RE.fullmatch(part)
                    or part.startswith("的")
                )
            ):
                clauses[-1] += part
            else:
                clauses.append(part)
            spaced = False
            adjacent = part not in TURN_WORDS and part != "\n"
        return clauses

    def score(
        self,
        text: str,
        *,
        private: bool,
        directed: bool,
        other_target: bool = False,
        positive: int = 1,
        negative: int = 2,
        interaction: int = 1,
    ) -> tuple[int, str]:
        body, _, _ = self.body(text)
        if not body or body.startswith("/") or self.query(body):
            return 0, "查询或命令"
        if UNCERTAIN_RE.search(text) or MEME_RE.search(text) or AFFECTION_TOPIC_RE.search(text):
            return 0, "不确定"
        called = bool(self.vocative_re.match(text.strip()))
        recipient = (private or directed or called) and not other_target
        implicit = recipient and (private or len(body) <= 40)
        scores: dict[str, tuple[int, str]] = {}
        unknown = blocked = turning = named_scope = False
        for part in self._clauses(text):
            if part == "\n":
                if other_target:
                    named_scope = False
                turning = False
                continue
            if part in TURN_WORDS:
                turning = True
                continue
            if (
                OTHER_ADDRESS_RE.fullmatch(part)
                and not self.name_re.fullmatch(part)
                and not EMOTION_WORD_RE.search(part)
                and not SINGLE_EMOTION_RE.search(part)
            ):
                implicit = named_scope = recipient = False
                unknown = True
                continue
            clause = TAIL_RE.sub("", part)
            vocative = self.vocative_re.match(clause)
            if vocative:
                named_scope = True
                clause = clause[vocative.end() :]
            if not clause:
                continue
            if MEME_CLAUSE_RE.fullmatch(part) or MEME_CLAUSE_RE.fullmatch(clause):
                return 0, "不确定"
            if GREETING_RE.fullmatch(clause):
                turning = False
                continue
            match = next(
                ((m, lexicon) for pattern, lexicon in self.emotion_rules if (m := pattern.fullmatch(clause))), None
            )
            signal = bool(
                match
                or EMOTION_WORD_RE.search(clause)
                or SINGLE_EMOTION_RE.search(clause)
                or NEGATION_RE.search(clause)
                or REFERENCE_RE.search(clause)
                or OTHER_EVALUATION_RE.fullmatch(clause)
                or FAILURE_RE.search(clause)
                or DISMISSIVE_RE.search(clause)
                or self.insult_re.search(clause)
            )
            blocked |= signal
            if (
                self.other_re.fullmatch(clause)
                and "你" not in clause
                and not self.name_re.search(clause)
                and not RESULT_RE.match(clause)
            ):
                turning = False
                continue
            if match is None:
                unknown |= signal or bool(self.fragment_re.fullmatch(clause))
                turning = False
                continue
            parsed, lexicon = match
            target = parsed["target"]
            if not (named_scope or implicit or (target and target != "你")):
                unknown = True
                turning = False
                continue
            word, degree = parsed["word"], parsed["degree"]
            strength, label = lexicon[word]
            if REFERENCE_RE.search(clause) or (
                NEGATION_RE.search(clause) and word not in ("不喜欢", "别烦我", "不舒服")
            ):
                unknown = True
                turning = False
                continue
            weak = bool(WEAK_RE.search(degree))
            strong = bool(STRONG_RE.search(degree)) or (word == "爱" and "真的" in degree) or "死了" in part
            if weak and strong:
                unknown = True
                turning = False
                continue
            magnitude = 1 if weak else min(3, abs(strength) + strong)
            if word in ("蠢", "傻", "笨"):
                magnitude = min(2, magnitude)
            delta = min(3, positive + magnitude - 1) if strength > 0 else -min(3, max(1, negative + magnitude - 2))
            if turning:
                scores.clear()
            scores[clause] = (delta, label)
            turning = False
        if unknown:
            return 0, "不确定"
        if scores:
            total = sum(delta for delta, _ in scores.values())
            labels = sorted({label for _, label in scores.values()})
            return max(-3, min(3, total)), "/".join(labels)
        return (
            (min(3, interaction), "基础交流") if recipient and not blocked and TEXT_RE.search(body) else (0, "对象不明")
        )


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
                last_interaction REAL NOT NULL DEFAULT 0,
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
            CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp);
        """)
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM events WHERE timestamp < ?", (now - EVENT_RETENTION_SECONDS,))
            if "last_interaction" not in {row["name"] for row in self.db.execute("PRAGMA table_info(affection)")}:
                self.db.execute("ALTER TABLE affection ADD COLUMN last_interaction REAL NOT NULL DEFAULT 0")
                self.db.execute("UPDATE events SET reason='历史事件'")
        self.next_cleanup = now + 3600

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
            if now >= self.next_cleanup:
                self.db.execute("DELETE FROM events WHERE timestamp < ?", (now - EVENT_RETENTION_SECONDS,))
                self.next_cleanup = now + 3600
            row = self._ensure_user(platform, user_id, initial)
            if kind == "formal_query":
                clock = "last_formal_query"
            elif kind == "query":
                clock = "last_query"
            elif reason == "基础交流":
                clock = "last_interaction"
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
                    reason,
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
            if segment["type"] == "voice":
                if text in ("[语音消息]", "[语音消息，转录失败]"):
                    continue
                if text.startswith("[语音: ") and text.endswith("]"):
                    text = text[5:-1].strip()
            parts.append(text)
        return "\n".join(parts)

    def _account(self, message: dict[str, Any]) -> str:
        route = message["message_info"].get("additional_config") or {}
        return next(
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

    def _addressed(self, message: dict[str, Any], named: bool) -> bool:
        if not message["message_info"].get("group_info") or named:
            return True
        account = self._account(message)
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
        description="对指向麦麦的普通交流与明确情绪本地记分",
    )
    async def record_interaction(self, message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        settings = self.config.affection
        if not self.config.plugin.enabled or message.get("is_notify"):
            return {"action": "continue"}
        text = self._message_text(message)
        body, named, punctuated = self.matcher.body(text)
        addressed = self._addressed(message, named)
        if self.matcher.query(body):
            if settings.group_requires_address and not addressed and body != "/affection":
                return {"action": "continue"}
            # 排除 Host 的引用描述、@ 展示位置和 ASR 包装，保留用户当前这句话。
            message["processed_plain_text"] = body
            return {"action": "continue", "modified_kwargs": {"message": message}}
        if not settings.automatic or not body or body.startswith("/"):
            return {"action": "continue"}
        account = self._account(message)
        other_target = any(
            (segment["type"] == "at" and str(segment["data"]["target_user_id"]) != account)
            or (segment["type"] == "reply" and str(segment["data"].get("target_message_sender_id")) != account)
            for segment in message["raw_message"]
        )
        delta, label = self.matcher.score(
            punctuated,
            private=not message["message_info"].get("group_info"),
            directed=self._addressed(message, False),
            other_target=other_target,
            positive=settings.positive_step,
            negative=settings.negative_step,
            interaction=settings.interaction_step,
        )
        if delta:
            result = await self.store.record(
                message["platform"],
                str(message["message_info"]["user_info"]["user_id"]),
                message["session_id"],
                str(message["message_id"]),
                "score",
                delta,
                settings.initial_score,
                settings.score_cooldown_seconds,
                label,
            )
            if result["accepted"]:
                self.ctx.logger.info(
                    "好感度已记分：platform=%s, user_id=%s, score=%s, delta=%s, label=%s",
                    message["platform"],
                    message["message_info"]["user_info"]["user_id"],
                    result["score"],
                    delta,
                    label,
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
        body, named, _ = self.matcher.body(text)
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
                    "正式查询" if kind == "formal" else "日常查询",
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
            "插件上报",
        )


def create_plugin() -> AffectionPlugin:
    return AffectionPlugin()
