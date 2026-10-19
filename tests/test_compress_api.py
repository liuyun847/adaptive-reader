"""``POST /api/compress``：读者点「太啰嗦」当场压缩该段（PLAN-actions-feedback §5.2）。

覆盖：成功写回三份产物 + ``artifacts_updated`` / ``hint``；缓存命中不再调模型；单段事实锚不一致
时**不写盘、正文不变**（降级文案点名对不上的锚点）；七种降级 ``reason`` 都走 200 + ``ok:false``
（材料 / 段不存在也在这一路）；该次点击顺带写一条 ``kind="compress"`` 的行为事件。

**这一段有没有事实锚要分得开**（2026-10-13 复核修复 F1）：三类锚点合计为 0 时校验是**空跑**，
不拒绝写入，但成功响应的 ``degraded`` 里必须留一条可读说明（缓存命中时也带着它）；
有锚点段的成功响应里**不许**出现这条说明。

**零真实模型调用**：全部用假客户端（注入固定响应），不打真网关、不设 ``AR_E2E_BILLABLE``。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ar.cache import ExplainCache
from ar.config import Config
from ar.fidelity import facts_of, split_rewritten_blocks
from ar.llm import LlmResult
from ar.rewrite import COMPRESS_NO_ANCHOR_NOTE
from ar.server import build_app
from test_rewrite import FAKE_QUOTE, fake_handler_for, fake_run, write_fake_doc

FAKE_KEY = "sk-" + "5" * 40
READER = "local-default"
SOURCE_ID = "fake-doc"

#: 压缩稿：比 p006 的当前正文（上一次的压缩稿）短、不含任何事实锚（数字 / 引文 / 书名号）
COMPRESSED = "只留骨架：同一个意思。"
#: p005 的压缩稿：更短，但**丢了**原文里的 1696 / 27 两个数字（事实锚不一致的现场）
P005_LOSSY = "马克思在 1859 年写的《政治经济学批判》里，开头就是“庞大的商品堆积”。"
#: p005 的压缩稿：更短、三类事实锚逐字照抄且顺序不变（有锚点段的**正常校验**现场）
P005_KEEPING = (
    "马克思在 1859 年写的《政治经济学批判》里，开头就是“庞大的商品堆积”；另有 1696 年与页码 27。"
)

#: 成功响应**逐键**（前端按同一份契约写，加减键都要在这里说清楚）
SUCCESS_KEYS = {
    "ok",
    "paragraph",
    "action",
    "text",
    "scope",
    "cached",
    "usage",
    "attempts",
    "model",
    "prompt_version",
    "degraded",
    "artifacts_updated",
    "hint",
    # 事实锚申报的可见面（§14.1，2026-10-14 收尾）：本次申报了什么 / 放行了哪几项 /
    # 有没有「多申报」/ 本次采用哪一档口径
    "fact_declarations",
    "waived",
    "over_declared",
    "fact_anchor_policy",
}
DEGRADED_KEYS = {"ok", "reason", "text", "degraded"}


class _CompressClient:
    """假客户端：认出压缩提示词就回压缩稿，否则回那批段的假决策（与 ``FakeChatClient`` 同形）。"""

    configured = True

    def __init__(
        self,
        *,
        compressed: str = COMPRESSED,
        batch_handler: Any = None,
        usage: dict | None = None,
        declarations: dict | None = None,
    ) -> None:
        self.compressed = compressed
        self.batch_handler = batch_handler
        self.declarations = declarations
        self.usage = usage or {
            "requests": 1,
            "prompt_tokens": 300,
            "completion_tokens": 60,
            "total_tokens": 360,
            "prompt_tokens_details": {"cached_tokens": 128},
        }
        self.calls: list[list[dict[str, str]]] = []

    def retry_policy(self):  # noqa: ANN201 - 假客户端：不重试（用例不必等退避）
        from ar.llm import RetryPolicy

        return RetryPolicy(max_retries=0)

    async def arequest_once(self, request: Any) -> LlmResult:
        messages = [dict(message) for message in request.messages]
        self.calls.append(messages)
        prompt = messages[-1]["content"]
        if "压缩要求" in prompt:
            payload: dict[str, Any] = {
                "action": "compress",
                "text": self.compressed,
                "why_note": "删掉了重复的三遍",
            }
            if self.declarations is not None:
                # 申报通道（compress-v3，§14.1）：模型逐条申报本次压缩放弃了哪几项事实锚。
                # 非对象形状**原样灌进响应**，用来守「申报读不通不许静默」那一条用例
                # （工具层只做「是不是对象」的粗筛，出参 schema 说的仍是对象）。
                payload["fact_declarations"] = (
                    dict(self.declarations) if isinstance(self.declarations, dict) else self.declarations
                )
            text = json.dumps(payload, ensure_ascii=False)
            return LlmResult(ok=True, text=text, model="fake-model", usage=dict(self.usage))
        if self.batch_handler is None:
            return LlmResult(ok=True, text="{}", model="fake-model", usage=dict(self.usage))
        reply = self.batch_handler(messages)
        return LlmResult(ok=True, text=reply, model="fake-model", usage=dict(self.usage))

    #: 用例用它数「压缩到底调了几次模型」
    @property
    def compress_calls(self) -> list[list[dict[str, str]]]:
        return [call for call in self.calls if "压缩要求" in call[-1]["content"]]


class _ExplodingClient:
    configured = True

    def retry_policy(self):  # noqa: ANN201
        from ar.llm import RetryPolicy

        return RetryPolicy(max_retries=0)

    async def arequest_once(self, request: Any) -> LlmResult:  # noqa: ANN401
        raise RuntimeError("网关不可用（回归用例假模型）")


class _GarbageClient:
    """模型答了，但输出不成结构（``bad_reply`` 的现场）。"""

    configured = True

    def retry_policy(self):  # noqa: ANN201
        from ar.llm import RetryPolicy

        return RetryPolicy(max_retries=0)

    async def arequest_once(self, request: Any) -> LlmResult:  # noqa: ANN401
        return LlmResult(ok=True, text="这句话不是 JSON", model="fake-model")


def make_client(
    dirs: SimpleNamespace,
    *,
    client_factory: Any = None,
    cache: ExplainCache | None = None,
    api_key: str = FAKE_KEY,
) -> TestClient:
    config = Config(
        base_url="https://gateway.invalid/provider/v1",
        model="fake/model",
        api_key=api_key,
        api_key_origin="env:AR_API_KEY" if api_key else None,
    )
    app = build_app(
        data_dir=dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=dirs.web,
        config=config,
        reader=READER,
        client_factory=client_factory,
        explain_cache=cache,
    )
    return TestClient(app)


def compress_body(**overrides: Any) -> dict:
    body = {"source_id": SOURCE_ID, "paragraph": "p006", "reason": "too_verbose"}
    body.update(overrides)
    return body


@pytest.fixture
def rewritten(dirs: SimpleNamespace, fake_chat_client) -> SimpleNamespace:
    """跑一次整篇改写（假模型）→ 三份产物齐备的现场。"""
    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler_for(doc))
    fake_run(dirs, client)
    return SimpleNamespace(dirs=dirs, doc=doc, target=dirs.data / SOURCE_ID, rewrite_client=client)


def _artifacts(target) -> dict[str, bytes]:  # noqa: ANN001
    return {name: (target / name).read_bytes() for name in ("decisions.json", "changes.json", "rewritten.md")}


def _payload(target, name: str) -> dict:  # noqa: ANN001
    return json.loads((target / name).read_text(encoding="utf-8"))


def _blocks(target) -> list[str]:  # noqa: ANN001
    return split_rewritten_blocks((target / "rewritten.md").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 参数校验 --
def test_compress_route_sits_before_the_api_fallback(rewritten: SimpleNamespace) -> None:
    """路由顺序守门（契约 §5.2）：新端点必须排在 ``/api/{rest:path}`` 兜底之前。"""
    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient())
    response = client.get("/api/compress")
    assert response.status_code == 404
    assert response.json()["error"].startswith("接口不存在"), response.json()
    # 兜底路由认得这个路径（说明匹配到的确实是兜底，而不是静态托管的 404）
    assert "/api/compress" in response.json()["error"]


def test_missing_source_id_or_paragraph_is_a_400(rewritten: SimpleNamespace) -> None:
    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient())
    assert client.post("/api/compress", json={"paragraph": "p006"}).status_code == 400
    assert client.post("/api/compress", json={"source_id": SOURCE_ID}).status_code == 400
    assert client.post("/api/compress", json={"source_id": SOURCE_ID, "paragraph": "  "}).status_code == 400


# -------------------------------------------------------------------- 成功 --
def test_success_writes_back_all_three_artifacts(rewritten: SimpleNamespace) -> None:
    fake = _CompressClient()
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    before_blocks = _blocks(rewritten.target)

    response = client.post("/api/compress", json=compress_body())
    assert response.status_code == 200
    payload = response.json()

    # ① 响应形状逐键（前端按同一份契约写）
    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True
    assert payload["paragraph"] == "p006"
    assert payload["action"] == "compress" and payload["scope"] == "paragraph"
    assert payload["text"] == COMPRESSED
    assert payload["cached"] is False and payload["artifacts_updated"] is True
    assert payload["model"] == "fake-model" and payload["prompt_version"] == "compress-v3"
    # p006 的原文里三类事实锚合计为 0 ⇒ 校验空跑，成功响应必须说出来（F1，见下一条用例）
    assert payload["degraded"] == [COMPRESS_NO_ANCHOR_NOTE], payload["degraded"]
    assert "ar check" in payload["hint"], "hint 里要有「请重跑 ar check」"
    assert payload["usage"]["prompt_tokens"] == 300 and payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 128
    assert [item["try"] for item in payload["attempts"]] == [1]

    # ② 三份产物都写回了：正文、决策、变更记录
    blocks = _blocks(rewritten.target)
    assert blocks[order.index("p006")] == COMPRESSED
    for pid in ("p001", "p002", "p003", "p004", "p005", "p007"):
        assert blocks[order.index(pid)] == before_blocks[order.index(pid)], pid
    decisions = _payload(rewritten.target, "decisions.json")
    assert [item["paragraph"] for item in decisions["decisions"]] == order
    assert decisions["decisions"][order.index("p006")]["action"] == "compress"
    changes = _payload(rewritten.target, "changes.json")
    change = changes["changes"][order.index("p006")]
    assert change["action"] == "compress"
    assert "太啰嗦" in change["why"]["note"]
    assert "figure" not in change, "压缩后正文变了，原来那张图不再配套"
    # ③ fidelity 重置为未校验 + hint 提示重跑 ar check
    assert changes["fidelity"]["checked"] is False and changes["fidelity"]["counts"] is None
    assert "ar check" in changes["fidelity"]["summary"]
    # ④ 该次点击写了一条 kind="compress" 的行为事件（ok=true）
    events = (rewritten.dirs.profiles / READER / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    event = json.loads(events[0])
    assert event["kind"] == "compress" and event["ok"] is True and event["paragraph"] == "p006"
    assert event["reader"] == READER and event["source_id"] == SOURCE_ID
    assert len(fake.compress_calls) == 1


def test_cache_hit_does_not_call_the_model_again(rewritten: SimpleNamespace) -> None:
    """缓存键含画像指纹（§1 第 14 条）：重复点同一段命中缓存、不再发请求、也不写盘。"""
    fake = _CompressClient()
    cache = ExplainCache()
    client = make_client(rewritten.dirs, client_factory=lambda: fake, cache=cache)

    first = client.post("/api/compress", json=compress_body()).json()
    assert first["cached"] is False and first["artifacts_updated"] is True
    after_first = _artifacts(rewritten.target)

    second = client.post("/api/compress", json=compress_body()).json()
    assert set(second) == SUCCESS_KEYS
    assert second["ok"] is True and second["cached"] is True
    assert second["text"] == COMPRESSED
    assert second["usage"] == {} and second["attempts"] == []
    assert second["artifacts_updated"] is False, "命中缓存这一次没有写盘"
    # 空跑说明跟着缓存一起回（p006 没有事实锚）：不能因为命中缓存就把它丢掉
    assert second["degraded"] == [COMPRESS_NO_ANCHOR_NOTE]
    assert len(fake.compress_calls) == 1, "缓存命中却又打了一次模型"
    assert _artifacts(rewritten.target) == after_first, "缓存命中不该动产物"


def test_cache_hit_entry_itself_carries_the_empty_anchor_note(rewritten: SimpleNamespace) -> None:
    """**G3**（2026-10-13 收尾复核）：零事实锚那一段命中缓存时，那条「校验空跑」说明跟着回。

    与上一条的区别在**独立性**：两次请求走**两个不同的 app**（各自新建），只共用同一个
    ``ExplainCache`` 实例——所以这条说明只可能来自**缓存条目本身**，不是同一个 app 里别处的状态、
    也不是「碰巧没被清掉」。另外直接查一眼缓存条目，把「实现到底存没存它」钉死在数据结构上
    （只断言响应的话，将来若改成命中时重算，用例会照样绿而路径已经不是缓存带来的了）。
    """
    from ar.cache import compress_cache_key

    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    source_text = str(rewritten.doc["paragraphs"][order.index("p006")]["text"])
    facts = facts_of(source_text)
    assert (facts.numbers, facts.quotes, facts.books) == ([], [], []), "这条用例需要「原文一个事实锚都没有」的现场"

    fake = _CompressClient()
    cache = ExplainCache()
    first_client = make_client(rewritten.dirs, client_factory=lambda: fake, cache=cache)
    second_client = make_client(rewritten.dirs, client_factory=lambda: fake, cache=cache)

    first = first_client.post("/api/compress", json=compress_body()).json()
    assert first["ok"] is True and first["cached"] is False
    assert first["degraded"] == [COMPRESS_NO_ANCHOR_NOTE]
    after_first = _artifacts(rewritten.target)

    # ① 缓存条目里确实存着这条说明（命中路径唯一的信息来源）
    entry = cache.get(
        compress_cache_key(
            source_id=SOURCE_ID, paragraph_id="p006", profiles_dir=rewritten.dirs.profiles, reader=READER
        )
    )
    assert entry is not None, "成功结果没进缓存"
    assert entry["degraded"] == [COMPRESS_NO_ANCHOR_NOTE], "缓存条目里没带上这条说明"

    # ② 另一个 app 上再点一次同一段：命中缓存，说明照旧、这一次没写盘
    second = second_client.post("/api/compress", json=compress_body()).json()
    assert set(second) == SUCCESS_KEYS
    assert second["ok"] is True and second["cached"] is True
    assert second["artifacts_updated"] is False, "命中缓存这一次没有写盘"
    assert second["degraded"] == [COMPRESS_NO_ANCHOR_NOTE], "命中缓存把空跑说明丢了"
    assert "没有可比对的事实锚" in second["degraded"][0]
    assert "未经事实锚校验" in second["degraded"][0]
    assert second["text"] == COMPRESSED
    assert second["usage"] == {} and second["attempts"] == []
    assert len(fake.compress_calls) == 1, "缓存命中却又打了一次模型"
    assert _artifacts(rewritten.target) == after_first, "命中缓存不该动产物"


# -------------------------------------------------------------------- 降级 --
# ------------------------------------------------ 事实锚：空跑可见 / 真跑照旧 --
def test_paragraph_without_fact_anchors_says_the_check_was_empty(rewritten: SimpleNamespace) -> None:
    """**F1**：这一段三类事实锚合计为 0 时校验空跑 —— 不拒绝写入，但成功响应必须说出来。

    现场固定：p006 的原文里一个数字 / 引文 / 书名号都没有（否则这条用例测的不是空跑）。
    没有这条说明时，``ok:true`` + ``degraded:[]`` 会让人以为「事实锚查过了、没问题」，
    而实际上是任何更短的文本都能过关（口径见 ``src/ar/fidelity.py`` 顶部那两条推论）。
    """
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    source_text = str(rewritten.doc["paragraphs"][order.index("p006")]["text"])
    facts = facts_of(source_text)
    assert (facts.numbers, facts.quotes, facts.books) == ([], [], []), "这条用例需要「原文一个事实锚都没有」的现场"

    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient())
    payload = client.post("/api/compress", json=compress_body()).json()

    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True, "没有事实可毁的段落压缩本来无害：不许因此拒绝"
    assert payload["degraded"] == [COMPRESS_NO_ANCHOR_NOTE]
    assert "没有可比对的事实锚" in payload["degraded"][0]
    assert "未经事实锚校验" in payload["degraded"][0]
    # 产物照常写盘（三段都写回了这一段）
    assert _blocks(rewritten.target)[order.index("p006")] == COMPRESSED
    changes = _payload(rewritten.target, "changes.json")
    assert changes["changes"][order.index("p006")]["action"] == "compress"
    assert changes["fidelity"]["checked"] is False and "ar check" in changes["fidelity"]["summary"]
    # 行为事件照旧记下（空跑说明不影响记账）
    event = json.loads(
        (rewritten.dirs.profiles / READER / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["kind"] == "compress" and event["ok"] is True


def test_anchor_bearing_paragraph_is_actually_checked(rewritten: SimpleNamespace) -> None:
    """有事实锚的段：正常校验（真跑过，成功响应里**没有**那条空跑说明），不一致时仍然拒写。"""
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    source_text = str(rewritten.doc["paragraphs"][order.index("p005")]["text"])
    facts = facts_of(source_text)
    assert facts.numbers and facts.quotes and facts.books, "这条用例需要「三类锚点都有」的现场"
    before = _artifacts(rewritten.target)

    # ① 锚点逐字照抄 → 校验真跑过、过了 ⇒ ok:true + degraded 里没有空跑说明
    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient(compressed=P005_KEEPING))
    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()
    assert payload["ok"] is True, payload
    assert payload["degraded"] == [], "有锚点的段不该报「校验空跑」"
    assert _blocks(rewritten.target)[order.index("p005")] == P005_KEEPING

    # ② 锚点被改坏（少 1696 / 27）→ 仍然拒写、正文与产物一个字节不动
    after_keeping = _artifacts(rewritten.target)
    client2 = make_client(rewritten.dirs, client_factory=lambda: _CompressClient(compressed=P005_LOSSY))
    payload = client2.post("/api/compress", json=compress_body(paragraph="p005")).json()
    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "1696" in payload["text"] or "27" in payload["text"]
    assert payload["degraded"], "降级必须可见"
    assert COMPRESS_NO_ANCHOR_NOTE not in payload["degraded"], "这不是空跑，别报成空跑"
    assert _artifacts(rewritten.target) == after_keeping, "事实锚不过却写了盘"
    assert _artifacts(rewritten.target) != before


def test_fact_anchor_mismatch_does_not_write_and_names_the_anchors(rewritten: SimpleNamespace) -> None:
    """压缩稿改坏了事实锚 → 不写盘、正文一个字节不变、降级文案点名对不上的锚点。"""
    fake = _CompressClient(compressed=P005_LOSSY)
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    before = _artifacts(rewritten.target)

    response = client.post("/api/compress", json=compress_body(paragraph="p005"))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "1696" in payload["text"] or "27" in payload["text"], payload["text"]
    assert payload["degraded"], "降级必须可见"
    assert _artifacts(rewritten.target) == before, "事实锚不过却写了盘"
    assert _blocks(rewritten.target)[order.index("p005")] == split_rewritten_blocks(
        before["rewritten.md"].decode("utf-8")
    )[order.index("p005")]
    # 事件照样记下来（ok=false + reason），前端与画像折算都靠它
    event = json.loads(
        (rewritten.dirs.profiles / READER / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["kind"] == "compress" and event["ok"] is False
    assert event["reason"] == "fact_anchor_mismatch"


def test_no_artifacts_when_the_material_or_the_rewrite_is_missing(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    fake = _CompressClient()
    client = make_client(dirs, client_factory=lambda: fake)
    # ① 材料根本没摄取
    response = client.post("/api/compress", json=compress_body())
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "no_artifacts"
    assert "ingest" in payload["text"]

    # ② 材料在，但三份改写产物还没有（先整篇 rewrite）
    write_fake_doc(dirs)
    response = client.post("/api/compress", json=compress_body())
    payload = response.json()
    assert response.status_code == 200
    assert payload["reason"] == "no_artifacts" and "ar rewrite" in payload["text"]
    assert fake.compress_calls == [], "产物不全却调了模型"


def test_paragraph_not_found_is_200_with_ok_false(rewritten: SimpleNamespace) -> None:
    fake = _CompressClient()
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    response = client.post("/api/compress", json=compress_body(paragraph="p999"))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "paragraph_not_found"
    assert "p999" in payload["text"]
    assert fake.compress_calls == [], "段不存在不该打模型"


def test_model_not_configured_is_a_visible_degradation(rewritten: SimpleNamespace) -> None:
    class _Unconfigured:
        configured = False

    client = make_client(rewritten.dirs, client_factory=lambda: _Unconfigured())
    before = _artifacts(rewritten.target)
    response = client.post("/api/compress", json=compress_body())
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["reason"] == "model_not_configured"
    assert "AR_API_KEY" in payload["text"]
    assert _artifacts(rewritten.target) == before


def test_model_error_and_bad_reply_are_distinguished(rewritten: SimpleNamespace) -> None:
    before = _artifacts(rewritten.target)
    boom = make_client(rewritten.dirs, client_factory=_ExplodingClient)
    payload = boom.post("/api/compress", json=compress_body()).json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "model_error"
    assert "失败" in payload["text"]

    garbage = make_client(rewritten.dirs, client_factory=_GarbageClient)
    payload = garbage.post("/api/compress", json=compress_body()).json()
    assert payload["ok"] is False and payload["reason"] == "bad_reply"
    assert _artifacts(rewritten.target) == before, "降级路径一律不许写盘"


# ------------------------------------------------- 事实锚申报（§14.1，compress-v2） --
#: p005 有锚点（数字 1859 / 1696 / 27、引文、书名）；这一稿只丢了两个数字
DECLARED_DROP = {"numbers": ["1696", "27"], "note": "举例数据只为给出定性结论"}


def write_profile_with_policy(dirs: SimpleNamespace, policy: str = "allow_declared_drops") -> None:
    """给这次 app 的读者写一份画像，事实锚口径设成 ``policy``（唯一创建路径：``set_preference``）。"""
    from ar.profile import default_profile, save_profile, set_preference

    profile = set_preference(default_profile(READER), "pref_fact_anchor", policy)
    save_profile(dirs.profiles, READER, profile)


def test_declared_drop_passes_under_the_loose_policy_and_is_visible(rewritten: SimpleNamespace) -> None:
    """「缺失 + 已申报 + 松口径」→ **放行**，且产物 / 响应 / `ar check` 三处都看得见。"""
    from ar.rewrite import check_fidelity

    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(compressed=P005_LOSSY, declarations=DECLARED_DROP)
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True and payload["artifacts_updated"] is True
    assert payload["fact_anchor_policy"] == "allow_declared_drops"
    assert payload["fact_declarations"] == DECLARED_DROP
    # ① 产物：申报落 changes[i].fact_declarations，正文当场换掉
    changes = _payload(rewritten.target, "changes.json")
    assert changes["changes"][order.index("p005")]["fact_declarations"] == DECLARED_DROP
    assert _blocks(rewritten.target)[order.index("p005")] == P005_LOSSY
    # ② 响应：放行条目与一句可读说明（页面拿它渲染「本段申报」+ 放行徽标 + 横幅）
    assert [item["kind"] for item in payload["waived"]] == ["numbers"]
    assert payload["waived"][0]["missing"] == ["1696", "27"]
    assert payload["waived"][0]["declared"] == ["1696", "27"]
    assert payload["over_declared"] == []
    assert any("放行" in note for note in payload["degraded"]), payload["degraded"]
    assert "放行" in payload["hint"] and "ar check" in payload["hint"]
    # ③ 命令行：重跑 `ar check` 用同一档口径认这一份申报（写盘后 fidelity.waived 有它）
    outcome = check_fidelity(
        SOURCE_ID, rewritten.dirs.data, profiles_dir=rewritten.dirs.profiles, reader=READER
    )
    assert outcome.policy == "allow_declared_drops"
    assert outcome.mismatches == [] and outcome.checked is True
    assert [item["kind"] for item in outcome.waived] == ["numbers"]
    fidelity = _payload(rewritten.target, "changes.json")["fidelity"]
    assert fidelity["checked"] is True and [item["kind"] for item in fidelity["waived"]] == ["numbers"]


def test_undeclared_drop_is_still_refused_under_the_loose_policy(rewritten: SimpleNamespace) -> None:
    """底线不变：**没申报就拒写**——松口径也不放行（这一条是本批的网）。"""
    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(compressed=P005_LOSSY)  # 模型一个字都没申报
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    before = _artifacts(rewritten.target)

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "没申报" in payload["text"]
    assert _artifacts(rewritten.target) == before, "没申报的缺失却写了盘"


def test_a_non_object_declaration_is_visible_on_the_success_path(rewritten: SimpleNamespace) -> None:
    """模型把 ``fact_declarations`` 给成一个字符串：按「没有申报」处理（安全方向不变），**但不静默**。

    这一段（p006）三类事实锚合计为 0，压缩照旧写盘；响应与产物的 ``degraded`` 里都要有那句
    「整条不是对象、按没有申报处理」——不然业务层只看到「没申报」，用户什么都看不到
    （工具层 ``ar.tools._run_compress_paragraph`` 是这条说明的唯一来源）。
    """
    fake = _CompressClient(compressed=COMPRESSED, declarations="数字 1696")
    client = make_client(rewritten.dirs, client_factory=lambda: fake)

    payload = client.post("/api/compress", json=compress_body()).json()

    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True and payload["artifacts_updated"] is True
    assert payload["fact_declarations"] == {} and payload["waived"] == []
    notes = [note for note in payload["degraded"] if "不是对象" in note]
    assert notes and "没有申报" in notes[0], payload["degraded"]
    # 产物也留痕：同一句说明进 ``meta.degraded``；「申报读不通」不许写进 ``changes[i]``
    changes = _payload(rewritten.target, "changes.json")
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    assert "fact_declarations" not in changes["changes"][order.index("p006")]
    assert any("不是对象" in note for note in changes["meta"]["degraded"])


def test_a_non_object_declaration_still_refuses_the_drop_under_the_loose_policy(
    rewritten: SimpleNamespace,
) -> None:
    """安全方向不变：整条申报读不通 = 没有申报 ⇒ p005 的缺失在松口径下**照旧拒写**。"""
    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(compressed=P005_LOSSY, declarations="数字 1696 / 27")
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    before = _artifacts(rewritten.target)

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert _artifacts(rewritten.target) == before, "申报读不通却放行了缺失"
    assert any("不是对象" in note for note in payload["degraded"]), payload["degraded"]


def test_declared_drop_is_refused_under_the_strict_policy(rewritten: SimpleNamespace) -> None:
    """同一份申报在严口径（``keep_all``）下**不放行**，文案里说清为什么。"""
    write_profile_with_policy(rewritten.dirs, "keep_all")
    fake = _CompressClient(compressed=P005_LOSSY, declarations=DECLARED_DROP)
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    before = _artifacts(rewritten.target)

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "keep_all" in payload["text"] and "pref_fact_anchor" in payload["text"], payload["text"]
    assert _artifacts(rewritten.target) == before


def test_declaration_that_does_not_cover_the_drop_is_refused(rewritten: SimpleNamespace) -> None:
    """申报与实测对不上（漏报了一个数字）→ 松口径也不放行。"""
    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(compressed=P005_LOSSY, declarations={"numbers": ["1696"], "note": "只报了一个"})
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    before = _artifacts(rewritten.target)

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "对不上" in payload["text"] and "未申报的缺失不放行" in payload["text"], payload["text"]
    assert _artifacts(rewritten.target) == before


def test_extra_anchor_is_refused_even_with_a_declaration(rewritten: SimpleNamespace) -> None:
    """多出来的锚点（换成别的数字）任何口径都不放行——申报只覆盖「少了什么」。"""
    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(
        compressed="马克思在 1859 年写的《政治经济学批判》里，开头就是“庞大的商品堆积”；另有 1696 年与页码 28。",
        declarations={"numbers": ["27"], "note": "想改个页码"},
    )
    client = make_client(rewritten.dirs, client_factory=lambda: fake)

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert payload["ok"] is False and payload["reason"] == "fact_anchor_mismatch"
    assert "多出" in payload["text"], payload["text"]


def test_over_declaration_is_visible_without_changing_the_verdict(rewritten: SimpleNamespace) -> None:
    """锚点一个没少却申报「放弃了某项」→ 照旧放行，但**多申报**这件事必须看得见（不静默）。"""
    from ar.rewrite import check_fidelity

    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient(
        compressed=P005_KEEPING,
        declarations={"quotes": [FAKE_QUOTE], "note": "其实这一项我照抄了"},
    )
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]

    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()

    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True, "多申报不改放行结论（本来就没有缺失可放行）"
    assert payload["waived"] == []
    assert [item["kind"] for item in payload["over_declared"]] == ["quotes"]
    assert any("多申报" in note for note in payload["degraded"]), payload["degraded"]
    # 产物里也留着这一份申报（重跑 ar check 后进 fidelity.over_declared）
    assert _payload(rewritten.target, "changes.json")["changes"][order.index("p005")]["fact_declarations"] == {
        "quotes": [FAKE_QUOTE],
        "note": "其实这一项我照抄了",
    }
    outcome = check_fidelity(
        SOURCE_ID, rewritten.dirs.data, profiles_dir=rewritten.dirs.profiles, reader=READER
    )
    assert outcome.mismatches == [] and outcome.waived == []
    assert [item["kind"] for item in outcome.over_declared] == ["quotes"]
    assert _payload(rewritten.target, "changes.json")["fidelity"]["over_declared"], "产物里没留痕"


def test_compress_prompt_carries_the_policy_block_and_keeps_the_text_last(rewritten: SimpleNamespace) -> None:
    """``compress-v2`` 提示词：口径块按**本读者**那一档写、申报规则在场、要压的原文仍在末尾。"""
    from ar.prompts import fact_anchor_block

    write_profile_with_policy(rewritten.dirs)
    fake = _CompressClient()
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    current = _blocks(rewritten.target)[order.index("p006")]

    client.post("/api/compress", json=compress_body())

    assert len(fake.compress_calls) == 1
    prompt = fake.compress_calls[0][-1]["content"]
    # 规则序号 6（压缩模板里申报那条），不是 rewrite 的 10
    assert fact_anchor_block("allow_declared_drops", rule_no=6) in prompt
    assert fact_anchor_block("keep_all", rule_no=6) not in prompt
    assert "fact_declarations" in prompt
    # 会变的内容仍在末尾（前缀缓存纪律）
    assert prompt.rstrip().endswith(f"[p006] {current}")


def test_compress_not_shorter_is_rejected(rewritten: SimpleNamespace) -> None:
    """压缩稿不比该段当前正文短 → 拒绝写入（正文保持原样）。"""
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    current = _blocks(rewritten.target)[order.index("p006")]
    fake = _CompressClient(compressed=current + "又加了一句更啰嗦的话。")
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    before = _artifacts(rewritten.target)

    payload = client.post("/api/compress", json=compress_body()).json()
    assert set(payload) == DEGRADED_KEYS
    assert payload["ok"] is False and payload["reason"] == "compress_not_shorter"
    assert "不比这一段现在的正文" in payload["text"]
    assert _artifacts(rewritten.target) == before


def test_reason_is_echoed_into_the_change_note_and_the_event(rewritten: SimpleNamespace) -> None:
    """``reason`` 缺省是 ``too_verbose``；给了别的值也不进提示词，只落在依据与事件里。"""
    fake = _CompressClient()
    client = make_client(rewritten.dirs, client_factory=lambda: fake)
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]

    payload = client.post("/api/compress", json=compress_body(reason="re-read")).json()
    assert payload["ok"] is True
    change = _payload(rewritten.target, "changes.json")["changes"][order.index("p006")]
    assert "reason=re-read" in change["why"]["note"]
    event = json.loads(
        (rewritten.dirs.profiles / READER / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["requested_reason"] == "re-read"

    # 缺省值：请求里不写 reason 也按 too_verbose 处理
    fake2 = _CompressClient()
    client2 = make_client(rewritten.dirs, client_factory=lambda: fake2)
    body = {"source_id": SOURCE_ID, "paragraph": "p001"}
    assert client2.post("/api/compress", json=body).json()["ok"] is True
    event = json.loads(
        (rewritten.dirs.profiles / READER / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["requested_reason"] == "too_verbose"


def test_event_write_failure_is_visible_on_the_success_path(
    rewritten: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行为事件是**尽力而为**的采集：写失败不改压缩结果，但**不许静默**（2026-10-13 收口）。

    成功响应照旧 ``ok:true`` + 写盘，只是 ``degraded`` 里多一条可读说明 —— 前端在成功路径上
    照样打 warn 横幅。两种失败都要有字：① 追加写抛异常；② 事件没通过校验被丢弃。
    """
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient())

    def boom(*args: Any, **kwargs: Any) -> int:
        raise OSError("events.jsonl 只读（回归用例）")

    # ① 追加写失败：events.jsonl 落不下去
    monkeypatch.setattr("ar.server.append_events", boom)
    payload = client.post("/api/compress", json=compress_body()).json()
    assert set(payload) == SUCCESS_KEYS
    assert payload["ok"] is True and payload["artifacts_updated"] is True
    assert payload["text"] == COMPRESSED
    assert _blocks(rewritten.target)[order.index("p006")] == COMPRESSED, "事件写失败不该拦住压缩写盘"
    assert payload["degraded"], "事件写失败被吞掉了：成功路径也要有一条可读说明"
    assert any("events.jsonl" in note for note in payload["degraded"]), payload["degraded"]
    assert any("不影响压缩结果与产物" in note for note in payload["degraded"]), payload["degraded"]
    assert not (rewritten.dirs.profiles / READER / "events.jsonl").exists(), "没写成功就不许有文件"

    # ② 事件没通过校验（被丢弃）：同样在成功响应里留字
    monkeypatch.setattr("ar.server.append_events", lambda *a, **k: 0)
    monkeypatch.setattr("ar.server.validate_event", lambda *a, **k: (None, "kind 不在白名单"))
    payload = client.post("/api/compress", json=compress_body(paragraph="p001")).json()
    assert payload["ok"] is True and set(payload) == SUCCESS_KEYS
    assert payload["degraded"], "事件被丢弃也必须可见"
    assert any("事件没通过校验" in note for note in payload["degraded"]), payload["degraded"]
    assert _blocks(rewritten.target)[order.index("p001")] == COMPRESSED


def test_compress_cache_key_is_frozen_and_carries_the_profile_fingerprint(tmp_path) -> None:  # noqa: ANN001
    """缓存键 = ``(source_id, paragraph, "compress", "", 画像指纹)``（§5.2 / §1 第 14 条）。"""
    from ar.cache import COMPRESS_KIND, compress_cache_key, explain_cache_key

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    key = compress_cache_key(source_id=SOURCE_ID, paragraph_id="p006", profiles_dir=profiles, reader=READER)
    assert key == (
        SOURCE_ID,
        "p006",
        COMPRESS_KIND,
        "",
        (str(profiles / READER / "profile.yaml"), -1, -1),
    )
    # 与讲解的键不可能撞（kind 一格不同），画像一改指纹就换
    explain = explain_cache_key(
        source_id=SOURCE_ID, paragraph_id="p006", kind="explain", selection="", profiles_dir=profiles, reader=READER
    )
    assert key != explain
    (profiles / READER).mkdir(parents=True, exist_ok=True)
    (profiles / READER / "profile.yaml").write_text("reader: local-default\n", encoding="utf-8")
    assert compress_cache_key(source_id=SOURCE_ID, paragraph_id="p006", profiles_dir=profiles, reader=READER) != key


def test_check_passes_after_a_successful_compress(rewritten: SimpleNamespace) -> None:
    """压缩写回后产物仍然自洽：段数 / 段序对位、事实锚全对 → ``ar check`` 通过（checked 翻 true）。"""
    from ar.rewrite import check_fidelity

    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient())
    assert client.post("/api/compress", json=compress_body()).json()["ok"] is True

    outcome = check_fidelity(SOURCE_ID, rewritten.dirs.data)
    assert outcome.problems == [] and outcome.mismatches == []
    assert outcome.checked is True
    assert outcome.counts["paragraphs"] == len(rewritten.doc["paragraphs"])


def test_compressing_the_expanded_draft_shortens_what_the_reader_sees(rewritten: SimpleNamespace) -> None:
    """压的是**阅读器里显示的正文**（p005 是展开稿）：比原文长但比当前正文短也算压缩成功。"""
    order = [str(p["id"]) for p in rewritten.doc["paragraphs"]]
    current = _blocks(rewritten.target)[order.index("p005")]
    source = str(rewritten.doc["paragraphs"][order.index("p005")]["text"])
    assert len(current) > len(source), "这条用例需要「展开稿比原文长」的现场"
    # 比当前正文短、但仍比原文长；三类事实锚逐字照抄、顺序不变
    compressed = P005_KEEPING
    assert len(compressed) < len(current)

    client = make_client(rewritten.dirs, client_factory=lambda: _CompressClient(compressed=compressed))
    payload = client.post("/api/compress", json=compress_body(paragraph="p005")).json()
    assert payload["ok"] is True, payload
    assert payload["text"] == compressed
    assert payload["degraded"] == [], "这一段有事实锚：真跑过校验就不该出现空跑说明"
    assert _blocks(rewritten.target)[order.index("p005")] == compressed
