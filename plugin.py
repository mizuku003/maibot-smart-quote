"""智能引用（smart-quote）：接管 Maisaka 的引用回复决策。

背景
----
Maisaka 内置的 ``reply`` 工具带一个 ``set_quote`` 参数，默认值为 ``True``
（见 ``src/maisaka/builtin_tool/reply.py``）。真正是否引用由两个条件相与决定::

    set_quote = bool(invocation_arguments.get("set_quote", True))   # 模型传的，默认 True
    enable_reply_quote = bool(config.global_config.chat.reply_style.enable_reply_quote)
    effective_set_quote = set_quote and enable_reply_quote

也就是说：**模型默认就会引用**，只有它主动传 ``set_quote=False`` 才不引用。全局开关
``enable_reply_quote`` 是一个"总闸"，关掉就一律不引用，无法按条件细分。

本插件要做的是：在这个决策链上插一层"策略层"，**由插件按配置决定引用与否**，
不再依赖模型给出的 ``set_quote``。

接管方式
--------
``set_quote`` 既然由本插件全权决定，就**不再下发给模型**（见
``_SCHEMA_HIDDEN_KEYS``），改由 ``after_response`` 无条件写入 —— 插件成为
唯一决策源。这样模型不会为一个永不采纳的参数白耗 token，也不存在
"模型省略参数 → ``bool(None)`` 误判"这类歧义分支。

需要留意麦麦里还有两处引用来源**不受本插件控制**：

1. ``chat.reply_style.enable_reply_quote`` 是总闸（``effective_set_quote =
   set_quote and enable_reply_quote``）。它**必须保持 ``true``**，否则本插件
   写入的 ``set_quote=True`` 会被抹掉，插件将彻底无法引用；
2. ``chinese_typo.enable_correction_quote`` —— 机器人打错字后补发纠正消息时，
   麦麦会自动让纠正那条引用上一条（``quote_previous``）。这一步发生在
   ``reply`` 工具内部的发送流水线里，插件阶段拿不到，无法干预。

实现原理
--------
插件通过两个 Maisaka 规划器 Hook 协作完成，全部是麦麦公开的扩展点：

1. ``maisaka.planner.before_request``（只读）
   模型请求发出前，载荷里有 ``items`` —— 即本次发给模型的全部 Context Items。
   其中的 ``UserMessageItem`` 的 ``parts[0].text`` 形如::

       <message msg_id="397286626" time="17:20:50" user="穗穗" ...>
       正在搜索：可爱女人

   我们从中按顺序解析出每个 ``msg_id``，形成一个"消息顺序表"，缓存到内存；
   同时把每条消息的**正文**也按 ``msg_id`` 存下来，供关键词黑白名单匹配。
   另外记录 ``session_id`` 与本次历史消息总数，供下一步计算间隔。

2. ``maisaka.planner.after_response``（可写）
   模型响应返回后、工具真正执行前触发。载荷里的 ``output_items`` 包含模型
   产出的 ``FunctionCallItem``，结构为::

       {
         "item_type": "FunctionCallItem",
         "meta": {...},
         "tool_call": {
           "call_id": "call_xxx",
           "func_name": "reply",
           "args": {"msg_id": "397286626", "set_quote": true, ...},
           "extra_content": {...}
         }
       }

   这里直接改写 ``tool_call.args["set_quote"]``。麦麦会把 Hook 返回的
   ``output_items`` 交给 ``deserialize_prompt_items()`` 反序列化并采纳
   （见 ``src/maisaka/chat_loop_service.py`` 中 ``after_response`` 的处理），
   因此改写后的参数会真正生效。

判定规则
--------
对每个 ``func_name == "reply"`` 的调用，按下面的顺序判定，**得到第一个明确
结论即停止**（优先级从高到低）：

1. 关键词黑白名单（``[keywords]``）→ 拿**被回复的那条消息**的正文去匹配：
   命中不引用关键词 → 一律不引用；命中引用关键词 → 一律引用。
   **黑名单优先**（与「排除名单优先于生效名单」一致）；
2. 该会话类型未启用会话控制 → 交给随机判定；
3. 排除名单 → 一律不引用；生效名单非空且不在其中 → 交给随机判定；
4. 间隔条件：用 ``args["msg_id"]`` 在 before_request 缓存的消息顺序表里定位索引，
   与最新消息索引求差，得到"隔了多少条消息"（即"回复的是较早的消息"）：
   - 低于硬阈值 ``no_quote_below_gap`` → 不引用，**不参与随机**；
   - 未达到软阈值 ``min_message_gap`` → 交给随机判定；
   - 达到 ``min_message_gap`` → 一律引用；
5. 以上均无结论 → 按该会话类型自带的随机权重（引用 / 不引用 / @）决定；
   本节的「启用随机判定」关闭时，改用该会话类型的 ``default_quote``。

两个间隔阈值构成三段式（以 硬=3 / 软=5 为例）：
间隔 < 3 → 必不引用；3 ≤ 间隔 < 5 → 随机；间隔 ≥ 5 → 必引用。

会话类型与对端 ID 判定
----------------------
麦麦的 session_id 是 ``platform + 路由 + 群号/用户号`` 拼接后的 **MD5 摘要**，
字符串里既没有 ``group`` 之类的字面量，也**不含原始群号/用户号**。
因此既不能按前缀识别会话类型，也不能从里面解析出对端 ID。

两者都改为**反查麦麦会话表**取权威值（``chat_manager``）：

- 会话类型 → ``chat_stream.is_group_session``
- 对端 ID → 群聊取 ``group_id``，私聊取 ``user_id``

载荷里若有显式字段（``is_group_chat`` / ``group_id`` / ``user_id``）则优先采用。
仅当反查失败时，才退回 ``session_id`` 命名规则兜底。

命令
----
``/引用状态`` 查看当前会话的策略与判定结果；
``/引用关键词`` 打印关键词黑白名单与群聊 / 私聊策略概览。

设计取舍
--------
- **纯扩展、零侵入**：不 patch 麦麦、不改配置文件；执行完全依赖 Hook 载荷。
- **失败安全**：任何一步解析失败都直接 ``return None``（不改写），保持麦麦原行为，
  绝不因为插件出错而打断正常回复。
- **状态仅存内存**：消息顺序表按 session_id 缓存最近一次的值，进程重启即清空；
  不影响任何持久化数据。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, ClassVar, Literal

from pydantic import field_validator

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# 插件配置 schema 版本（与插件版本无关：只有配置字段增删时才需要动它）。
# 注意：宿主会拿它与磁盘上 config.toml 的 plugin.config_version 比对，
# 一旦这里的值变大就会触发一次配置迁移/重写，因此**不要**跟着 _manifest.json
# 的插件版本号一起改。
SUPPORTED_CONFIG_VERSION = "1.0.0"

# 从 <message msg_id="397286626" ...> 标签里抠出 msg_id
_MESSAGE_ID_RE = re.compile(r'<message\s+[^>]*?msg_id\s*=\s*"([^"]+)"')
# 兜底：单引号形态
_MESSAGE_ID_RE_SQ = re.compile(r"<message\s+[^>]*?msg_id\s*=\s*'([^']+)'")

# 一次抠出「msg_id + 该条消息的正文」。
# 一条 part 的 text 里可能连着好几个 <message> 块，所以用前瞻切分：
# 正文一直吃到下一个 <message 或字符串结尾为止。
# 正文里若带 </message> 闭合标签，取值时再剥掉。
_MESSAGE_BLOCK_RE = re.compile(
    r"""<message\s[^>]*?msg_id\s*=\s*["']([^"']+)["'][^>]*>(.*?)(?=<message\s|\Z)""",
    re.DOTALL,
)
# 正文里可能残留的闭合标签（麦麦当前不写，但格式一变就会漏出来）
_MESSAGE_CLOSE_RE = re.compile(r"</message\s*>", re.IGNORECASE)

# 引用回复工具名（Maisaka 内置）
_REPLY_TOOL_NAME = "reply"

# 富回复参数里本插件不想要的两项（模型自己塞图 / 表情包）
_STRIPPED_RICH_KEYS = ("attach_pic", "attach_emoji")

# 从下发给模型的 reply schema 里一并移除的「插件独占参数」。
#   * set_quote —— 引用与否由本插件的策略层全权决定，模型填什么都会被改写，
#     留着只会白耗 token、还多出一个"模型省略该参数"的歧义分支；
#   * attach_at —— 需要 @ 时由本插件自己写入目标 msg_id，不该让模型指定 @ 谁。
#
# ⚠ 千万不要把这两项并进 _STRIPPED_RICH_KEYS：那个常量同时驱动
# rewrite_set_quote 末尾的 args.pop()，会把本插件刚写进去的判定值删掉，
# 于是宿主用缺省 True 兜底 —— 结果变成「全部引用」。
_SCHEMA_HIDDEN_KEYS = _STRIPPED_RICH_KEYS + ("set_quote", "attach_at")

# 群聊消息行独有属性：麦麦用 user_cardname（群名片）填充，私聊一律没有。
# 因此"出现过 group_card"可以**确定**是群聊；没出现过则无法下结论。
_GROUP_CARD_RE = re.compile(r"<message\s+[^>]*?group_card\s*=\s*[\"']")

# 会话表缓存有效期（秒）。查会话表是一次跨进程 IPC，不必每个 hook 都问一次。
_SESSION_CACHE_TTL_SECONDS = 60.0

# 麦麦新格式 session_id：平台+路由+目标 拼接后的 MD5 摘要（32 位十六进制）
_MD5_HEX_RE = re.compile(r"[0-9a-f]{32}")


def _is_rich_reply_enabled() -> bool:
    """麦麦是否开启了富回复（``experimental.enable_rich_reply``）。

    ``attach_at`` 属于富回复参数：麦麦在 ``reply.py`` 里会先看这个开关，
    没开就把整个 ``attach_at`` 丢掉。所以插件在写 ``attach_at`` 前必须先问一句，
    否则写了也是白写（还会让日志显示"命中 @ 却没 @ 到"，很迷惑）。

    注意导入路径：``global_config`` 住在 ``src.config.config`` **模块**里，
    而不是 ``src.config`` **包**里（麦麦的 ``reply.py`` 用的就是
    ``from src.config import config as config_module``）。写错一级就会
    一直抛 AttributeError，被兜底吞掉后恒返回 False。

    查询失败时保守返回 False —— 宁可不 @，也不要塞一个必然被丢弃的参数。
    """
    for importer in (
        lambda: __import__("src.config.config", fromlist=["global_config"]).global_config,
        lambda: __import__("src.config", fromlist=["config"]).config.global_config,
    ):
        try:
            return bool(importer().experimental.enable_rich_reply)
        except Exception:  # noqa: BLE001
            continue
    return False


def _safe_log(plugin: Any, level: str, message: str, *args: Any) -> None:
    """尽最大努力写日志，绝不因为日志本身而抛异常。

    SDK 的 ``plugin.ctx`` 是只读属性，且只有在 Runner 环境里才被赋值
    （``maibot_sdk/plugin.py`` 里否则会 ``raise RuntimeError``）。
    本插件的 hook 有可能在 Runner 之外被调用（自检脚本、单元测试），
    此时如果日志写失败，异常会从 ``except`` 分支里逃出去、掀翻整个调用方。
    所以这里把「取 ctx → 取 logger → 记录」三层全部包进 try。
    """
    try:
        logger = getattr(plugin.ctx, "logger", None)
        if logger is None:
            return
        getattr(logger, level)(message, *args)
    except Exception:  # noqa: BLE001
        pass


def _build_hook_result(base_kwargs: dict[str, Any], **modified: Any) -> dict[str, Any]:
    """按麦麦 Hook 协议组装**阻塞型**处理器的返回值。

    这是本插件曾经整体失效的根因，务必保留说明：

    1. Runner 只认 ``{"action": ..., "modified_kwargs": {...}}`` 这一种形态
       （``plugin_runtime/runner/runner_main.py`` 里写死了
       ``raw.get("modified_kwargs")``）。返回裸字典 —— 例如直接
       ``{"output_items": ...}`` —— Runner 取到 ``None``，宿主
       （``plugin_runtime/host/hook_dispatcher.py``）便不会合并，
       改动被静默丢弃，连一条警告日志都不会有。
    2. 宿主是用 ``modified_kwargs`` **整体替换** kwargs，而不是逐键合并。
       所以必须把原始 kwargs 的**全部键**原样带回去，否则同一个 Hook 的
       后续处理器（以及宿主自己的收尾逻辑）会拿不到参数。
       典型后果：``after_response`` 丢了 ``item_schema_version``，
       宿主反序列化直接抛 ValueError，改动同样被丢弃。
    """

    merged = dict(base_kwargs)
    merged.update(modified)
    return {"action": "continue", "modified_kwargs": merged}


# 判定结果的短标签，用于日志与命令输出。
# 用词与配置面板、README 保持一致：排除名单 / 生效名单 / 间隔条件 / 随机判定。
_REASON_DISABLED = "该会话类型未启用会话控制"
_REASON_BLACKLIST = "命中排除名单"
_REASON_TARGET_UNKNOWN = "目标消息不在本次上下文中"
_REASON_GAP_MET = "间隔条件命中"
_REASON_GAP_NOT_MET = "间隔条件未命中"
_REASON_GAP_TOO_CLOSE = "间隔低于硬阈值，不引用"
_REASON_KEYWORD_QUOTE = "命中引用关键词"
_REASON_KEYWORD_NO_QUOTE = "命中不引用关键词"
_REASON_NOT_WHITELISTED = "不在生效名单"
_REASON_GAP_DISABLED = "未启用间隔条件"

# 关键词黑白名单的「生效范围」。刻意用中文取值：
#   * 配置面板会把 Literal 渲染成**下拉框**，用户不用猜 any / group / private 怎么写；
#   * config.toml 里存的是「仅群聊」，手改时也一眼看得懂。
# 旧配置里的 any / group / private 由 KeywordRule 的校验器自动翻译，不会失效。
_SCOPE_ANY = "不限"
_SCOPE_GROUP = "仅群聊"
_SCOPE_PRIVATE = "仅私聊"
_SCOPE_CHOICES = (_SCOPE_ANY, _SCOPE_GROUP, _SCOPE_PRIVATE)
_LEGACY_SCOPE_MAP = {
    "any": _SCOPE_ANY,
    "group": _SCOPE_GROUP,
    "private": _SCOPE_PRIVATE,
}

# 关键词条目命中后的处理方式。同样用中文取值 → 面板下拉框，
# 也避免 ``quote = false`` 那种"关掉引用即不引用"的双重否定。
_ACTION_QUOTE = "引用"
_ACTION_NO_QUOTE = "不引用"
_ACTION_CHOICES = (_ACTION_QUOTE, _ACTION_NO_QUOTE)

# 回复形态（三态）
_FORM_QUOTE = "quote"   # 引用回复
_FORM_PLAIN = "plain"   # 普通回复（不引用、不 @）
_FORM_AT = "at"         # @ 目标消息发送者后回复

_FORM_LABELS = {
    _FORM_QUOTE: "引用",
    _FORM_PLAIN: "不引用",
    _FORM_AT: "@对方",
}


class PluginSectionConfig(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "开关"
    __ui_icon__ = "power"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="关闭后本插件不介入引用决策，恢复麦麦默认行为",
        json_schema_extra={"label": "启用插件", "hint": "开=接管引用，关=麦麦默认行为"},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本，请勿修改",
        json_schema_extra={
            "hidden": True,
            "disabled": True,
            "label": "配置版本",
            "hint": "请勿修改",
        },
    )


class SessionPolicyConfig(PluginConfigBase):
    """某一类会话（群聊 / 私聊）的引用策略。

    自包含：是否启用 → 名单例外 → 间隔条件 → 无结论时按权重随机
    （关掉随机则用「固定引用行为」）。
    """

    # 子类只覆盖 __ui_label__ / __ui_icon__ / __ui_order__ 与默认值。
    # 同一字段的 label / hint / placeholder 必须与基类逐字一致，
    # 否则面板上群聊 / 私聊两节会看起来不一样。

    enabled: bool = Field(
        default=True,
        description="这类会话是否套用本节的策略",
        json_schema_extra={
            "label": "启用会话控制",
            "hint": "开=本节生效；关=跳过本节条件、改用随机",
        },
    )
    default_quote: bool = Field(
        default=False,
        description="不进行随机时，这类会话的固定引用行为",
        json_schema_extra={
            "label": "固定引用行为",
            "hint": "随机判定关闭时生效：开=引用，关=不引用",
            "depends_on": "random_enabled",
            "depends_value": False,
        },
    )
    min_message_gap: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="回复的消息需间隔该条数以上才视为满足间隔条件",
        json_schema_extra={
            "label": "引用间隔阈值",
            "hint": "达到该间隔引用；0=不启用",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    no_quote_below_gap: int = Field(
        default=1,
        ge=0,
        le=1000,
        description="回复的消息间隔低于该条数时一律不引用",
        json_schema_extra={
            "label": "低于该间隔不引用",
            "hint": "低于该间隔不引用；0=不启用",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    whitelist: list[str] = Field(
        default_factory=list,
        description="只对这些群 / 人生效；留空=全部生效",
        json_schema_extra={
            "label": "生效名单",
            "hint": "留空=全部会话",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    blacklist: list[str] = Field(
        default_factory=list,
        description="这些群 / 人一律不引用；优先级高于生效名单",
        json_schema_extra={
            "label": "排除名单",
            "hint": "留空=不排除；优先于生效名单",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    # —— 以下为随机判定：本节自带，不从别处继承 ——
    random_enabled: bool = Field(
        default=True,
        description="无结论时是否按权重随机决定；关闭则改用本节的「固定引用行为」",
        json_schema_extra={
            "label": "启用随机判定",
            "hint": "开=按下面三项权重随机；关=用固定引用行为",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    quote_weight: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="本节随机命中「引用」的相对权重",
        json_schema_extra={
            "label": "引用权重",
            "hint": "相对权重；0=不启用",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )
    no_quote_weight: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="本节随机命中「不引用」的相对权重",
        json_schema_extra={
            "label": "不引用权重",
            "hint": "相对权重；0=不启用",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )
    at_weight: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="本节 @ 对方的概率，与上两项独立叠加",
        json_schema_extra={
            "label": "@ 权重",
            "hint": "独立随机；0=不启用；需开启富回复",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )


class GroupPolicyConfig(SessionPolicyConfig):
    """群聊的引用策略。 默认「引用间隔阈值」= 5：只对 5 条之前的消息进行引用"""

    __ui_label__ = "群聊"
    __ui_icon__ = "users"
    __ui_order__ = 2

    # 群聊默认设置间隔阈值，避免紧邻消息也触发引用。
    # 只覆盖默认值，说明文字必须与基类**逐字一致**，否则面板两节会看起来不一样。
    min_message_gap: int = Field(
        default=5,
        ge=0,
        le=1000,
        description="回复的消息需间隔该条数以上才视为满足间隔条件",
        json_schema_extra={
            "label": "引用间隔阈值",
            "hint": "达到该间隔引用；0=不启用",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )


class PrivatePolicyConfig(SessionPolicyConfig):
    """私聊的引用策略。 私聊一般不需要引用，也不需要 @（at_weight 默认 0）。 若要一律不引用：关掉「启用随机判定」即可。"""

    __ui_label__ = "私聊"
    __ui_icon__ = "user"
    __ui_order__ = 3

    # 以下四项只覆盖默认值，说明文字必须与基类**逐字一致**，否则面板两节会看起来不一样。
    # 私聊默认：不给间隔条件留缝（两个阈值都 0）、关掉随机判定（即一律不引用）、不 @。
    no_quote_below_gap: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="回复的消息间隔低于该条数时一律不引用",
        json_schema_extra={
            "label": "低于该间隔不引用",
            "hint": "低于该间隔不引用；0=不启用",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    random_enabled: bool = Field(
        default=False,
        description="无结论时是否按权重随机决定；关闭则改用本节的「固定引用行为」",
        json_schema_extra={
            "label": "启用随机判定",
            "hint": "开=按下面三项权重随机；关=用固定引用行为",
            "depends_on": "enabled",
            "depends_value": True,
        },
    )
    quote_weight: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="本节随机命中「引用」的相对权重",
        json_schema_extra={
            "label": "引用权重",
            "hint": "相对权重；0=不启用",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )
    no_quote_weight: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="本节随机命中「不引用」的相对权重",
        json_schema_extra={
            "label": "不引用权重",
            "hint": "相对权重；0=不启用",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )
    at_weight: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="本节 @ 对方的概率，与上两项独立叠加",
        json_schema_extra={
            "label": "@ 权重",
            "hint": "独立随机；0=不启用；需开启富回复",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "depends_on": "random_enabled",
            "depends_value": True,
        },
    )


class AdvancedConfig(PluginConfigBase):
    """高级项"""

    __ui_label__ = "高级"
    __ui_icon__ = "wrench"
    __ui_order__ = 8

    verbose_log: bool = Field(
        default=False,
        description="输出每次判定的详细信息",
        json_schema_extra={"label": "详细日志", "hint": "开=输出判定细节，用于排查问题"},
    )
    strip_rich_attachments: bool = Field(
        default=True,
        description=(
            "把富回复参数从下发给模型的 reply 工具中移除，让模型看不到；"
            "关闭后模型可自行附图或表情（引用仍由本插件决定，不受影响）"
        ),
        json_schema_extra={
            "label": "隐藏富回复参数",
            "hint": "开=模型看不到附图、表情；建议保持开启",
        },
    )


class KeywordRule(PluginConfigBase):
    """一条关键词条目（列表项）。

    条目内的引导文字只能写在 ``label`` / ``placeholder`` 上
    （``hint`` 会被 SDK 丢掉，写了也白写）。条目之间的先后顺序就是优先级。
    """

    name: str = Field(
        default="",
        description="备注名，只用于日志与排查",
        json_schema_extra={
            "label": "备注名",
            "placeholder": "如：提及麦麦时引用（可留空）",
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否启用这一条",
        json_schema_extra={
            "label": "启用",
            "placeholder": "关=跳过这一条",
        },
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="被回复的消息里出现任一关键词即视为命中",
        json_schema_extra={
            "label": "关键词",
            "placeholder": "一行一个，如：麦麦",
        },
    )
    action: Literal["引用", "不引用"] = Field(
        default=_ACTION_QUOTE,
        description="命中后如何处理",
        json_schema_extra={
            "label": "命中后",
            "placeholder": "引用 / 不引用",
        },
    )
    session_type: Literal["不限", "仅群聊", "仅私聊"] = Field(
        default=_SCOPE_ANY,
        description="这一条在哪些会话里生效",
        json_schema_extra={
            "label": "生效会话",
            "placeholder": "不限 / 仅群聊 / 仅私聊",
        },
    )
    sessions: list[str] = Field(
        default_factory=list,
        description="只对这些群 / 人生效",
        json_schema_extra={
            "label": "限定群号 / QQ 号",
            "placeholder": "留空=不限",
        },
    )

    @field_validator("session_type", mode="before")
    @classmethod
    def _normalize_scope(cls, value: Any) -> str:
        """把旧配置里的 any / group / private 翻译成中文取值。

        必须用 ``mode="before"``：``Literal`` 只认精确相等，翻译得赶在校验之前。
        认不出来的值一律落回「不限」——这样手改 TOML 打错字时只会退化成
        「对所有会话生效」，而不是让整份配置加载失败。
        """
        text = str(value or "").strip()
        if text in _SCOPE_CHOICES:
            return text
        return _LEGACY_SCOPE_MAP.get(text.lower(), _SCOPE_ANY)

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> str:
        """容错：认不出的取值一律按「引用」处理，别让整份配置加载失败。"""
        text = str(value or "").strip()
        return text if text in _ACTION_CHOICES else _ACTION_QUOTE


class KeywordConfig(PluginConfigBase):
    """关键词黑白名单（可选·可加多条）。 条目自上而下匹配，命中第一条即生效 —— 顺序即优先级"""

    __ui_label__ = "关键词"
    __ui_icon__ = "tag"
    __ui_order__ = 5

    entries: list[KeywordRule] = Field(
        default_factory=list,
        description="关键词条目",
        json_schema_extra={
            "label": "关键词条目",
            "hint": "自上而下匹配，命中第一条即生效（顺序即优先级）",
        },
    )


class QuoteControlConfig(PluginConfigBase):
    """智能引用的全部配置。

    每次回复前按下列顺序判定，**得到第一个明确结论即停止**：

    1. **关键词黑白名单**——被回复的消息命中某一条时直接采用该条的结论
       （条目自上而下匹配，顺序即优先级）；
    2. **群聊 / 私聊**——排除名单、生效名单、间隔条件；
    3. **该会话类型的随机判定**——前述条件均无结论时，按本节权重随机决定；
       关闭随机判定时，改用本节的「固定引用行为」。

    没有全局权重：群聊与私聊各自带一套权重，互不影响。
    日常只需调整「群聊」「私聊」两节；「关键词」是例外清单，可长期留空。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    group: GroupPolicyConfig = Field(default_factory=GroupPolicyConfig)
    private: PrivatePolicyConfig = Field(default_factory=PrivatePolicyConfig)
    keywords: KeywordConfig = Field(default_factory=KeywordConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)

    @field_validator("keywords", mode="before")
    @classmethod
    def _migrate_flat_keywords(cls, value: Any) -> Any:
        """兼容上一版的**扁平** ``[keywords]`` 节。

        上一版是四个平铺字段（``quote_keywords`` / ``quote_scope`` /
        ``no_quote_keywords`` / ``no_quote_scope``）；本版改成条目列表
        ``[[keywords.entries]]``。这里把旧形状翻译成两条条目，避免老配置直接加载失败。

        翻译时**把「不引用」排在前**，以保持旧版「黑名单优先」的语义。
        """
        if not isinstance(value, dict):
            return value
        if "quote_keywords" not in value and "no_quote_keywords" not in value:
            return value
        entries: list[dict[str, Any]] = []
        no_quote = _clean_keywords(value.get("no_quote_keywords"))
        if no_quote:
            entries.append(
                {
                    "name": "不引用关键词（旧配置迁移）",
                    "enabled": True,
                    "keywords": no_quote,
                    "action": "不引用",
                    "session_type": value.get("no_quote_scope", _SCOPE_ANY),
                    "sessions": [],
                }
            )
        quote = _clean_keywords(value.get("quote_keywords"))
        if quote:
            entries.append(
                {
                    "name": "引用关键词（旧配置迁移）",
                    "enabled": True,
                    "keywords": quote,
                    "action": "引用",
                    "session_type": value.get("quote_scope", _SCOPE_ANY),
                    "sessions": [],
                }
            )
        return {"entries": entries}


# 嵌套配置类在模块顶层定义，必须显式 rebuild 才能让 pydantic 解析全部前向引用；
# 否则麦麦加载配置时会抛 PydanticUserError: not fully defined。
GroupPolicyConfig.model_rebuild()
PrivatePolicyConfig.model_rebuild()
KeywordRule.model_rebuild()
KeywordConfig.model_rebuild()
QuoteControlConfig.model_rebuild()


def _plain_id(value: Any) -> str:
    """归一化平台 ID：去掉前缀（如 'qq:123456' -> '123456'）。"""
    text = str(value or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    return text


def _extract_msg_ids(items: Any) -> list[str]:
    """从 before_request 的 items 里按顺序抠出全部 msg_id。

    只认 ``UserMessageItem``（真正的聊天消息），因为助手消息与工具调用不带
    ``<message msg_id=...>`` 标签。返回顺序与上下文顺序一致（旧 → 新）。
    """
    if not isinstance(items, list):
        return []
    ordered: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("item_type") or "") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            for regex in (_MESSAGE_ID_RE, _MESSAGE_ID_RE_SQ):
                for match in regex.finditer(text):
                    msg_id = match.group(1).strip()
                    if msg_id and msg_id not in ordered:
                        ordered.append(msg_id)
    return ordered


def _extract_msg_texts(items: Any) -> dict[str, str]:
    """从 before_request 的 items 里抠出 ``msg_id → 消息正文``。

    只认 ``UserMessageItem``，与 ``_extract_msg_ids`` 同一套口径。
    正文即 ``<message ...>`` 标签之后、下一个 ``<message`` 之前的那段文字，
    并剥掉可能残留的 ``</message>`` 闭合标签。

    同一 msg_id 出现多次时**保留最后一次**（上下文里若有重复，越靠后的越新）。
    """
    if not isinstance(items, list):
        return {}
    texts: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("item_type") or "") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            for match in _MESSAGE_BLOCK_RE.finditer(text):
                msg_id = match.group(1).strip()
                if not msg_id:
                    continue
                body = _MESSAGE_CLOSE_RE.sub("", match.group(2)).strip()
                texts[msg_id] = body
    return texts


def _looks_like_group_hint(session_id: str, kwargs: dict[str, Any]) -> bool | None:
    """只凭 hook 载荷本身能得出的会话类型结论。

    麦麦的 ``session_id`` 是平台、路由与群号/用户号拼接后的 MD5 摘要，
    字符串里不含 "group" 之类的字面量，因此不能只靠关键词猜测。

    Returns:
        ``True`` / ``False`` 表示载荷已给出明确答案；``None`` 表示载荷没说，
        必须另行反查会话表（见 ``QuoteControlPlugin._is_group_session``）。
    """
    for key in ("is_group_chat", "is_group"):
        value = kwargs.get(key)
        if isinstance(value, bool):
            return value

    if str(kwargs.get("group_id") or "").strip():
        return True

    lowered = str(session_id or "").lower()
    for token in ("group", "guild", "channel"):
        if token in lowered:
            return True
    return None


def _items_group_hint(items: Any) -> bool | None:
    """从提示词 items 里嗅探群聊特征。

    群聊的消息行带 ``group_card="..."``（麦麦用 ``user_cardname`` 群名片填充），
    私聊消息一律没有这个属性。因此"出现过 group_card"可以**确定**是群聊；
    没出现过则无法下结论（返回 ``None``），交由会话表判定。
    """
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("item_type") or "") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and _GROUP_CARD_RE.search(text):
                return True
    return None


def _local_session_lookup(session_id: str) -> Any:
    """在当前进程内直接反查麦麦会话表。

    注意：插件跑在**独立子进程**里（``supervisor._spawn_runner`` 用
    ``create_subprocess_exec`` 拉起），这个进程里的 ``chat_manager`` 是另一份
    实例，内存中没有任何会话；能否命中完全取决于它能否自行连上数据库。
    因此这里只当**最后兜底**，首选走宿主能力 ``chat.get_all_streams``。
    """
    try:
        from src.chat.message_receive.chat_manager import chat_manager

        return chat_manager.get_existing_session_by_session_id(
            session_id
        ) or chat_manager.get_session_by_session_id(session_id)
    except Exception:  # noqa: BLE001 - 反查失败不应影响主流程
        return None


def _normalize_scope(scope: Any) -> str:
    """把任意来源的范围值归一化成三种中文取值之一。

    正常只会拿到三种中文取值（``KeywordConfig`` 的校验器已把旧配置里的
    any / group / private 翻译过）。万一有别的东西混进来（例如离线脚本直接
    构造对象、绕过校验），一律按「不限」处理——与校验器的兜底保持一致。
    """
    text = str(scope or "").strip()
    if text in _SCOPE_CHOICES:
        return text
    return _LEGACY_SCOPE_MAP.get(text.lower(), _SCOPE_ANY)


def _scope_allows(scope: Any, actual: str) -> bool:
    """``scope`` 是否覆盖 ``actual`` 这类会话。

    ``actual`` 取 ``_SCOPE_GROUP`` / ``_SCOPE_PRIVATE``（不是「不限」）。
    """
    normalized = _normalize_scope(scope)
    return normalized == _SCOPE_ANY or normalized == actual


def _first_keyword_hit(text: str, keywords: list[str]) -> str:
    """返回 ``keywords`` 里第一个在 ``text`` 中出现的词；都没命中返回空串。

    子串包含、忽略大小写，不写正则、不做分词 —— 用户填什么就按字面找什么。
    返回命中的那个词（而不是布尔）是为了让日志能写清"是被哪个词命中的"。
    """
    if not text:
        return ""
    lowered = text.lower()
    for keyword in keywords:
        if keyword.lower() in lowered:
            return keyword
    return ""


def _clean_keywords(raw: Any) -> list[str]:
    """去掉空串与首尾空白，保留用户填写的顺序。"""
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def _summarize_keyword_entries(entries: Any) -> str:
    """把关键词条目压成一行日志摘要（启动日志 / ``/引用状态`` 都用它）。

    形如 ``3 条（启用 2：引用 1 / 不引用 1）``；一条都没有时是 ``0 条``。
    """
    items = list(entries or [])
    if not items:
        return "0 条"
    enabled = [x for x in items if bool(getattr(x, "enabled", True))]
    quote_n = sum(
        1
        for x in enabled
        if str(getattr(x, "action", _ACTION_QUOTE) or "").strip() != _ACTION_NO_QUOTE
    )
    return (
        f"{len(items)} 条（启用 {len(enabled)}："
        f"引用 {quote_n} / 不引用 {len(enabled) - quote_n}）"
    )


def _describe_keyword_entry(index: int, rule: Any) -> str:
    """把一条关键词条目渲染成 ``/引用关键词`` 里的一行。

    列表项在面板上只能填 ``label`` / ``placeholder``，看不到整体说明，
    所以命令里把「顺序 / 备注 / 关键词 / 命中后 / 生效范围 / 是否启用」
    一次列全，方便用户对着排查。
    """
    name = str(getattr(rule, "name", "") or "").strip() or "（未命名）"
    words = _clean_keywords(getattr(rule, "keywords", None))
    action = str(getattr(rule, "action", _ACTION_QUOTE) or "").strip() or _ACTION_QUOTE
    scope = _normalize_scope(getattr(rule, "session_type", _SCOPE_ANY))
    sessions = _clean_keywords(getattr(rule, "sessions", None))
    parts = [
        f"{index}. {name}",
        f"   命中后：{action}",
        f"   关键词：{'、'.join(words) if words else '（空，永不命中）'}",
        f"   生效会话：{scope}",
    ]
    if sessions:
        parts.append(f"   限定名单：{'、'.join(sessions)}")
    if not bool(getattr(rule, "enabled", True)):
        parts.append("   ⚠ 已停用")
    return "\n".join(parts)


def _weighted_pick(
    *,
    quote_weight: float,
    no_quote_weight: float,
    at_weight: float = 0.0,
    roll: float | None = None,
    roll_at: float | None = None,
) -> str:
    """按权重随机抽取回复形态。

    两次独立抽取，``@`` 与「引用 / 不引用」**叠加**而非互斥：

    1. 先按 ``quote_weight : no_quote_weight`` 抽二态 → 引用 or 不引用；
    2. 再按 ``at_weight`` 独立抽一次 → 是否叠加 @。

    所以 `@` 不抢前两者的比例：调大 `at_weight` 只让 @ 更常出现，
    引用/不引用之间的相对比例保持不变。

    Args:
        quote_weight: 引用的权重，相对值。
        no_quote_weight: 不引用的权重，相对值。
        at_weight: @ 对方的权重，相对值；为 0 即不叠加 @。
        roll: 仅供测试注入的第一抽随机数（``[0, 1)``）。
        roll_at: 仅供测试注入的第二抽随机数（``[0, 1)``）。

    Returns:
        ``"quote"`` / ``"plain"`` / ``"at"`` 之一。
        引用/不引用两项都 ≤ 0 时退化为 ``"plain"``（保守：不引用、不打扰）。
    """
    w_quote = max(0.0, float(quote_weight))
    w_plain = max(0.0, float(no_quote_weight))
    at_on = _roll_below(at_weight, roll_at)

    # 第一抽：引用 or 不引用（只在两者之间归一化）
    total = w_quote + w_plain
    if total <= 0:
        base_quote = False  # 无权重可用 → 保守选择普通回复
    else:
        draw = random.random() if roll is None else roll
        base_quote = draw * total < w_quote

    if not at_on:
        return "quote" if base_quote else "plain"
    # 叠加 @：引用 + @ 时 set_quote 会让麦麦自己 @ 被引用人，故退化为 quote
    if base_quote:
        return "quote"
    return "at"


def _roll_below(weight: float, roll: float | None = None) -> bool:
    """按相对权重抽一次「是否命中」。``weight <= 0`` 恒为 False。"""
    w = max(0.0, float(weight))
    if w <= 0:
        return False
    draw = random.random() if roll is None else roll
    return draw * 1.0 < min(w, 1.0)


def _pick_quote_by_weight(*, quote_weight: float, no_quote_weight: float, roll: float | None = None) -> bool:
    """两态便捷封装：只关心"引用 / 不引用"时使用（保持向后兼容语义）。"""
    return _weighted_pick(
        quote_weight=quote_weight, no_quote_weight=no_quote_weight, at_weight=0.0, roll=roll
    ) == "quote"


def _strip_reply_tool_params(tool_definitions: Any, keys: tuple[str, ...]) -> list[str]:
    """从 tool_definitions 里 ``reply`` 工具的 schema 中删掉指定参数。

    就地修改传入的列表（麦麦后续直接读这份数据），返回**实际被删掉的参数名**。

    ``tool_definitions`` 的形态（``tool_option.to_openai_function_schema``）::

        [{"type": "function",
          "function": {"name": "reply", "description": "...",
                       "parameters": {"type": "object",
                                      "properties": {"msg_id": {...}, ...},
                                      "required": ["msg_id"]}}}]

    只碰 ``reply`` 这一个工具；``required`` 里若有同名字段也一并剔除
    （否则 schema 自相矛盾，某些模型端会校验失败）。

    参数名不在 schema 里（比如麦麦根本没开富回复、没有 ``attach_at``）时
    静默跳过，不写进返回值。
    """
    if not isinstance(tool_definitions, list):
        return []

    removed: list[str] = []
    for tool in tool_definitions:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        if str(function.get("name") or "") != _REPLY_TOOL_NAME:
            continue

        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue

        properties = parameters.get("properties")
        if isinstance(properties, dict):
            for key in keys:
                if properties.pop(key, None) is not None:
                    removed.append(key)

        required = parameters.get("required")
        if isinstance(required, list):
            kept = [name for name in required if name not in keys]
            if len(kept) != len(required):
                parameters["required"] = kept

    return removed


class QuoteControlPlugin(MaiBotPlugin):
    """智能引用主类：通过 planner Hook 接管 reply 的引用参数。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = QuoteControlConfig

    def __init__(self) -> None:
        super().__init__()
        # session_id -> 按顺序排列的 msg_id 列表（旧 → 新）
        self._msg_order: dict[str, list[str]] = {}
        # session_id -> {msg_id: 消息正文}，供关键词黑白名单匹配。
        # 每次都整体替换（不是累加），所以占用只与"本次上下文的消息数"相当。
        self._msg_text: dict[str, dict[str, str]] = {}
        # session_id -> 是否群聊（由 before_request 的 group_card 特征沉淀）
        self._session_is_group: dict[str, bool] = {}
        # 宿主会话表快照：session_id -> 会话信息（is_group_session / group_id / user_id）
        self._session_table_cache: dict[str, dict[str, Any]] | None = None
        self._session_table_at: float = 0.0
        # 会话表查询失败是否已告警（避免每次回复都刷屏）
        self._session_table_warned: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._msg_order.clear()
        self._msg_text.clear()
        self._session_is_group.clear()
        self._session_table_cache = None
        self._session_table_at = 0.0
        kw = self.config.keywords
        self.ctx.logger.info(
            "智能引用已加载：关键词条目=%s，"
            "群聊(会话控制=%s, 固定引用=%s, 间隔阈值=%d, 低于该间隔不引用=%d, "
            "随机判定=%s, 权重=%g/%g/@%g)，"
            "私聊(会话控制=%s, 固定引用=%s, 间隔阈值=%d, 低于该间隔不引用=%d, "
            "随机判定=%s, 权重=%g/%g/@%g)",
            _summarize_keyword_entries(getattr(kw, "entries", None)),
            self.config.group.enabled,
            self.config.group.default_quote,
            self.config.group.min_message_gap,
            self.config.group.no_quote_below_gap,
            self.config.group.random_enabled,
            self.config.group.quote_weight,
            self.config.group.no_quote_weight,
            self.config.group.at_weight,
            self.config.private.enabled,
            self.config.private.default_quote,
            self.config.private.min_message_gap,
            self.config.private.no_quote_below_gap,
            self.config.private.random_enabled,
            self.config.private.quote_weight,
            self.config.private.no_quote_weight,
            self.config.private.at_weight,
        )

    async def on_unload(self) -> None:
        self._msg_order.clear()
        self._msg_text.clear()
        self._session_is_group.clear()
        self._session_table_cache = None
        self._session_table_at = 0.0
        self.ctx.logger.info("智能引用已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        self.ctx.logger.info(
            "智能引用配置已热更新（version=%s）",
            version or SUPPORTED_CONFIG_VERSION,
        )

    # ------------------------------------------------------------------
    # 会话信息（类型 / 对端 ID）
    # ------------------------------------------------------------------

    async def _session_table(self) -> dict[str, dict[str, Any]]:
        """取回 ``session_id → 会话信息`` 映射（带 TTL 缓存）。

        走宿主能力 ``chat.get_all_streams``：它由**主进程**执行，拿到的是权威的
        ``is_group_session`` / ``group_id`` / ``user_id``，不受插件子进程隔离影响
        （这正是原实现用 ``from src.chat... import chat_manager`` 反查会失效的原因）。

        能力调用失败时退回上一份缓存；从未成功过则返回空表，由调用方继续兜底。
        失败只在**首次**打一条 warning，避免每次回复都刷屏。
        """
        now = time.monotonic()
        if (
            self._session_table_cache is not None
            and (now - self._session_table_at) < _SESSION_CACHE_TTL_SECONDS
        ):
            return self._session_table_cache

        def _warn_once(msg: str, *args: Any) -> None:
            if self._session_table_warned:
                return
            self._session_table_warned = True
            _safe_log(self, "warning", msg, *args)

        table: dict[str, dict[str, Any]] | None = None
        try:
            streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
            if isinstance(streams, list):
                table = {}
                for stream in streams:
                    if not isinstance(stream, dict):
                        continue
                    sid = str(
                        stream.get("session_id") or stream.get("stream_id") or ""
                    ).strip()
                    if sid:
                        table[sid] = stream
            elif isinstance(streams, dict) and not streams.get("success", True):
                _warn_once(
                    "查询会话表失败：%s（将改用兜底判定；"
                    "请确认 _manifest.json 的 capabilities 已声明 chat.get_all_streams，"
                    "改动清单后热重载即可生效）",
                    streams.get("error"),
                )
        except Exception as exc:  # noqa: BLE001 - 拿不到就走兜底，绝不影响麦麦
            _warn_once(
                "查询会话表异常（改用兜底判定）：%s；"
                "若提示未获授权，请确认 _manifest.json 的 capabilities 已声明 "
                "chat.get_all_streams（改动清单后热重载即生效）",
                exc,
            )

        if table is not None:
            self._session_table_cache = table
            self._session_table_at = now
            self._session_table_warned = False
            return table
        return self._session_table_cache or {}

    async def _session_info(self, session_id: str) -> dict[str, Any] | None:
        """取单个会话的权威信息；查不到返回 ``None``。"""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        return (await self._session_table()).get(sid)

    async def _is_group_session(
        self, session_id: str, kwargs: dict[str, Any], items: Any = None
    ) -> bool:
        """判定会话类型（群聊 / 私聊），按可信度从高到低逐级回退。

        顺序：

        1. 本次 ``items`` 里出现 ``group_card``（确定性正信号，顺带沉淀到缓存）；
        2. 载荷显式字段 / ``session_id`` 字面量；
        3. 本会话此前沉淀过的结论（``before_request`` 写入）；
        4. 宿主会话表 ``is_group_session``（权威）；
        5. 同进程反查会话表（子进程里多半拿不到，仅兜底）；
        6. 都没有 → 保守判为私聊。
        """
        sid = str(session_id or "").strip()

        if _items_group_hint(items) is True:
            if sid:
                self._session_is_group[sid] = True
            return True

        hint = _looks_like_group_hint(session_id, kwargs)
        if hint is not None:
            return hint

        if sid and sid in self._session_is_group:
            return self._session_is_group[sid]

        stream = await self._session_info(sid)
        if isinstance(stream, dict):
            is_group = bool(stream.get("is_group_session"))
            if sid:
                self._session_is_group[sid] = is_group
            return is_group

        stream_obj = _local_session_lookup(sid)
        if stream_obj is not None:
            is_group = bool(getattr(stream_obj, "is_group_session", False))
            if sid:
                self._session_is_group[sid] = is_group
            return is_group

        return False

    async def _peer_id_for(self, session_id: str, kwargs: dict[str, Any]) -> str:
        """取当前会话的群号（群聊）或用户号（私聊）。

        **不能从 session_id 字符串里解析**：麦麦的 session_id 是
        ``platform + 路由 + 群号/用户号`` 拼接后的 MD5 摘要，本身不含原始 ID。
        因此这里走宿主会话表反查，与 ``_is_group_session`` 同源。

        顺序：载荷显式字段 → 宿主会话表 → 同进程反查 → 尾段兜底。
        """
        payload = kwargs or {}

        # 1. 载荷显式字段（命令载荷会带 group_id / user_id）
        hint = payload.get("is_group_chat")
        if isinstance(hint, bool):
            key = "group_id" if hint else "user_id"
            value = _plain_id(payload.get(key))
            if value:
                return value
        for key in ("group_id", "user_id"):
            value = _plain_id(payload.get(key))
            if value:
                return value

        # 2. 宿主会话表（权威，跨进程可用）
        stream = await self._session_info(session_id)
        if isinstance(stream, dict):
            target = stream.get("group_id") if stream.get("is_group_session") else stream.get("user_id")
            value = _plain_id(target)
            if value:
                return value

        # 3. 同进程反查（子进程里多半拿不到，仅兜底）
        stream_obj = _local_session_lookup(session_id)
        if stream_obj is not None:
            target = (
                stream_obj.group_id
                if getattr(stream_obj, "is_group_session", False)
                else stream_obj.user_id
            )
            value = _plain_id(target)
            if value:
                return value

        # 4. 兜底：仅对**旧格式** session_id（如 qq_group_123456789）取尾段。
        #    新格式是 32 位 MD5，尾段是随机字符——截出来只会得到一个"看起来像 ID
        #    但其实毫无意义"的值，会让名单规则静默失效。宁可为空（规则不匹配 =
        #    保守放行），也不要编一个假 ID 去撞名单。
        text = str(session_id or "").strip()
        if not text or _MD5_HEX_RE.fullmatch(text):
            return ""
        return _plain_id(text.rsplit("_", 1)[-1])

    # ------------------------------------------------------------------
    # Hook 1：记录消息顺序 + 裁剪工具 schema
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="quote_control_record_order",
        description="记录消息顺序；并按配置从 reply 工具参数中移除不需要的富回复参数。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def record_message_order(
        self,
        items: Any = None,
        tool_definitions: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """记录消息顺序；必要时改写 tools，让模型看不到某些参数。

        两件事：

        1. 把 ``items`` 中的 msg_id 顺序存起来，供间隔条数判定使用；
        2. 若 ``advanced.strip_rich_attachments`` 为真，从 ``tool_definitions``
           里 ``reply`` 工具的 ``parameters.properties`` 中**删掉**
           ``_SCHEMA_HIDDEN_KEYS`` 里的全部参数
           （``attach_pic`` / ``attach_emoji`` / ``set_quote`` / ``attach_at``）。

        第 2 点是「暴露面控制」而非「运行期拦截」：这几个参数要么本插件
        不想要（模型随手塞图 / 表情包），要么本插件会全权接管并在
        ``after_response`` 里自己写入（``set_quote`` / ``attach_at``）。
        与其等模型传进来再 pop 掉（白耗 token、模型还会疑惑"为什么没生效"），
        不如**从一开始就不给它看**。``before_request`` 的载荷里本就带
        ``tool_definitions``（``chat_loop_service.py:1093-1095``），就地改这份
        快照即可。

        只移除模型端真正存在的键（如麦麦未开富回复时就没有 ``attach_at``），
        返回值即实际移除的参数名列表。

        只在真的改动了才返回字典；否则返回 ``None``，让麦麦走原路径。
        """
        result: dict[str, Any] | None = None
        try:
            ordered = _extract_msg_ids(items)
            if session_id and ordered:
                self._msg_order[session_id] = ordered
            # 正文缓存：整体替换，避免历史会话无限堆积
            texts = _extract_msg_texts(items)
            if session_id and texts:
                self._msg_text[session_id] = texts
            if self.config.advanced.verbose_log:
                _safe_log(
                    self, "info",
                    "记录消息顺序：session=%s，解析到 %d 条消息（含正文 %d 条）",
                    session_id, len(ordered), len(texts),
                )
        except Exception as exc:  # 只读部分，出错也不影响麦麦
            _safe_log(self, "warning", "记录消息顺序失败（已忽略）：%s", exc)

        try:
            # 顺带沉淀会话类型：group_card 是群聊独有的消息属性，
            # 而 after_response 载荷里没有 items，只能靠这里提前记下来。
            if session_id and _items_group_hint(items) is True:
                self._session_is_group[session_id] = True
        except Exception as exc:  # noqa: BLE001 - 嗅探失败不影响主流程
            _safe_log(self, "warning", "嗅探会话类型失败（已忽略）：%s", exc)

        try:
            advanced = self.config.advanced
            if advanced is not None and advanced.strip_rich_attachments:
                trimmed = _strip_reply_tool_params(tool_definitions, _SCHEMA_HIDDEN_KEYS)
                if trimmed:
                    # 注意：必须走 _build_hook_result 包装成 modified_kwargs，
                    # 并带上原始 kwargs 的全部键（items / item_schema_version …），
                    # 否则宿主既不会采纳改动，后续处理器也会丢参数。
                    result = _build_hook_result(
                        kwargs,
                        items=items,
                        tool_definitions=tool_definitions,
                        session_id=session_id,
                    )
                    _safe_log(
                        self, "info",
                        "已从 reply 工具参数中移除 %d 项：%s（session=%s）",
                        len(trimmed), "、".join(trimmed), session_id,
                    )
        except Exception as exc:  # 移除失败则原样放行，绝不影响麦麦
            _safe_log(self, "warning", "移除 reply 工具参数失败（已忽略）：%s", exc)

        return result

    # ------------------------------------------------------------------
    # Hook 2：改写引用参数
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.after_response",
        name="quote_control_rewrite",
        description="按配置改写 reply 调用的 set_quote / attach_at，实现三态引用控制。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def rewrite_set_quote(
        self, output_items: Any = None, session_id: str = "", **kwargs: Any
    ) -> dict[str, Any] | None:
        """遍历 output_items，改写 reply 调用的引用形态。

        三态分别落成：

        - ``quote`` → ``set_quote=True``，并清掉 ``attach_at``；
        - ``plain`` → ``set_quote=False``，并清掉 ``attach_at``；
        - ``at``    → ``set_quote=False`` + ``attach_at=[目标 msg_id]``。

        ``set_quote`` 与 ``attach_at`` 都不在下发给模型的 schema 里
        （``_SCHEMA_HIDDEN_KEYS``），因此正常情况下 ``args`` 里根本没有这两个
        键，这里**无条件写入**本插件的判定值即可。万一模型仍传了（例如它看过
        旧快照），也会被同一行覆盖掉——两种情形走的是同一条分支。

        另外按 ``advanced.strip_rich_attachments`` 剥掉模型自己传来的
        ``attach_pic`` / ``attach_emoji``（开启富回复后模型才拿得到这两个参数，
        本插件把它们拦掉，避免回复变成随手塞图/表情）。
        """
        try:
            if not isinstance(output_items, list):
                return None

            # 会话类型与对端 ID 都要走宿主能力（本进程拿不到麦麦的内存会话表），
            # 一次解析、循环内复用，避免每条 reply 调用都重复查询。
            is_group = await self._is_group_session(session_id, kwargs)
            peer_id = await self._peer_id_for(session_id, kwargs)
            advanced = self.config.advanced
            strip_attachments = advanced is None or bool(advanced.strip_rich_attachments)
            changed = 0
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("item_type") or "") != "FunctionCallItem":
                    continue
                tool_call = item.get("tool_call")
                if not isinstance(tool_call, dict):
                    continue
                if str(tool_call.get("func_name") or "") != _REPLY_TOOL_NAME:
                    continue

                args = tool_call.get("args")
                if not isinstance(args, dict):
                    args = {}
                    tool_call["args"] = args

                target_msg_id = str(args.get("msg_id") or "").strip()
                form, reason = self._decide(
                    session_id=session_id,
                    is_group=is_group,
                    target_msg_id=target_msg_id,
                    peer_id=peer_id,
                    kwargs=kwargs,
                )

                # 引用形态：quote 才引用，其余一律不引用
                desired_quote = form == _FORM_QUOTE
                original = args.get("set_quote")
                # 关键：宿主 reply 工具的缺省值是 True（缺省即引用），
                # 而本插件的语义默认是「不引用」。模型省略参数时 args 里没有
                # set_quote，`bool(None) == False` 会被误判成「已经是目标态」而
                # 不写入，结果宿主用 True 兜底 → 引用。因此 original is None
                # 时必须显式写入，才能把宿主的缺省 True 覆盖掉。
                if original is None or bool(original) != desired_quote:
                    args["set_quote"] = desired_quote
                    changed += 1

                # @ 形态：写入 attach_at（指向目标消息 → 麦麦解析出它的发送者）
                if form == _FORM_AT and not target_msg_id:
                    # 模型没给目标消息 → 没人可 @，退化为普通回复
                    # （免得日志写着"@对方"、实际却谁也没 @ 到）
                    form = _FORM_PLAIN
                    reason = f"{reason}（无目标消息，退化为普通回复）"
                if form == _FORM_AT and target_msg_id and not _is_rich_reply_enabled():
                    # 麦麦没开富回复 → attach_at 会被丢弃，退化为普通回复
                    form = _FORM_PLAIN
                    reason = f"{reason}（麦麦未开富回复，退化为普通回复）"

                if form == _FORM_AT and target_msg_id:
                    desired_at = [target_msg_id]
                    if args.get("attach_at") != desired_at:
                        args["attach_at"] = desired_at
                        changed += 1
                elif args.pop("attach_at", None) is not None:
                    # 非 @ 形态：清掉可能残留的 attach_at（含模型自己传的）
                    changed += 1

                # 拦截模型自己塞的图 / 表情包
                if strip_attachments:
                    for key in _STRIPPED_RICH_KEYS:
                        if args.pop(key, None) is not None:
                            changed += 1

                if self.config.advanced.verbose_log:
                    _safe_log(
                        self, "info",
                        "引用判定：session=%s 群聊=%s 目标=%s 形态=%s 原set_quote=%s（%s）",
                        session_id, is_group, target_msg_id or "-",
                        _FORM_LABELS.get(form, form), original, reason,
                    )

            if changed:
                _safe_log(
                    self, "info",
                    "已改写 %d 个 reply 参数（session=%s）", changed, session_id,
                )
                # 关键：包装成 modified_kwargs，并保留原始 kwargs 的全部键。
                # 宿主用它整体替换 kwargs；漏掉 item_schema_version 会让宿主
                # 反序列化失败，改写同样作废。
                return _build_hook_result(
                    kwargs,
                    output_items=output_items,
                    session_id=session_id,
                )
        except Exception as exc:  # noqa: BLE001
            # 注意：本 hook 可能在 Runner 之外被调用（自检 / 单测），
            # 此时 self.ctx 不可用，日志本身也不能再抛异常。
            _safe_log(self, "warning", "改写引用参数失败（已忽略，保持麦麦原行为）：%s", exc)
        return None

    # ------------------------------------------------------------------
    # 判定核心
    # ------------------------------------------------------------------

    def _decide(
        self,
        *,
        session_id: str,
        is_group: bool,
        target_msg_id: str,
        peer_id: str = "",
        kwargs: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """算出这条 reply 该用哪种形态发出。

        返回的形态是 ``"quote"`` / ``"plain"`` / ``"at"`` 三者之一：

        - ``"quote"`` —— 引用回复（``set_quote=True``）；
        - ``"plain"`` —— 普通回复（``set_quote=False``，不加 @）；
        - ``"at"`` —— @ 目标消息发送者后回复（``set_quote=False`` + ``attach_at``）。

        判定顺序（自上而下，得到第一个明确结论即停止）：

        1. **关键词条目**（``[[keywords.entries]]``）→ 拿**被回复的那条消息**的正文，
           按条目顺序自上而下匹配，**命中第一条即采用其结论、不再往下看** ——
           所以把「不引用」的条目排在前面就等价于"黑名单优先"。
           目标消息正文不在缓存里时按未命中处理，继续往下走；
        2. 该会话类型未启用会话控制 → 随机判定（本节的「启用随机判定」关闭时
           用该类型的 ``default_quote``）；
        3. 名单判定：命中排除名单 → 一律不引用；生效名单非空且不在其中 →
           随机判定；
        4. 间隔条件（``min_message_gap > 0`` 或 ``no_quote_below_gap > 0`` 时参与判定）：
           - 间隔低于 ``no_quote_below_gap`` → **不引用**，不参与随机判定；
           - 间隔未达到 ``min_message_gap`` → 不否决，落到随机判定；
           - 间隔达到 ``min_message_gap`` → **一律引用**；
        5. 间隔条件**未启用**时，该条件不参与判定，落到随机判定。

        于是两个间隔阈值构成清晰的三段式（以 硬=3 / 软=5 为例）：
        间隔 < 3 → 必不引用；3 ≤ 间隔 < 5 → 随机；间隔 ≥ 5 → 必引用。

        **优先级**：关键词黑白名单 > 硬阈值 > 间隔条件 > 随机判定。
        也就是说随机判定只在"前面的条件都给不出明确结论"时才接手，
        关键词命中时不受随机权重影响。

        **兜底值只有一个来源**：该会话类型的 ``default_quote``（面板「固定引用行为」）。
        它仅在随机判定关闭时被采用，全局不再另设一份兜底值。

        **权重来源**：本节自带 ``quote_weight`` / ``no_quote_weight`` /
        ``at_weight``，随机抽取只用这三个值，不从别处继承（没有全局权重）。
        本节的「启用随机判定」关掉时它们不参与，直接采用 ``default_quote``。

        Returns:
            (形态, 判定原因短标签)
        """
        policy = self.config.group if is_group else self.config.private

        # 1. 关键词条目：命中即给出结论，优先级最高
        #    peer_id 由调用方（异步）预先解析好传进来——解析要走宿主能力，
        #    这里保持同步纯函数，方便离线复演与单测。
        if not peer_id:
            peer_id = _plain_id((kwargs or {}).get("group_id") or (kwargs or {}).get("user_id"))
        keyword_hit = self._keyword_decision(
            session_id=session_id,
            target_msg_id=target_msg_id,
            is_group=is_group,
            peer_id=peer_id,
        )
        if keyword_hit is not None:
            return keyword_hit

        # 2. 该会话类型未启用会话控制 → 随机判定（仍用本节的权重与固定引用行为）
        if not policy.enabled:
            return self._weighted_fallback(
                bool(policy.default_quote), _REASON_DISABLED, policy=policy
            )

        # 3. 名单判定：排除名单直接否决；生效名单为空视为全部放行
        blacklist = {_plain_id(x) for x in (policy.blacklist or []) if _plain_id(x)}
        whitelist = {_plain_id(x) for x in (policy.whitelist or []) if _plain_id(x)}
        if blacklist and peer_id and peer_id in blacklist:
            return _FORM_PLAIN, _REASON_BLACKLIST
        if whitelist and peer_id and peer_id not in whitelist:
            return self._weighted_fallback(
                bool(policy.default_quote), _REASON_NOT_WHITELISTED, policy=policy
            )

        # 4. 间隔条件：软阈值 min_message_gap + 硬阈值 no_quote_below_gap
        #    两者相互独立——只启用硬阈值（min_message_gap=0）时它照样生效。
        min_gap = max(0, int(policy.min_message_gap or 0))
        hard_below = max(0, int(getattr(policy, "no_quote_below_gap", 0) or 0))
        if min_gap <= 0 and hard_below <= 0:
            # 两个阈值都没启用 → 间隔条件不参与判定，落到随机判定
            return self._weighted_fallback(
                bool(policy.default_quote),
                _REASON_GAP_DISABLED,
                policy=policy,
            )

        gap = self._message_gap(session_id, target_msg_id)
        if gap is None:
            # 目标消息不在本次上下文中（过旧 / 缓存未命中）→ 无法判定，落到随机判定
            return self._weighted_fallback(
                bool(policy.default_quote),
                _REASON_TARGET_UNKNOWN,
                policy=policy,
            )

        # 4a. 硬阈值：间隔低于它一律不引用，**不参与随机判定**。
        #     这是"硬否决"，与 min_message_gap 的"软阈值"（未达到就交给随机）不同。
        if hard_below > 0 and gap < hard_below:
            return _FORM_PLAIN, f"{_REASON_GAP_TOO_CLOSE}（间隔 {gap} 条 < {hard_below}）"

        if min_gap <= 0:
            # 只启用了硬阈值，且本条没被它否决 → 落到随机判定
            return self._weighted_fallback(
                bool(policy.default_quote),
                _REASON_GAP_DISABLED,
                policy=policy,
            )

        if gap < min_gap:
            # 间隔条件未命中 → 不否决，交给随机判定
            return self._weighted_fallback(
                bool(policy.default_quote),
                f"{_REASON_GAP_NOT_MET}（间隔 {gap} 条 < {min_gap}）",
                policy=policy,
            )

        # 5. 间隔条件命中 → 一律引用。
        #    没有开关：若允许"命中也不引用"，这个条件就变成"离得远的消息永不引用、
        #    离得近的反而随机"，无人会用；等价效果请用「低于该间隔不引用」
        #    （硬阈值，填同一个 N）表达。
        return _FORM_QUOTE, f"{_REASON_GAP_MET}（间隔 {gap} 条 ≥ {min_gap}）"

    def _msg_text_for(self, session_id: str, msg_id: str) -> str | None:
        """取某条消息的正文；没缓存到返回 None。

        取不到时**不要**退化成空串去匹配——空串和"这条消息确实没有文字"
        （例如纯图片消息）是两回事，分开处理才能让日志说清原因。
        """
        if not msg_id:
            return None
        cached = self._msg_text.get(session_id)
        if not cached:
            return None
        return cached.get(str(msg_id).strip())

    def _keyword_decision(
        self,
        *,
        session_id: str,
        target_msg_id: str,
        is_group: bool,
        peer_id: str = "",
    ) -> tuple[str, str] | None:
        """按关键词黑白名单给出结论；没有条目命中则返回 ``None``（交给后续判定）。

        条目**自上而下匹配，命中第一条即停** —— 顺序即优先级，用户把
        「不引用」排在前面就等价于"黑名单优先"。

        判定分两轮，先筛"适用条目"再取正文，避免为不相关的条目白查缓存：

        1. 逐条筛掉不适用的：未启用 / 生效会话不覆盖 / 不在限定名单里 / 关键词为空；
        2. 一条适用的都没有 → 本节不参与；
        3. 取被回复消息的正文，取不到 → 本节不参与（与间隔条件同样保守）；
        4. 按顺序对适用条目做子串匹配，第一个命中的条目给出结论。
        """
        config = getattr(self.config, "keywords", None)
        rules = list(getattr(config, "entries", None) or []) if config else []
        if not rules:
            return None

        actual_scope = _SCOPE_GROUP if is_group else _SCOPE_PRIVATE
        peer = _plain_id(peer_id)

        applicable: list[tuple[int, Any, list[str]]] = []
        for index, rule in enumerate(rules):
            if not bool(getattr(rule, "enabled", True)):
                continue
            if not _scope_allows(getattr(rule, "session_type", _SCOPE_ANY), actual_scope):
                continue
            limited = {_plain_id(x) for x in (getattr(rule, "sessions", None) or [])}
            limited.discard("")
            if limited and (not peer or peer not in limited):
                # 配了限定名单但当前拿不到 peer_id → 无法确认适用，跳过而不是误用
                continue
            words = _clean_keywords(getattr(rule, "keywords", None))
            if not words:
                continue
            applicable.append((index, rule, words))

        if not applicable:
            return None

        text = self._msg_text_for(session_id, target_msg_id)
        if text is None:
            # 目标消息过旧 / 不在本次上下文里 → 无法匹配。
            # 与间隔条件同样保守：宁可放过，不误判。
            return None

        for index, rule, words in applicable:
            hit = _first_keyword_hit(text, words)
            if not hit:
                continue
            label = str(getattr(rule, "name", "") or "").strip() or f"第 {index + 1} 条"
            if str(getattr(rule, "action", _ACTION_QUOTE) or "").strip() == _ACTION_NO_QUOTE:
                return _FORM_PLAIN, f"{_REASON_KEYWORD_NO_QUOTE}「{hit}」（{label}）"
            return _FORM_QUOTE, f"{_REASON_KEYWORD_QUOTE}「{hit}」（{label}）"
        return None

    def _effective_weights(self, policy: Any) -> tuple[float, float, float]:
        """取该会话类型生效的权重三元组 ``(引用, 不引用, @)``。

        权重就存在会话策略里（群聊 / 私聊各一套，互不影响），没有全局权重，
        所以这里只是把本节三个值取出来。
        """
        return (
            float(getattr(policy, "quote_weight", 0.0) or 0.0),
            float(getattr(policy, "no_quote_weight", 0.0) or 0.0),
            float(getattr(policy, "at_weight", 0.0) or 0.0),
        )

    def _weighted_fallback(
        self, fixed_value: bool, reason: str, policy: Any
    ) -> tuple[str, str]:
        """各条件均无结论时的兜底：按随机权重决定；随机判定关闭则用固定值。

        Args:
            fixed_value: 本节「启用随机判定」关闭时要采用的固定结论（True=引用）。
            reason: 走到这里的判定原因短标签，会拼进返回文案。
            policy: 当前会话策略——权重与「启用随机判定」都取自它。
        """
        if not bool(getattr(policy, "random_enabled", True)):
            return (_FORM_QUOTE if fixed_value else _FORM_PLAIN), reason
        # 权重来源：本节自带的三个权重
        w_quote, w_no_quote, w_at = self._effective_weights(policy)
        # @ 需麦麦开富回复；没开就当作不叠加，免得抽中了还得退化、日志来回变
        rich_reply = _is_rich_reply_enabled()
        at_weight = w_at if rich_reply else 0.0
        form = _weighted_pick(
            quote_weight=w_quote,
            no_quote_weight=w_no_quote,
            at_weight=at_weight,
        )
        # 只有「用户配了 @（at_weight > 0）但麦麦没开富回复」才提示。
        # 用户压根没配 @、或富回复正常时都不提示——否则会把排查方向带偏
        # （明明是自己没配 @，日志却怪麦麦没开富回复）。
        if w_at > 0 and not rich_reply:
            suffix = "（@ 未叠加：麦麦未开富回复）"
        else:
            suffix = ""
        return form, f"{reason}｜随机→{_FORM_LABELS[form]}{suffix}"

    def _message_gap(self, session_id: str, target_msg_id: str) -> int | None:
        """被回复消息距最新消息隔了多少条。

        用 ``before_request`` 缓存的消息顺序表定位目标 msg_id 的索引，
        与列表末尾（最新）求差。目标不在表中时返回 None。
        """
        if not target_msg_id:
            return None
        ordered = self._msg_order.get(session_id)
        if not ordered:
            return None
        try:
            index = ordered.index(target_msg_id)
        except ValueError:
            return None
        return len(ordered) - 1 - index

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @Command(
        "quote_status",
        description="查看当前会话的智能引用状态与判定结果",
        pattern=r"^(/引用状态|/引用查询)$",
    )
    async def cmd_quote_status(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
        is_group = await self._is_group_session(stream_id, kwargs)
        policy = self.config.group if is_group else self.config.private
        kw = self.config.keywords
        actual_scope = _SCOPE_GROUP if is_group else _SCOPE_PRIVATE

        # 关键词条目：命令侧拿不到「被回复的那条消息」，没法真的匹配一次，
        # 只能报告配置情况 + 哪些条目覆盖当前会话类型。
        entries = list(getattr(kw, "entries", None) or [])
        if entries:
            in_scope = [
                r
                for r in entries
                if _scope_allows(getattr(r, "session_type", _SCOPE_ANY), actual_scope)
            ]
            usable = [r for r in in_scope if bool(getattr(r, "enabled", True))]
            keywords_line = (
                f"关键词条目：{_summarize_keyword_entries(entries)}"
                f"｜本会话类型适用 {len(usable)} 条"
                f"（按被回复消息的正文自上而下匹配，需在回复时判定，此处不计入）"
            )
        else:
            keywords_line = "关键词条目：未配置"

        w_quote, w_no_quote, w_at = self._effective_weights(policy)
        random_on = bool(getattr(policy, "random_enabled", True))
        if random_on:
            weights_desc = (
                f"引用 {w_quote:g} / 不引用 {w_no_quote:g}"
                f"（二选一）；@ {w_at:g}（叠加）"
            )
            if w_at > 0 and not _is_rich_reply_enabled():
                weights_desc += "；麦麦未开启富回复，@ 不参与"
        else:
            weights_desc = "未启用（改用「固定引用行为」判定）"

        lines = [
            f"会话类型：{'群聊' if is_group else '私聊'}（{stream_id or '未知'}）",
            keywords_line,
            f"随机判定：{weights_desc}",
            f"会话控制：{'启用' if policy.enabled else '关闭'}",
            f"固定引用行为：{'引用' if policy.default_quote else '不引用'}（仅在随机判定关闭时生效）",
            f"引用间隔阈值：{policy.min_message_gap}（0=不启用）",
            f"低于该间隔不引用：{policy.no_quote_below_gap}（0=不启用）",
            f"已缓存消息数：{len(self._msg_order.get(stream_id) or [])}",
        ]
        text = "\n".join(lines)
        await self.ctx.send.text(text, stream_id)
        return True, text, True

    @Command(
        "quote_keywords",
        description="打印关键词条目与群聊 / 私聊策略概览",
        pattern=r"^(/引用关键词|/引用配置)$",
    )
    async def cmd_quote_keywords(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        g, p = self.config.group, self.config.private
        kw = self.config.keywords

        entries = list(getattr(kw, "entries", None) or [])
        if entries:
            keywords_text = "\n".join(
                _describe_keyword_entry(i, rule) for i, rule in enumerate(entries, 1)
            )
            keywords_text += (
                "\n（自上而下匹配，命中第一条即采用其结论、不再往下看 —— "
                "把「不引用」排在前面就等价于黑名单优先）"
            )
        else:
            keywords_text = "未配置（留空 = 全部按群聊 / 私聊策略判定）"

        rich_reply = _is_rich_reply_enabled()

        def _policy_line(label: str, pol: Any) -> str:
            """一行展示某类会话的策略（含两个间隔阈值与本节的随机权重）。"""
            if int(pol.min_message_gap or 0) > 0:
                gap_part = f"间隔≥{pol.min_message_gap} 条"
            else:
                gap_part = "不启用间隔"
            hard_part = (
                f"｜间隔<{pol.no_quote_below_gap} 条不引用"
                if int(pol.no_quote_below_gap or 0) > 0
                else ""
            )
            if bool(getattr(pol, "random_enabled", True)):
                q = max(0.0, float(getattr(pol, "quote_weight", 0.0) or 0.0))
                nq = max(0.0, float(getattr(pol, "no_quote_weight", 0.0) or 0.0))
                at = min(max(0.0, float(getattr(pol, "at_weight", 0.0) or 0.0)), 1.0)
                at_part = f"｜@ {at * 100:.0f}%"
                if at > 0 and not rich_reply:
                    at_part += "（麦麦未开富回复，不参与）"
                if q + nq > 0:
                    wpart = (
                        f"｜权重 引用 {q / (q + nq) * 100:.0f}%"
                        f" / 不引用 {nq / (q + nq) * 100:.0f}%{at_part}"
                    )
                else:
                    # 两项全 0：@ 仍是独立叠加抽，仍会照抽——说清楚，免得以为 @ 也停了
                    wpart = f"｜权重全 0 → 一律不引用{at_part}"
            else:
                wpart = "｜随机判定 关闭"
            return (
                f"{label}：{'启用' if pol.enabled else '关闭'}"
                f"｜固定引用 {'引用' if pol.default_quote else '不引用'}"
                f"｜{gap_part}{hard_part}{wpart}"
                f"｜生效名单 {len(pol.whitelist)} 个｜排除名单 {len(pol.blacklist)} 个"
            )

        text = (
            f"【智能引用】\n"
            f"— 关键词条目（按被回复消息的正文匹配，自上而下、命中即停）—\n"
            f"{keywords_text}\n"
            f"— 群聊 / 私聊策略（各自带权重，互不影响）—\n"
            f"{_policy_line('群聊', g)}\n"
            f"{_policy_line('私聊', p)}"
        )
        await self.ctx.send.text(text, stream_id)
        return True, text, True


def create_plugin() -> QuoteControlPlugin:
    """Runner 加载入口。"""
    return QuoteControlPlugin()
