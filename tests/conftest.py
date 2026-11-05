"""共享夹具与工具。

- 临时数据目录 / 画像目录 / 前端目录：所有用例都在 ``tmp_path`` 里跑，不碰项目真实产物；
- 假模型客户端：默认不发真实网络请求（契约的密钥与网络纪律）；
- ``collect_ignore``：``tests/web`` 归前端（W2），默认用 ``pytest tests`` 时不收集它。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 让 `pytest`（不经 uv 安装）也能直接导入 src 布局
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ar.llm import LlmRequest, LlmResult, RetryPolicy  # noqa: E402 - 必须在 sys.path 注入之后

# 前端无头用例在 tests/web（由另一路并行开发）：默认 `pytest tests` 不收集
collect_ignore = ["web"]

# 契约里的主样例材料（5,341 字符 / 22 段）；不存在时相关用例 skip
REAL_SAMPLE = Path.home() / "Desktop" / "code" / "working" / "学习" / "_第1章第1节三版对照" / "00-底本.md"

SAMPLE_DOC = """# 样例标题

> 出处说明：这份文档头应当被切分规则跳过。

---

第一段：带  连续空白与全角空格\u3000的段落。

第二段。

第三段。
"""


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    """隔离的数据 / 画像 / 前端目录。"""
    data_dir = tmp_path / "data"
    profiles_dir = tmp_path / "profiles"
    web_dir = tmp_path / "web"
    data_dir.mkdir()
    profiles_dir.mkdir()
    return SimpleNamespace(tmp=tmp_path, data=data_dir, profiles=profiles_dir, web=web_dir)


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    path = tmp_path / "sample.md"
    path.write_text(SAMPLE_DOC, encoding="utf-8")
    return path


@pytest.fixture
def real_sample() -> Path:
    if not REAL_SAMPLE.is_file():
        pytest.skip(f"样例材料不存在：{REAL_SAMPLE}")
    return REAL_SAMPLE


@dataclass
class FakeModelReply:
    """替身回复对象（字段与 ``ar.model.ModelReply`` 对齐）。"""

    text: str
    model: str = "fake-model"
    finish_reason: str = "stop"
    usage: dict[str, Any] | None = None
    degraded_params: tuple[str, ...] = ()


def reply_to_result(reply: Any) -> LlmResult:
    """假回复 → 统一调用层的 ``LlmResult``（几个假客户端的 ``arequest_once`` 共用这一段）。"""
    usage = getattr(reply, "usage", None)
    return LlmResult(
        ok=True,
        text=getattr(reply, "text", "") or "",
        model=getattr(reply, "model", None),
        finish_reason=getattr(reply, "finish_reason", None),
        usage=dict(usage) if isinstance(usage, dict) else None,
        degraded_params=tuple(getattr(reply, "degraded_params", ()) or ()),
    )


class FakeOnceMixin:
    """把「只实现了同步 ``chat``」的假客户端接到统一调用层要的 ``arequest_once`` 上。

    四个业务模块迁移后都走 ``ar.tools`` → ``ar.llm``：那一层只认
    ``arequest_once`` / ``retry_policy`` / ``configured`` 三个成员。这里补上前两个，
    既有写法（假模型只管 ``chat`` 回什么、``calls`` 记账）一个都不用改。

    ``chat`` 抛出的异常**照旧往外抛**：``ar.llm`` 会把它归一成 ``UNKNOWN`` 失败
    （与迁移前「假模型抛错 = 调用失败」同一条降级路径）。

    默认策略是**不重试**：用例不必等退避，也不会因为一次失败多打几次账；
    要考重试的用例自己构造带 ``policy`` 的假客户端。
    """

    def retry_policy(self) -> RetryPolicy:
        policy = getattr(self, "policy", None)
        return policy if isinstance(policy, RetryPolicy) else RetryPolicy(max_retries=0)

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        reply = self.chat([dict(message) for message in request.messages], max_tokens=request.max_tokens)
        return reply_to_result(reply)


@dataclass
class FakeModelClient(FakeOnceMixin):
    """假模型客户端：同步 ``chat`` + 异步 ``achat``，记录收到的消息。

    ``error`` 非空时每次调用都抛它，用来验证「调用失败 → 可见降级」。
    """

    text: str = ""
    model: str = "fake-model"
    error: Exception | None = None
    configured: bool = True
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    def _reply(self) -> FakeModelReply:
        return FakeModelReply(text=self.text, model=self.model)

    def chat(self, messages: Any, *, temperature: float | None = None, max_tokens: Any = None) -> FakeModelReply:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self._reply()

    async def achat(
        self, messages: Any, *, temperature: float | None = None, max_tokens: Any = None
    ) -> FakeModelReply:
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)


@pytest.fixture
def fake_reply() -> type[FakeModelReply]:
    return FakeModelReply


@dataclass
class FakeChatClient(FakeOnceMixin):
    """可编程的假模型：``on_chat(messages)`` 决定这批请求回什么（或抛错）。

    ``run_rewrite`` 会**并发**发多批请求，所以假客户端必须自己认得出「这一批问的是哪几段」
    （默认实现就是从提示词里抓 ``[pNNN]``），而不是靠调用顺序。
    """

    handler: Any = None
    model: str = "fake-model"
    usage: dict[str, Any] | None = None
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    def _reply(self, messages: Any) -> FakeModelReply:
        self.calls.append(list(messages))
        if self.handler is None:
            return FakeModelReply(text="", model=self.model, usage=self.usage)
        result = self.handler(list(messages))
        if isinstance(result, FakeModelReply):
            return result
        return FakeModelReply(
            text=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
            model=self.model,
            usage=self.usage,
        )

    def chat(self, messages: Any, **kwargs: Any) -> FakeModelReply:
        return self._reply(messages)

    async def achat(self, messages: Any, **kwargs: Any) -> FakeModelReply:
        return self._reply(messages)


@pytest.fixture
def fake_client() -> type[FakeModelClient]:
    return FakeModelClient


@pytest.fixture
def fake_chat_client() -> type[FakeChatClient]:
    return FakeChatClient


# ⚠ `run_cli` 是**进程内**直调 `ar.cli.main()`：不传 `--config` 时 `load_config()` 走的是
# `paths.DEFAULT_CONFIG_PATH`，也就是**开发机真实的** `config.local.json`。
# 凡是要断言「模型未配置」（退出码 5 / 启动横幅写「未配置」）的用例，必须显式传一份空配置
# （用下面的 `nokey_config`），否则开发机一配钥匙，用例就从绿转红——2026-10-09 实测踩到：
# 本机 `config.local.json` 补上 `api_key` 后，3 条用例当场变红（详见 README §7）。
@pytest.fixture
def run_cli(capsys: pytest.CaptureFixture[str]):
    """跑一次 CLI 并拿到 (退出码, 合并输出)；打印流同时被捕获。"""

    def _run(*argv: str) -> tuple[int, str]:
        from ar.cli import main

        try:
            code = main(list(argv))
        except SystemExit as exc:  # argparse 的 --help / --version / 用法错误
            code = int(exc.code or 0)
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return _run


@pytest.fixture
def nokey_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一份**不含密钥**的临时配置，给 CLI 用例显式 `--config` 用。

    两件事一起做，少一件就仍然与开发机有关：

    1. 配置文件里 `api_key: ""`（其余键由 `load_config()` 用默认值补齐）；
    2. 把环境变量 `AR_API_KEY` 摘掉——它的优先级**高于**配置文件，
       开发机若设过它，光写空配置照样是「模型已配置」。

    于是「模型未配置 → 退出码 5」「启动横幅写未配置」这类断言只取决于用例自己
    （与 `test_rewrite.py` 的 `rewrite_env` 同一口径，那边填的是假 key）。
    """
    path = tmp_path / "config.nokey.json"
    path.write_text(json.dumps({"api_key": ""}), encoding="utf-8")
    #
    # 3. ``AR_ROUTES_FILE`` 指到一份**不存在的路径**：配置文件里没写 ``routes`` 时 ``load_config()``
    #    会回落读项目根的 ``config.routes.yaml``（开发机用 ``ar routes import`` 生成过就有），
    #    链模式**无视密钥直接发请求**——不挡住，这些用例在开发机上必红（2026-10-26 实测）。
    from ar.config import API_KEY_ENV

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setenv("AR_ROUTES_FILE", str(tmp_path / "no-such-routes.yaml"))
    return path
