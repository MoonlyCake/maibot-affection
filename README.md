# MaiBot 好感度插件

开发者：**GPT-6.1 Sol Ultra**。发布与维护：[MoonlyCake](https://github.com/MoonlyCake)。

记录麦麦对每位用户的好感度，接管自然语言查询，并按麦麦现有人设生成简短、拟人的回复。好感度按 `platform + user_id` 保存，同一平台的群聊与私聊共享记录；范围为 **-100～100**，初始值为 **0**。

目标版本：**MaiBot 1.3.3、maibot-plugin-sdk 2.9.0、Python 3.12+**。SDK 最低版本为 **2.8.1**，使用其 `task_name` 模型任务路由。

## 安装

1. 下载 Release 安装 ZIP，将其中的 `affection/` 复制到 MaiBot 的 `plugins/`；如果下载 GitHub 源码 ZIP，将解压后的仓库根目录重命名为 `affection` 再复制。最终得到 `plugins/affection/plugin.py` 和 `plugins/affection/_manifest.json`。
2. 启动或重载 MaiBot，在 WebUI 插件管理中启用 **麦麦好感度**。首次加载由 Runner 根据配置模型生成 `config.toml`，可在 WebUI 调整配置。
3. 确认主程序已配置可用的 `replyer` 模型，以及麦麦昵称、人设和说话风格。

也可直接克隆仓库到 MaiBot 的插件目录：

```bash
git clone https://github.com/MoonlyCake/maibot-affection.git plugins/affection
```

插件使用标准库 SQLite，无需额外数据库服务或模型配置。

## 使用与记分

私聊可直接说：

- `我的好感度多少？`
- `你喜欢我吗？`
- `你对我印象怎么样？`
- `我在你心里算什么？`
- `咱俩现在算啥关系？`

查询分成两套，读取同一份好感度：

| 模式 | 示例 | 回复方式 |
|---|---|---|
| 日常查询 | `你喜欢我吗？`、`麦麦，我在你心里算什么？`、`我的好感度多少？` | 按人设随意、拟人地聊天；问分数时自然提到分数 |
| 正式查询 | `@麦麦 查询好感度`、`@麦麦 查看我的好感度`、`/affection` | 保留人设，以严肃、认真、清晰的语气说明分数、范围和关系阶段 |

群聊默认需 **@ 麦麦**，或以麦麦昵称、别名开头。`/affection` 是正式查询的快捷命令，无需再 @。私聊可直接发送 `查询好感度`。两套使用不同的原生 Command，正则互不重叠。

查询由轻量 Hook 提取当前正文，再通过原生 `Command` 的完整句式正则接管，随后停止普通聊天流程，避免重复回复。询问分数时提示模型自然提到真实分数；询问关系时用日常感受表达。@ 位置、引用回复中的当前提问，以及 Host 已完成的语音转写都可处理；被引用、转发的历史内容以及 `他说“你喜欢我吗？”`、`如何提高好感度？`、`把我的好感度改成100` 等句子不触发查询。识别范围是代码中明确列出的中文句式，新增表达可扩展 `SCORE_PHRASES`、`RELATION_PHRASES`。

自动记分采用明确的完整短句：`谢谢你`、`辛苦了`、`你真棒`、`我喜欢你` 等默认 **+1**；明确辱骂、`滚开`、`闭嘴` 等默认 **-2**。群聊记分同样要求指向麦麦。其他消息不改变分数；查询本身也不加分。正向与负向分别有 **300 秒**冷却，冷却时间和分数在重启后保留。

关系阶段：`-100～-41 排斥`、`-40～-11 疏远`、`-10～19 初识`、`20～49 熟悉`、`50～79 亲近`、`80～100 信任`。

两套查询分别对同一用户设置 **10 秒**冷却，日常查询后可以立即发起正式查询；同一套冷却内重复查询会静默接管。同时生成的查询回复达到上限时，也静默接管。模型或发送失败写入日志，本次查询仍已接管。

## 性能与 Token

普通消息识别、自动记分和数据库操作都在本地完成。获准回复的查询只发起一次原生 `replyer` 调用，默认输出预算 **160 Token**，提示回复为 1～2 句、80 字以内。提示仅包含缓存的人设、说话风格和当前关系信息。全局 bot 配置热更新时刷新昵称与人设缓存，插件配置热更新时刷新相关设置。

数据库位于 `ctx.paths.data_dir / "affection.sqlite3"`，通常映射到 `data/plugins/com.local.mai-affection/`。使用独立 SQLite 的原因是：分数按身份精确查询，传统数据库可以直接完成；原生 `ctx.db` 仅开放 Host 已注册的数据模型，自定义好感表适合保存在插件专属目录；embedding 和向量检索会增加模型调用与检索开销，此功能无需相似性检索。SQL 在单个工作线程执行，使用 WAL；事件去重、冷却检查和分数更新在同一事务内完成，事务中不等待模型。

`events` 保存已处理事件的去重记录，包括冷却内拒绝的事件，持续保留以阻止旧事件重放；分数查询与事件写入均使用主键。它只记录命中查询或明确记分句式的消息，不保存完整聊天记录。

## 消息合约与接管

本插件依赖 MaiBot 1.3.3 的 `chat.receive.after_process` 合约：该 Hook 明确允许 `modified_kwargs` 改写消息，Host 随后反序列化消息并按 `processed_plain_text` 路由原生 Command。仅命中查询时，将这一个字段设为规范化查询正文；`raw_message`、发送者与路由信息继续使用原消息。后续 Hook、会话注册和命令链看到规范化正文，这是让两套 Command 匹配当前提问的有意行为。

Hook 的 Host 序列化器明确构造 `raw_message`、`message_info.user_info.user_id`、消息段 `type/data`；文本和语音 `data` 均为字符串。插件按此合约直接读取必填字段，复用 Host 的 Hook／Command 异常隔离。成功的原生 ASR 产生 `[语音: ...]` 包装，仅完整包装会被剥除；其他预填语音文本按原文处理，关闭或失败的 ASR 占位不匹配查询。

`sync_to_maisaka_history=True` 是 `send.text` 的参数，由 Host 内部完成历史同步，所需能力仍为 `send.text`。相关固定源码：

- [HookSpec 允许改写](https://github.com/Mai-with-u/MaiBot/blob/d64a5d1ba367f316161a7dae84511d9769e77b71/src/chat/message_receive/bot.py#L88)、[消息序列化与恢复](https://github.com/Mai-with-u/MaiBot/blob/d64a5d1ba367f316161a7dae84511d9769e77b71/src/plugin_runtime/host/message_utils.py)。
- [原生 ASR 格式](https://github.com/Mai-with-u/MaiBot/blob/d64a5d1ba367f316161a7dae84511d9769e77b71/src/chat/message_receive/message.py#L419)、[发送与内部历史同步](https://github.com/Mai-with-u/MaiBot/blob/d64a5d1ba367f316161a7dae84511d9769e77b71/src/plugin_runtime/capabilities/core.py#L322)。

## 配置默认值

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `plugin.enabled` | `true` | 插件开关 |
| `plugin.config_version` | `"1.0.0"` | 配置格式标识；当前格式未变更 |
| `affection.initial_score` | `0` | 新用户初始值，-100～100 |
| `affection.automatic` | `true` | 启用本地自动记分 |
| `affection.positive_step` | `1` | 正向加分，1～10 |
| `affection.negative_step` | `2` | 负向扣分，1～10 |
| `affection.score_cooldown_seconds` | `300` | 每用户、每方向的记分间隔 |
| `affection.group_requires_address` | `true` | 群聊需 @ 或昵称开头 |
| `affection.extra_names` | `[]` | 主程序昵称与别名之外的称呼 |
| `reply.cooldown_seconds` | `10` | 每用户、每套查询各自的间隔 |
| `reply.max_inflight` | `4` | 同时生成回复的上限，1～32 |
| `reply.max_tokens` | `160` | 输出 Token 上限，32～2048 |
| `reply.timeout_seconds` | `30` | 生成与发送任务的超时，1～120 秒 |
| `reply.persona_max_chars` | `1200` | 缓存人设与风格的总字符预算，100～8000 |

调整 `initial_score` 只影响尚无记录的用户。关闭 `automatic` 后，其他插件仍可通过公开 API 记分。

## 其他插件调用

公开的是插件间 `API`，不会向 LLM 注册 Tool。调用名称使用清单 ID 命名空间，SDK 自动解包 Host 返回值：

这两个公开 API 面向已安装插件，不设置调用方或用户级授权。任何已安装插件都可读取指定用户的分数，并在启用时按冷却、事件去重和单次变化限制调整分数；持续调用可改变关系阶段，因此调用方属于可信插件。关闭 `plugin.enabled` 会停止调整，读取 API 仍可查询已有分数。

```python
state = await self.ctx.api.call(
    "com.local.mai-affection.get_affection",
    platform="qq",
    user_id="123456",
)
# {"score": 0, "relationship": "初识"}

result = await self.ctx.api.call(
    "com.local.mai-affection.adjust_affection",
    platform="qq",
    user_id="123456",
    stream_id="当前聊天流ID",
    event_id="来源插件:真实事件ID",
    delta=1,
    reason="完成了一次协助",
)
# {"accepted": True, "score": 1}
```

`get_affection(platform, user_id)` 返回 `score`、`relationship`，未建档用户返回初始值。`adjust_affection` 的 `event_id` 必填，`delta` 为 -10～10 的非零整数，`reason` 可选。更新遵循同方向记分冷却与分数上下限；相同 `platform + stream_id + event_id` 的记分事件只处理一次。重复事件或冷却内调用返回 `accepted=False` 和当前 `score`；禁用或参数错误时返回 `accepted=False` 和 `reason`。

## 验证

在插件目录 `plugins/affection/` 内、Python 3.12+ 且已安装 SDK 的环境运行：

```bash
python -m unittest discover -s tests -v
```

31 项本地测试已通过，覆盖句式边界、群聊称呼、记分、两套查询的独立冷却、事件去重、持久化、并发限制、语音包装边界及组件调用行为。测试使用真实 SDK，只模拟 Host RPC 边界；同时已用官方 Host 的 ManifestValidator、PluginLoader 和 ComponentRegistry 核验清单、加载及 5 个组件的注册。部署后依次检查：

1. WebUI 中插件、两套查询 Command 与记分 Hook 正常加载，日志出现加载完成提示。
2. 私聊发送 `我的好感度多少？`，确认只有一条符合现有人设的短回复；10 秒内重问，确认静默。
3. 群聊分别发送无称呼及 @ 麦麦的查询，确认默认仅后者接管；随后发送 `@麦麦 查询好感度`，确认正式回复认真说明分数和关系。被引用、转发的历史正文和第三人称句子保留普通聊天流程。
4. 发送 `谢谢你` 后查询，确认增加 1；冷却内重复感谢不继续加分。重启后确认分数保留。
5. 修改主程序人设并保存，冷却结束后再问关系，检查回复使用更新后的设定。

已按下列源码版本核对开发接口，并进行本地测试。真实 MaiBot 进程、平台适配器和在线 LLM 的完整联调尚未执行；上述部署检查用于完成该验证。

## 开发依据

- [官方插件开发规范](https://docs.mai-mai.org/plugin/)、[API 参考](https://docs.mai-mai.org/plugin/api-reference)、[Hook 文档](https://docs.mai-mai.org/plugin/hooks)。
- [SDK 2.9.0 固定源码 e79989d](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/e79989deb26414869ed47e16d9f695a8b891116d)：能力代理、配置与模型任务路由。
- [MaiBot 1.3.3 固定源码 d64a5d1](https://github.com/Mai-with-u/MaiBot/tree/d64a5d1ba367f316161a7dae84511d9769e77b71)：消息 Hook、Command 拦截与配置热更新。

MIT License，见 `LICENSE`。
