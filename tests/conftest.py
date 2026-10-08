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

# 前端无头用例在 tests/web（由另一路并行开发）：默认 `pytest tests` 不收集
collect_ignore = ["web"]

# 契约里的主样例材料（5,341 字符 / 22 段）；不存在时相关用例 skip
REAL_SAMPLE = Path(r"C:\Users\MLTZ\Desktop\code\working\学习\_第1章第1节三版对照\00-底本.md")

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


@dataclass
class FakeModelClient:
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
class FakeChatClient:
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
