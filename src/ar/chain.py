"""多源回退链：路由定义、逐跳凭据解析、额度冷却表（形状照本机 DSH 插件 ``dsh-llm-auto``）。

为什么不直接调插件：``dsh-llm-auto`` 是 DSH 宿主内部的模型适配器，只对外提供
``/api/llm-auto/{routes,catalog,diag}`` 三个只读端点，**没有** OpenAI 兼容的
``POST /chat/completions``。所以回退链的规矩在 Python 侧复刻一份：

- **链序**：配置里的顺序就是尝试顺序（``ordering.mode: manual`` 的口径）；
- **换跳判据**：黑名单 + 默认换——只有 ``ABORTED`` 一类调用方主动放弃才不换，
  其余失败（AUTH / QUOTA / SERVER / INVALID_REQUEST / UNKNOWN …）一律换下一跳；
- **换跳前先摘思考**：上游在历史里看到 ``reasoning_content`` 却吃不下，会 400 或把它摊成正文
  （插件 §3「跨路由历史思考摘除」踩过同一个坑）；
- **额度冷却**：额度类失败（``QUOTA`` 余额打满 / ``QUOTA_RATE_WINDOW`` 窗口打满）先同步记一条
  5 分钟短闸门，再**只查这一家、绕缓存**确认：确认有档位打满就改成"被耗尽那几档里最晚的重置
  时刻"，确认不了只留短闸门（插件 0.11 的顺序，见 ``ModelClient._confirm_quota_failure``）；
  没密钥（``NOT_CONFIGURED``）直接按默认时长冷；``AUTH`` **不冷却**；**键是 provider 而不是
  单跳**（额度是账号级的，一个 provider 在链上只允许出现一次）；
- **短闸门**：某 trip 打了个 429，同一次调用里其余 trip 在 ``gate_ms`` 内也跳过它
  （瞬时限速不冷却，只在本轮内生效）；
- **兜底**：过滤后一跳不剩（全冷）⇒ **忽略冷却**照链硬走，避免假故障「链已用尽」。

**密钥纪律**：每一跳的密钥只在发请求那一刻现读（环境变量 > 本机 DSH 凭据文件
``~/.dsh/.credentials.yaml`` 的 ``refs.<名字>``），既不进 ``Route``（它进 ``Config``、
会进 ``public_dict()`` 与元数据），也不落盘、不打印；来源只以 ``env:名字`` / ``file:名字`` 的形式出现。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DEFAULT_COOLDOWN_MS",
    "cooldown_file_path",
    "DEFAULT_GATE_MS",
    "SHORT_GATE_MS",
    "COOLDOWN_REASONS",
    "QUOTA_CODES",
    "ROUTE_KEYS",
    "Route",
    "CooldownEntry",
    "CooldownTable",
    "DEFAULT_ROUTES_YAML",
    "ORDER_REASONS",
    "ORDERING_MODES",
    "QUOTA_KINDS",
    "OrderedRoute",
    "order_routes",
    "resolve_resets",
    "parse_reset_at",
    "default_credentials_path",
    "default_routes_path",
    "parse_routes",
    "resolve_api_key",
    "load_routes_file",
]

#: 默认冷却时长：没密钥（``NOT_CONFIGURED``）这类"不会自己好"的失败冷这么久；
#: 额度类是「5 分钟短闸门 + 补查确认后按重置时刻」（见 ``SHORT_GATE_MS``），不用这个值
DEFAULT_COOLDOWN_MS = 600_000
#: 额度类失败**补查确认之前**的同步短闸门（插件 ``DEFAULT_GATE_MS`` 同值）：补查要一次 HTTPS
#: 往返，这期间已经排好序的并发批（rewrite 默认 4）会照旧白撞同一家
SHORT_GATE_MS = 5 * 60 * 1000
#: 同一次调用内的短闸门：429 之后这么长时间里其余 trip 也跳过它
DEFAULT_GATE_MS = 120_000
#: 额度类失败码（余额打满 / 窗口打满）：都要「先记短闸门 → 只查这一家 → 确认则换成重置时刻」
QUOTA_CODES = ("QUOTA", "QUOTA_RATE_WINDOW")
#: 会触发冷却的错误码（瞬时错误不冷却：下一次说不定就好了，冷却只会误伤）。
#:
#: - ``QUOTA`` / ``QUOTA_RATE_WINDOW``：额度打满，走「短闸门 → 补查确认 → 换成重置时刻」；
#: - ``NOT_CONFIGURED``：没密钥 ⇔ 没凭据（不会自己好），按 ``DEFAULT_COOLDOWN_MS`` 冷；
#: - **``AUTH`` 不在这里**（插件 0.11 的口径）：鉴权错的原因常常在调用方（key 填错 / 环境变量没设），
#:   改好就该立刻可用；插件实测 commandcode 快通道报额度、慢通道报 AUTH，冷掉 AUTH 会把
#:   一条其实能用的通道停十分钟。
#:
#: ⚠ 窗口额度耗尽（``QUOTA_RATE_WINDOW``）**要冷**：AR 一个 provider 只有一条 route，
#: 插件那句"只停那一个通道"在这边等于"什么都停不住" —— 不冷就是每次调用都先去白撞一次，
#: 直到窗口自己重置（周窗最长七天）。
COOLDOWN_REASONS = (*QUOTA_CODES, "NOT_CONFIGURED")
#: 认识的额度接口（与 DSH 插件的 ``quota.sources`` 一一对应；其余 provider 一律按量）
QUOTA_KINDS = ("commandcode", "opencode-go")
#: ``order_routes`` 的三个桶（与插件同口径：按月重置升序 → 额度查不到 → 按量兜底）
ORDER_REASONS = ("monthly-reset-asc", "subscription-reset-unknown", "unknown")
#: 排序模式：auto = 插件那种「按重置时刻升序 + 跳过冷却中的家」；manual = 严格按配置顺序
ORDERING_MODES = ("auto", "manual")

#: 路由项允许的键（其余键报错，防止 ``api_key`` 之类的错写静默失效）
ROUTE_KEYS = (
    "provider",
    "model",
    "base_url",
    "api_key_env",
    "headers",
    "quota",
    "quota_base",
    # 手动覆盖（探测不到时的逃生口）：subscription / reset_at
    "subscription",
    "reset_at",
    # 推理档位（插件 0.10 的 keepThinking / breakToolLoop 是「工具循环」的事，AR 目前不发工具，
    # 那条不搬；档位是**现在就有用**的：每跳一张表，按名字取上游认识的那个值）
    "reasoning_effort",
    "reasoning_efforts",
)

#: ``uv run ar routes import`` 用的默认片段：本机 DSH 插件当前生效的那条链
DEFAULT_ROUTES_YAML = """# 回退链默认片段：本机 DSH ``dsh-llm-auto`` 插件当前生效的那条链（2026-10-26 抄自
# ``~/.dsh/profiles/desktop/cordis.patch.yml`` 的 ``- id: llm-auto``），顺序即尝试顺序。
# 密钥不进这里：每跳只写 ``api_key_env``，值现读环境变量或 ``~/.dsh/.credentials.yaml`` 的 ``refs``。
#
# 用别的链：另存一份 YAML，然后 ``uv run ar routes import <该文件>``（或直接把片段贴进
# config.local.json 的 "routes"，两种写法等价；YAML 里的注释/下划线键在读入时忽略）。
# 每项可写：provider / model / base_url / api_key_env / headers /
#   subscription（订阅制，额度按账号级记账）/ reset_at（月度重置时刻，epoch 毫秒或带时区的 ISO 串）。
# 排序（auto，默认）与 DSH 插件的 auto 同口径：① 订阅且知道月度重置时刻的按重置时刻升序 →
#   ② 订阅但重置时刻未知的按配置顺序 → ③ 按量计费的永远最后；冷却中的家跳过，全冷时照链硬走。
# 所以下面的书写顺序只在「同一桶内」有意义；要不要调整先后请改 reset_at / subscription，而不是挪行。
# quota：这家的额度接口类型。写了它，排序会去上游现查「月度重置时刻」——不用手填任何时间。
# commandcode → GET {quota_base}/alpha/billing/credits + /alpha/billing/subscriptions（取 currentPeriodEnd）；
#   「打满」= 任一窗口 exceeded，**或**余额低于 $0.2（ar.quota.CREDITS_FLOOR_USD：上游在"余额不够一次
#   预扣"时就拒，而它给的 belowThreshold 阈值是 0、永不触发 —— 详见 README §5.0）
- { provider: commandcode, model: deepseek/deepseek-v4.1-flash, api_key_env: CMD_API_KEY,
    base_url: https://api.commandcode.ai/provider/v1, quota: commandcode,
    quota_base: https://api.commandcode.ai }
# opencode-go → GET {quota_base}/usage（取 usage.monthly.resetsAt）；
# 推理接口必须带会话头，否则回 400 MissingSessionID（值抄自本机 profile 的 llm-pi-ai.providers.opencode-go）。
- { provider: opencode-go, model: deepseek-v4.1-flash, api_key_env: OPENCODE_API_KEY,
    base_url: https://opencode.ai/zen/go/v1, quota: opencode-go,
    quota_base: https://opencode.ai/zen/go/v1,
    headers: { x-opencode-session: 596b2b83-2a18-4d76-86a5-297d7b6ffd4c } }
# ju-liu / deepseek-official 都是按量计费：没有重置时刻，auto 排序里永远排最后，桶内保持这个顺序。
- { provider: ju-liu, model: DeepSeek-V4.1-flash, api_key_env: JU_LIU_API_KEY,
    base_url: https://juflowai.com/v1 }
# 官方直连：模型 id 是 deepseek-flash（2026-10-26 用 /models 实测），密钥读环境变量 DEEPSEEK_API_KEY。
- { provider: deepseek-official, model: deepseek-flash, api_key_env: DEEPSEEK_API_KEY,
    base_url: https://api.deepseek.com }
"""

_API_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_routes_path() -> Path:
    """默认的链片段路径：``<项目根>/config.routes.yaml``（存在才用，不存在就回落单源模式）。"""
    from .paths import PROJECT_ROOT

    return PROJECT_ROOT / "config.routes.yaml"


def cooldown_file_path() -> Path:
    """冷却表落盘位置：``~/.dsh/storages/adaptive_reader_cooldowns.json``（``DSH_HOME`` 可覆盖根目录）。

    放在 DSH 的 ``storages`` 下是照插件的先例（它的冷却落在 ``storages/llm_auto_cooldown.json``）：
    那目录本来就是「跨进程活着的状态」的落脚点，备份与清理都有固定去处。
    """
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(home) / "storages" / "adaptive_reader_cooldowns.json"


def default_credentials_path() -> Path:
    """本机 DSH 凭据文件（``DSH_HOME`` 可覆盖 ``~/.dsh``）。"""
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(home) / ".credentials.yaml"


# ------------------------------------------------------------------ 路由定义 --
@dataclass(frozen=True)
class Route:
    """链上的一跳（**不含密钥**：密钥只在发请求时按 ``api_key_env`` 现读）。

    ``auto_order`` 用到的两个属性与 DSH 插件同一个定义：

    - ``subscription``：订阅制还是按量（按量的**永远排最后**，且「没有重置时刻」）；
    - ``reset_at_ms``：**月度**重置时刻（插件口径：只用月度档，不用 5h/周速率窗——按速率窗排会每几小时抖一次）。
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    headers: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    #: 额度接口的 kind（``commandcode`` / ``opencode-go``），见 ``ar.quota``；不写 = 不去探测
    quota: str | None = None
    #: 上游接口根（额度接口挂在它下面：CC 是 ``https://api.commandcode.ai``，Go 是 ``https://opencode.ai/zen/go/v1``）
    quota_base: str | None = None
    #: 手动覆盖：给了就当订阅 / 按月重置排序（``quota`` 探测不到时的逃生口）
    subscription_override: bool | None = None
    reset_at_override_ms: int | None = None
    #: 这一跳的**默认**推理档位：名字或**上游原值**（``null`` = 不写该键；字符串 = 进 ``reasoning_effort``）
    reasoning_effort: str | None = None
    #: 这一跳认识哪些档位名（插件 ``reasoningEfforts`` 那张表）；不给 = 只认 ``reasoning_effort`` 那一个值
    reasoning_efforts: tuple[tuple[str, str | None], ...] = field(default=(), compare=False)

    def reasoning_value(self, effort: str | None = None) -> tuple[bool, str | None]:
        """这一次请求要带的档位 → ``(是否该写 reasoning_effort 这个键, 写什么值)``。

        - ``effort`` 给了：查这张路由的 ``reasoning_efforts`` 表，**认不出来就整键不写**（插件口径，
          绝不把陌生的档位名硬塞给上游）；
        - ``effort`` 没给：用 ``reasoning_effort``（默认档），``None`` = 不写。
        "写但值是 null"与"不写"在 OpenAI 风格请求里是两回事，所以返回的是二元组不是单值。
        """
        if effort is not None:
            table = dict(self.reasoning_efforts)
            return (True, table[effort]) if effort in table else (False, None)
        return (True, self.reasoning_effort) if self.reasoning_effort is not None else (False, None)

    @property
    def label(self) -> str:
        """人读标签 ``provider/model``（与插件日志、README 同一写法）。"""
        return f"{self.provider}/{self.model}"

    def public_dict(self) -> dict[str, Any]:
        """可打印视图：头（可能带会话 id）原样给出，密钥名给出，值一律不给。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "headers": dict(self.headers),
            "quota": self.quota,
            "quota_base": self.quota_base,
            "subscription_override": self.subscription_override,
            "reset_at_override_ms": self.reset_at_override_ms,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_efforts": dict(self.reasoning_efforts),
        }


def _route_from_mapping(item: Any, index: int) -> Route:
    """单条路由项 → ``Route``；形状不对就 ``InputError``（配置错误一律早报，不静默跳过）。"""
    from .errors import InputError

    where = f"配置项 routes[{index}]"
    if not isinstance(item, dict):
        raise InputError(f"{where} 必须是 JSON 对象，实际是 {type(item).__name__}")
    unknown = [key for key in item if key not in ROUTE_KEYS and not str(key).startswith("_")]
    if unknown:
        raise InputError(
            f"{where} 里有未知键 {', '.join(map(str, unknown))}（可用：{'、'.join(ROUTE_KEYS)}；"
            "下划线开头的键当注释忽略）"
        )
    provider = item.get("provider")
    model = item.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise InputError(f"{where}.provider 必须是非空字符串")
    if not isinstance(model, str) or not model.strip():
        raise InputError(f"{where}.model 必须是非空字符串")
    base_url = item.get("base_url")
    if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
        raise InputError(f"{where}.base_url 必须是非空字符串（或整段不写）")
    env_name = item.get("api_key_env")
    if env_name is None and isinstance(base_url, str) and base_url.strip():
        # 有直连 base_url 却没有密钥名：发出去一定 401，早点报错
        raise InputError(f"{where}.api_key_env 必填（这一跳要直连 {base_url.strip()}）")
    if env_name is not None and (not isinstance(env_name, str) or not _API_KEY_NAME_RE.match(env_name.strip())):
        raise InputError(f"{where}.api_key_env 必须是环境变量名的形式（字母/数字/下划线）")

    headers_raw = item.get("headers") or {}
    if not isinstance(headers_raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers_raw.items()
    ):
        raise InputError(f"{where}.headers 必须是「字符串 → 字符串」的对象")

    quota = item.get("quota")
    if quota is not None and quota not in QUOTA_KINDS:
        raise InputError(f"{where}.quota 只能是 {' / '.join(QUOTA_KINDS)}（或整段不写 = 不探测）")
    quota_base = item.get("quota_base")
    if quota_base is not None and (not isinstance(quota_base, str) or not quota_base.strip()):
        raise InputError(f"{where}.quota_base 必须是非空字符串（或整段不写）")

    subscription = item.get("subscription")
    if subscription is not None and not isinstance(subscription, bool):
        raise InputError(f"{where}.subscription 必须是 true / false（或整段不写 = 由探测决定）")
    # 手动覆盖：不给就不填键；给了就必须比"没给"更聪明——用来兜住上游改形状 / 断网的时候
    reset_at_ms = parse_reset_at(item.get("reset_at"), where=f"{where}.reset_at")

    return Route(
        provider=provider.strip(),
        model=model.strip(),
        base_url=base_url.strip() if isinstance(base_url, str) and base_url.strip() else None,
        api_key_env=env_name.strip() if isinstance(env_name, str) else None,
        headers=tuple(sorted((key.lower(), value) for key, value in headers_raw.items())),
        quota=quota,
        quota_base=quota_base.strip() if isinstance(quota_base, str) else None,
        subscription_override=subscription,
        reset_at_override_ms=reset_at_ms,
        reasoning_effort=_effort_value(item.get("reasoning_effort"), where=f"{where}.reasoning_effort"),
        reasoning_efforts=_effort_table(item.get("reasoning_efforts"), where=f"{where}.reasoning_efforts"),
    )


def _effort_value(raw: Any, *, where: str) -> str | None:
    """``reasoning_effort`` 的值 → 字符串或 ``None``（``null`` / 不写都表示「不写这个键」）。"""
    from .errors import InputError

    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise InputError(f"{where} 必须是非空字符串，或整段不写（null = 不写该键）")
    return raw.strip()


def _effort_table(raw: Any, *, where: str) -> tuple[tuple[str, str | None], ...]:
    """``reasoning_efforts`` 的档位表 → ``((名字, 上游值), ...)``；值可以是 ``null``（= 不写该键）。"""
    from .errors import InputError

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise InputError(f"{where} 必须是「档位名 → 上游值」的对象")
    table: list[tuple[str, str | None]] = []
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise InputError(f"{where} 里有非法档位名 {name!r}")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InputError(f"{where}.{name} 必须是字符串，或 null（= 该档不写 reasoning_effort）")
        table.append((name.strip(), value.strip() if isinstance(value, str) else None))
    return tuple(table)


def parse_reset_at(raw: Any, *, where: str = "配置项 routes[].reset_at") -> int | None:
    """把 ``reset_at`` 解析成 epoch 毫秒：接受 epoch 毫秒整数，或带时区的 ISO 8601 串。

    只有带时区的 ISO 串才收（``2026-11-01T00:00:00+08:00`）——裸时刻在不同机器上会解释成不同时间，
    静默差 8 小时会让冷却提前放行、又去白撞一家已打满的订阅。认不出来直接报错。
    """
    from .errors import InputError

    if raw is None:
        return None
    if isinstance(raw, bool):
        raise InputError(f"{where} 必须是 epoch 毫秒整数或带时区的 ISO 8601 串")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return int(text)
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InputError(f"{where} 认不出来：{raw!r}（用 epoch 毫秒或 2026-11-01T00:00:00+08:00 这种带时区的写法）") from exc
        if moment.tzinfo is None:
            raise InputError(f"{where} 缺少时区：{raw!r}（例如改成 {text}+08:00）")
        return int(moment.timestamp() * 1000)
    raise InputError(f"{where} 必须是 epoch 毫秒整数或带时区的 ISO 8601 串")


def parse_routes(raw: Any) -> tuple[Route, ...]:
    """``routes`` 段 → ``Route`` 元组；``None`` / 空数组 = 单源模式（返回空元组）。

    每一项都必须是**带直连地址的对象**：单源模式那条 ``base_url`` 认不出是哪个 provider，
    所以链上的每一跳都得自己说明地址。
    """
    from .errors import InputError

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or isinstance(raw, (str, bytes)):
        raise InputError(f"配置项 routes 必须是数组，实际是 {type(raw).__name__}")
    parsed = [_route_from_mapping(item, index) for index, item in enumerate(raw)]
    # 只声明了 provider/model、没写 base_url 的项**不参与**链（插件那种写法在 DSH 里由 adapter 解析地址，
    # 这里没有那层解析）；写成下划线注释键的项一律当注释
    routes = tuple(route for route in parsed if route.base_url)
    if not routes:
        return ()
    seen: set[str] = set()
    for route in routes:
        if route.provider in seen:
            # 冷却按 provider 记账，同家出现两次会让「跳过这条」的语义含糊
            raise InputError(f"配置项 routes 里 provider {route.provider!r} 出现了多次（冷却按 provider 记，请合并）")
        seen.add(route.provider)
    return routes


# ------------------------------------------------------------------ 自动排序 --
@dataclass(frozen=True)
class OrderedRoute:
    """排序结果里的一项：路由 + 为什么排在这儿 + 是不是在冷却。"""

    route: Route
    reason: str
    cooling: bool = False
    #: 这一跳**此刻**的月度重置时刻（``ar.quota`` 探测出来的；查不到就是 None）
    reset_at_ms: int | None = None


def order_routes(routes: tuple[Route, ...], report: Any = None, cooling: set[str] | None = None) -> list[OrderedRoute]:
    """插件 ``auto`` 排序的等价实现（**不改配置**，每次请求现算）。

    ``report`` 是 ``ar.quota.QuotaReport``（按 provider 查）；没给 = 什么都不查，全按量。

    - ① **按月重置升序**：从额度接口探测到月度重置时刻的（CC 的 ``currentPeriodEnd`` /
      Go 的 ``usage.monthly.resetsAt``）——越早回血越靠前；
    - ② **额度查不到的**：保持配置顺序（断网 / 上游改形状 / 没有额度接口，但配了 ``quota``）；
    - ③ **按量兜底的**：没配 ``quota`` 的（如 ju-liu / deepseek-official）**永远最后**，桶内按配置顺序；
    - 冷却中的条目**照样列出**（标 ``cooling``），跳不跳过由调用方决定——这里只排序。

    冷却**不改变排序键**（插件同口径）：同一家冷着也按它自己的键占位，免得一阵风过后顺序乱跳。
    """
    cooled = cooling or set()
    resolved = resolve_resets(routes, report)

    def bucket(item: tuple[int, Route]) -> tuple[int, float, int]:
        index, route = item
        reset = resolved.get(route.provider, {}).get("reset_at_ms")
        if reset is not None:
            return (0, float(reset), index)
        if route.quota is not None or route.reset_at_override_ms is not None:
            return (1, 0.0, index)  # 认识这家额度、但这次没查到 ⇒ 未知档
        return (2, 0.0, index)  # 按量兜底：永远最后

    indexed = list(enumerate(routes))
    # 稳定排序：同桶同键时按配置位置（插件口径：桶内保持配置顺序）
    indexed.sort(key=bucket)
    out: list[OrderedRoute] = []
    for _, route in indexed:
        key = bucket((0, route))
        reason = ORDER_REASONS[0] if key[0] == 0 else ORDER_REASONS[1] if key[0] == 1 else ORDER_REASONS[2]
        out.append(
            OrderedRoute(
                route=route,
                reason=reason,
                cooling=route.provider in cooled,
                reset_at_ms=resolved.get(route.provider, {}).get("reset_at_ms"),
            )
        )
    return out


def resolve_resets(routes: tuple[Route, ...], report: Any = None) -> dict[str, dict[str, Any]]:
    """每一跳的「月度重置时刻 / 是否订阅 / 为什么」——探测结果优先，手动覆盖其次。

    返回 ``{provider: {reset_at_ms, subscription, kind, note}}``；``kind`` 是
    ``monthly-reset-asc`` / ``unknown`` / ``never-expires`` 三态（插件 ``reasonOf`` 同口径）。
    """
    out: dict[str, dict[str, Any]] = {}
    for route in routes:
        observed = report.reset_of(route.provider) if report is not None else None
        reset = route.reset_at_override_ms
        kind = "never-expires" if route.quota is None else "unknown"
        if observed is not None:
            reset = observed if reset is None else reset
        if reset is not None:
            kind = "monthly-reset-asc"
        elif route.quota is not None:
            kind = "unknown"
        subscription = route.subscription_override
        if subscription is None:
            subscription = kind == "monthly-reset-asc"
        out[route.provider] = {"reset_at_ms": reset, "subscription": subscription, "kind": kind}
    return out


# ------------------------------------------------------------------ 密钥解析 --
def _read_dsh_ref(name: str, path: Path) -> str:
    """从 DSH 凭据文件的 ``refs.<名字>`` 取值；读不到返回空串（**不抛**：只是这一跳没密钥）。"""
    if not path.is_file():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    refs = data.get("refs") if isinstance(data, dict) else None
    if not isinstance(refs, dict):
        return ""
    value = refs.get(name)
    return value.strip() if isinstance(value, str) else ""


def resolve_api_key(route: Route, *, env: dict[str, str] | None = None) -> tuple[str, str | None]:
    """这一跳的密钥 → ``(密钥, 来源)``；没有就 ``("", None)``。

    优先级：环境变量 ``<api_key_env>`` > ``~/.dsh/.credentials.yaml`` 的 ``refs.<api_key_env>``。
    来源串只写**名字**（``env:CMD_API_KEY`` / ``file:CMD_API_KEY``），从不写值。
    """
    if not route.api_key_env:
        return "", None
    lookup = os.environ if env is None else env
    from_env = (lookup.get(route.api_key_env) or "").strip()
    if from_env:
        return from_env, f"env:{route.api_key_env}"
    from_file = _read_dsh_ref(route.api_key_env, default_credentials_path())
    if from_file:
        return from_file, f"file:{route.api_key_env}"
    return "", None


def load_routes_file(path: Path | str) -> tuple[Route, ...]:
    """读一份 YAML 链片段（``config.routes.yaml`` 的格式）→ ``Route`` 元组。"""
    from .errors import InputError

    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"链文件读不了：{source}（{exc.strerror or exc}）") from exc
    except yaml.YAMLError as exc:
        raise InputError(f"链文件不是合法 YAML：{source}（{exc}）") from exc
    if isinstance(data, dict):
        data = data.get("routes")
    return parse_routes(data)


# ------------------------------------------------------------------ 冷却表 --
@dataclass(frozen=True)
class CooldownEntry:
    """一条冷却记录（键 = provider）。"""

    provider: str
    until_ms: float
    reason: str


class CooldownTable:
    """冷却表（按 provider 记账，到期自动解除）。时钟可注入，测试钉得死。

    给了 ``persist_path`` 就把「剩余多少毫秒」存成 JSON（清单照插件 0.11 的 ``storages/
    llm_auto_cooldown.json``：键 = provider）。重启后 ``load()`` 一读就能接着冷，不用再白撞一次。
    盘上只记**相对时长**、不记绝对时刻：单调时钟重启归零，绝对时刻没有跨进程意义。
    读写失败一律只告警（``persist_error`` 记最后一个原因），**不影响路由**。
    """

    def __init__(self, *, clock: Any = None, persist_path: Path | str | None = None) -> None:
        self._clock = clock
        self._entries: dict[str, CooldownEntry] = {}
        self._persist_path = Path(persist_path) if persist_path is not None else None
        #: 最近一次落盘/读盘失败的原因（``None`` = 一切正常；排查用，不参与任何判断）
        self.persist_error: str | None = None

    def _now(self) -> float:
        """当前时刻（毫秒，**浮点**）：截断成整数会让「刚记的时间戳」与下一次读数相等时差出 1ms，
        冷却到期判定就提前放行了。"""
        import time

        return (self._clock or (lambda: time.monotonic() * 1000))()

    def mark(self, provider: str, duration_ms: int, reason: str) -> CooldownEntry:
        """给某家记一条冷却；已有更晚的截止时刻就保留它（只延长、不缩短）。记完即落盘。"""
        until = self._now() + max(0.0, float(duration_ms))
        current = self._entries.get(provider)
        if current is not None and current.until_ms >= until:
            return current
        self._entries[provider] = CooldownEntry(provider=provider, until_ms=until, reason=reason)
        self.save()
        return self._entries[provider]

    def active(self) -> dict[str, CooldownEntry]:
        """当前仍在冷却的记录（顺手清掉到期的）。"""
        now = self._now()
        for provider in [key for key, entry in self._entries.items() if entry.until_ms <= now]:
            del self._entries[provider]
        return dict(self._entries)

    def remaining_ms(self, provider: str) -> int:
        entry = self.active().get(provider)
        return 0 if entry is None else max(0, int(entry.until_ms - self._now()))

    def clear(self, provider: str | None = None) -> None:
        """解除一家（``None`` = 全解）。"""
        if provider is None:
            self._entries.clear()
        else:
            self._entries.pop(provider, None)
        self.save()

    # ------------------------------------------------------------ 落盘 --
    def records(self) -> dict[str, dict[str, Any]]:
        """可落盘的那份账：``{provider: {remaining_ms, reason}}``（到期的先剔掉）。"""
        now = self._now()
        return {
            key: {"remaining_ms": int(round(entry.until_ms - now)), "reason": entry.reason}
            for key, entry in self._entries.items()
            if entry.until_ms > now
        }

    def save(self) -> None:
        """把剩余冷却写到 ``persist_path``；没配路径 / 写失败都只告警，不抛。"""
        if self._persist_path is None:
            return
        payload = {"version": 1, "cooldowns": self.records()}
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._persist_path)
        except OSError as exc:
            self.persist_error = f"冷却表写入失败：{type(exc).__name__}（{exc}）"
            return
        self.persist_error = None

    def load(self) -> int:
        """从 ``persist_path`` 读回剩余冷却；返回恢复的条数（坏文件 / 读不到 = 0 条，不抛）。"""
        if self._persist_path is None or not self._persist_path.is_file():
            return 0
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.persist_error = f"冷却表读取失败：{type(exc).__name__}（{exc}）"
            return 0
        items = data.get("cooldowns") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            self.persist_error = "冷却表读取失败：介质里没有 cooldowns 对象"
            return 0
        now = self._now()
        restored = 0
        for provider, item in items.items():
            if not isinstance(item, dict):
                continue
            remaining = item.get("remaining_ms")
            reason = item.get("reason")
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining <= 0:
                continue
            self._entries[str(provider)] = CooldownEntry(
                provider=str(provider),
                until_ms=now + float(remaining),
                reason=reason if isinstance(reason, str) and reason else "UNKNOWN",
            )
            restored += 1
        self.persist_error = None
        self.save()  # 顺手把过期 / 坏条目从介质里清掉
        return restored
