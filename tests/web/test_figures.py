"""就地配图的无头验证（playwright 驱动本机 Edge；`PLAN-actions-feedback.md` §4.3）。

覆盖五件事：

1. **就地渲染**：图容器在段落容器 `#blk-pNNN` 内、**正文之后**；SVG 走 `DOMParser` 插**真节点**
   （绕开 `md.js` 的 `escapeHtml`——否则页面里只会出现 SVG 源码文本）；图不进 `rewritten.md`。
2. **mermaid 懒加载**：页面打开时 5.3 MiB 的 `web/vendor/mermaid.min.js` **一个字节都不下**
   （`__arDebug.figureScript.injected === 0`），滚到图那里才注入 `<script>` 并调 `mermaid.render()`；
   **只注入一次**（两个 mermaid 图 → 一个 `<script>`）。
3. **失败退回文字**（DESIGN §6）：加载失败（路由 abort）/ `render()` 抛错（假库）/
   渲染返回空串 / 后端已给 `figure_error`（夹具 p016）/ SVG 源码畸形 / 带 `<script>` 的 SVG ——
   六种都在**原位置**留一句可读说明 + 横幅 + `__arDebug.degradations`，**不留空白、不留破图**。
4. **可断言**：容器的 `data-state`（pending/loading/ready/fallback）与 `__arDebug.figures[]`。
5. **不发模型请求**：画图这条路只碰静态资源与 `/api/doc`，不碰 `/api/explain`、`/api/compress`。

就绪只等页面里的显式标志（`__arDebug.ready` / `data-state`），不 sleep；每个用例跑完由 conftest
统一断言页面没有 JS 报错（路由 abort 造成的 `Failed to load resource` 是既定噪音，conftest 里已排除）。
"""

from __future__ import annotations

import copy
import re

import pytest
from playwright.sync_api import expect

from ar_backend import (
    FAKE_MERMAID_EMPTY_JS, FAKE_MERMAID_JS, FAKE_MERMAID_THROWS_JS, FakeApi, dbg,
    fixture_doc, has_banner, open_api, open_fixture,
)

MERMAID_URL_GLOB = "**/vendor/mermaid.min.js"
MERMAID_SCRIPT = "mermaid.min.js"


@pytest.fixture(scope="module")
def doc() -> dict:
    return fixture_doc()


@pytest.fixture
def requests(page) -> list:
    """本次页面发过的所有请求 URL（断言懒加载次数与「不发模型请求」）。"""
    urls: list = []
    page.on("request", lambda req: urls.append(req.url))
    return urls


def mermaid_script_requests(urls: list) -> list:
    return [url for url in urls if url.endswith(MERMAID_SCRIPT)]


def squeeze(text: str) -> str:
    """去空白后的可比形态（Markdown 渲染会把段内换行变成空格，比对时不能死抠空白）。"""
    return re.sub(r"\s+", "", text)


def figure(page, paragraph_id: str):
    return page.locator(f'#blk-{paragraph_id} [data-role="figure"]')


def figure_state(page, paragraph_id: str) -> str:
    return page.evaluate(
        "(pid) => { const n = document.querySelector(`#blk-${pid} [data-role=figure]`);"
        " return n ? n.dataset.state : null; }",
        paragraph_id,
    )


def figure_record(page, paragraph_id: str) -> dict:
    return page.evaluate(
        "(pid) => (globalThis.__arDebug.figures || []).find((f) => f.paragraph === pid) || null",
        paragraph_id,
    )


def scroll_figure_into_view(page, paragraph_id: str) -> None:
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid} [data-role=figure]`)"
        ".scrollIntoView({block: 'center'})",
        paragraph_id,
    )


def wait_figure_state(page, paragraph_id: str, state: str, timeout: int = 60000) -> None:
    page.wait_for_function(
        "(arg) => { const n = document.querySelector(`#blk-${arg[0]} [data-role=figure]`);"
        " return n && n.dataset.state === arg[1]; }",
        arg=[paragraph_id, state], timeout=timeout,
    )


def stub_mermaid(page, script: str) -> None:
    """把绘图库换成一个几十字节的假实现（`render()` 的形状照 mermaid 12）。"""
    page.route(MERMAID_URL_GLOB, lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=script,
    ))


def two_mermaid_figures(doc: dict) -> dict:
    """把夹具改成「两个 mermaid 图」（p013 / p024 都配 mermaid），用来验只注入一次。"""
    payload = copy.deepcopy(doc)
    by_id = {c["paragraph"]: c for c in payload["changes"]["changes"]}
    for pid in ("p013", "p024"):
        by_id[pid].pop("figure_error", None)
        by_id[pid]["action"] = "illustrate"
        by_id[pid]["figure"] = {
            "kind": "mermaid",
            "source": 'flowchart TD\n  A["甲"] --> B["乙"]',
            "caption": f"{pid} 的流程图",
        }
    return payload


# =================================================== 1. SVG 就地渲染（夹具 p024）

def test_svg_figure_renders_in_place_after_the_text(page, ar_server, requests, doc):
    open_fixture(page, ar_server)
    changes = {c["paragraph"]: c for c in doc["changes"]["changes"]}

    node = figure(page, "p024")
    expect(node).to_be_visible()
    assert node.get_attribute("data-kind") == "svg"
    assert node.get_attribute("data-state") == "ready"
    assert figure_record(page, "p024")["state"] == "ready"

    # 图容器在段落容器内、**正文之后**（不是别处，也不是浮窗）
    assert page.evaluate(
        """() => {
            const block = document.querySelector('#blk-p024');
            const text = block.querySelector('[data-role=block-text]');
            const fig = block.querySelector('[data-role=figure]');
            return !!fig && !!(text.compareDocumentPosition(fig) & Node.DOCUMENT_POSITION_FOLLOWING);
        }""",
    ), "图容器要在段落容器内、正文之后"

    # 里面是**真 SVG 节点**（绕开 md.js 的转义：否则这里只会是一段源码文本）
    assert page.evaluate(
        "() => { const s = document.querySelector('#blk-p024 [data-role=figure] svg');"
        " return !!s && s.namespaceURI === 'http://www.w3.org/2000/svg'; }",
    ), "SVG 必须是真节点（被 escapeHtml 转成文本就会在这里露馅）"
    assert page.locator('#blk-p024 [data-role="figure"] svg rect').count() == 2
    assert "8 蒲式耳" in node.inner_text()
    assert page.locator('#blk-p024 [data-role="figure-caption"]').text_content() == \
        changes["p024"]["figure"]["caption"]
    assert "<svg" not in node.inner_text(), "源码的尖括号跑到页面上当文字了"

    # SVG 不需要绘图库：一个脚本都不该注入
    assert dbg(page)["figureScript"]["injected"] == 0
    assert mermaid_script_requests(requests) == []

    # 图不进 rewritten.md：该段正文与底本一致（Markdown 标记渲染成标签，比对时去掉 ** 与空白）
    paragraph_text = {p["id"]: p["text"] for p in doc["meta"]["paragraphs"]}["p024"]
    rendered = page.locator('#blk-p024 [data-role="block-text"]').inner_text()
    assert squeeze(rendered) == squeeze(paragraph_text.replace("**", ""))


def test_svg_without_namespace_is_repaired_not_dropped(page, ar_server):
    """缺 `xmlns` 的 SVG：修好命名空间再插（裸 `<svg>` 插进 HTML 是「看不见的破图」）。"""
    payload = copy.deepcopy(fixture_doc())
    by_id = {c["paragraph"]: c for c in payload["changes"]["changes"]}
    by_id["p024"]["figure"] = {
        "kind": "svg",
        "source": '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>',
        "caption": "缺 xmlns 的图",
    }
    api = FakeApi(page, payload)
    open_api(page, ar_server)

    assert figure_state(page, "p024") == "ready"
    assert page.evaluate(
        "() => { const s = document.querySelector('#blk-p024 [data-role=figure] svg');"
        " return !!s && s.namespaceURI === 'http://www.w3.org/2000/svg'; }",
    )
    assert api.model_calls() == []


# =================================================== 2. mermaid 懒加载 + 真渲染（夹具 p013）

def test_mermaid_is_lazy_loaded_only_when_the_figure_scrolls_into_view(page, ar_server, requests):
    open_fixture(page, ar_server)

    # 首屏：图还没进视口 → 绘图库一个字节都没下，容器是占位状态
    assert figure_state(page, "p013") == "pending"
    assert dbg(page)["figureScript"]["injected"] == 0
    assert mermaid_script_requests(requests) == [], "打开页面就下了 5.3 MiB 的绘图库（懒加载没生效）"
    assert page.locator('#blk-p013 [data-role="figure"] svg').count() == 0

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "ready")

    assert dbg(page)["figureScript"]["injected"] == 1
    assert len(mermaid_script_requests(requests)) == 1, mermaid_script_requests(requests)
    assert page.locator('#blk-p013 [data-role="figure"] svg').count() == 1
    # 真库渲染出来的图（mermaid 会给 svg 带 class="flowchart"）
    klass = page.locator('#blk-p013 [data-role="figure"] svg').get_attribute("class") or ""
    assert "flowchart" in klass, f"不像 mermaid 真渲染的结果：class={klass!r}"
    assert figure_record(page, "p013")["state"] == "ready"
    # 夹具里 p016 本来就是「后端判定配图失败」的样本，所以只针对 p013 断言没有降级
    assert not has_banner(page, "配图渲染失败（p013）")
    assert page.locator('#blk-p013 [data-role="figure-caption"]').text_content()
    assert page.evaluate(
        """() => {
            const block = document.querySelector('#blk-p013');
            const text = block.querySelector('[data-role=block-text]');
            const fig = block.querySelector('[data-role=figure]');
            return !!(text.compareDocumentPosition(fig) & Node.DOCUMENT_POSITION_FOLLOWING);
        }""",
    )


def test_mermaid_script_is_injected_at_most_once_per_page(page, ar_server, requests, doc):
    """两个 mermaid 图也只有一个 `<script>`（装的是同一个脚本，注入两次没有意义）。"""
    stub_mermaid(page, FAKE_MERMAID_JS)
    FakeApi(page, two_mermaid_figures(doc))
    open_api(page, ar_server)

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "ready")
    scroll_figure_into_view(page, "p024")
    wait_figure_state(page, "p024", "ready")

    assert dbg(page)["figureScript"]["injected"] == 1
    assert len(mermaid_script_requests(requests)) == 1, mermaid_script_requests(requests)
    assert page.locator('#blk-p013 [data-role="figure"] svg').count() == 1
    assert page.locator('#blk-p024 [data-role="figure"] svg').count() == 1


def test_figure_rendering_makes_no_model_requests(page, ar_server, doc):
    stub_mermaid(page, FAKE_MERMAID_JS)
    api = FakeApi(page, two_mermaid_figures(doc))
    open_api(page, ar_server)

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "ready")
    scroll_figure_into_view(page, "p024")
    wait_figure_state(page, "p024", "ready")

    assert api.model_calls() == [], f"画图不该打模型：{api.model_calls()}"


# =================================================== 3. 失败一律退回文字

def test_mermaid_load_failure_falls_back_to_text(page, ar_server, requests):
    """加载失败（脚本取不到）：原位置留一句可读说明 + 横幅，不留空白、不留破图。"""
    page.route(MERMAID_URL_GLOB, lambda route: route.abort())
    open_fixture(page, ar_server)

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "fallback")

    node = figure(page, "p013")
    text = node.inner_text()
    assert "图渲染失败，退回文字说明" in text, text
    assert "图表库加载失败" in text, text
    assert page.locator('#blk-p013 [data-role="figure"] svg').count() == 0, "不许留破图"
    record = figure_record(page, "p013")
    assert record["state"] == "fallback" and record["reason"]
    assert has_banner(page, "配图渲染失败（p013）")
    assert any(d["kind"] == "figure" for d in dbg(page)["degradations"])
    # 图注还在（读者至少知道这里本来该画什么）
    assert page.locator('#blk-p013 [data-role="figure-caption"]').text_content()


def test_mermaid_render_error_falls_back_to_text(page, ar_server, requests):
    """`render()` 抛错（真库遇到畸形源码就是这样）：同样退回文字。"""
    stub_mermaid(page, FAKE_MERMAID_THROWS_JS)
    open_fixture(page, ar_server)

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "fallback")

    text = figure(page, "p013").inner_text()
    assert "图渲染失败，退回文字说明" in text and "Parse error" in text, text
    assert page.locator('#blk-p013 [data-role="figure"] svg').count() == 0
    assert has_banner(page, "配图渲染失败（p013）")


def test_mermaid_empty_render_result_falls_back_to_text(page, ar_server, requests):
    """「渲染成功」但返回空串（真库偶发）：也必须说出来，不许静默空白。"""
    stub_mermaid(page, FAKE_MERMAID_EMPTY_JS)
    open_fixture(page, ar_server)

    scroll_figure_into_view(page, "p013")
    wait_figure_state(page, "p013", "fallback")
    assert "空结果" in figure(page, "p013").inner_text()


def test_backend_figure_error_shows_text_and_degrades_visibly(page, ar_server, requests, doc):
    """后端已判定配图失败（`figure_error`）：原地说明 + 降级可见，且**根本不去加载绘图库**。"""
    open_fixture(page, ar_server)
    changes = {c["paragraph"]: c for c in doc["changes"]["changes"]}
    assert changes["p016"]["action"] == "keep", "配图失败那一段的 action 记 keep（正文照旧）"
    assert "figure" not in changes["p016"]

    node = figure(page, "p016")
    expect(node).to_be_visible()
    assert node.get_attribute("data-state") == "fallback"
    text = node.inner_text()
    assert "图渲染失败，退回文字说明" in text
    assert changes["p016"]["figure_error"] in text
    # 没有 caption 时把 why.note 一并写出来（契约 §4.3：caption 或 why.note）
    assert "原本要画的是" in text and changes["p016"]["why"]["note"] in text

    assert figure_record(page, "p016")["reason"] == changes["p016"]["figure_error"]
    assert has_banner(page, "配图渲染失败（p016）")
    assert any(d["kind"] == "figure" for d in dbg(page)["degradations"])
    # 这一段本来就没图可画：绘图库一次都不该被注入
    assert dbg(page)["figureScript"]["injected"] == 0
    assert mermaid_script_requests(requests) == []
    # 正文照旧：动作标记是「保持」，也没有「未实现」那一行
    assert page.locator('#blk-p016 [data-role="marker"]').get_attribute("data-action") == "keep"
    assert page.locator('#blk-p016 [data-role="tip-deferred"]').count() == 0


def test_malformed_svg_falls_back_to_text(page, ar_server):
    payload = copy.deepcopy(fixture_doc())
    by_id = {c["paragraph"]: c for c in payload["changes"]["changes"]}
    by_id["p024"]["figure"] = {"kind": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><rect',
                               "caption": "畸形的图"}
    api = FakeApi(page, payload)
    open_api(page, ar_server)

    assert figure_state(page, "p024") == "fallback"
    text = figure(page, "p024").inner_text()
    assert "图渲染失败，退回文字说明" in text and "SVG 源码不是合法 XML" in text, text
    assert page.locator('#blk-p024 [data-role="figure"] svg').count() == 0
    assert has_banner(page, "配图渲染失败（p024）")
    assert api.model_calls() == []


def test_svg_with_a_script_tag_is_refused_by_the_whitelist(page, ar_server):
    """白名单（§4.2）第二道闸：带 `<script>` 的 SVG 直接退回文字，不冒险插进页面。"""
    payload = copy.deepcopy(fixture_doc())
    by_id = {c["paragraph"]: c for c in payload["changes"]["changes"]}
    by_id["p024"]["figure"] = {
        "kind": "svg",
        "source": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        "caption": "带脚本的图",
    }
    FakeApi(page, payload)
    open_api(page, ar_server)

    assert figure_state(page, "p024") == "fallback"
    assert "不允许的内容" in figure(page, "p024").inner_text()
    assert page.locator('#blk-p024 [data-role="figure"] svg').count() == 0
    assert page.locator('#blk-p024 [data-role="figure"] script').count() == 0


def test_unknown_figure_kind_falls_back_to_text(page, ar_server):
    payload = copy.deepcopy(fixture_doc())
    by_id = {c["paragraph"]: c for c in payload["changes"]["changes"]}
    by_id["p024"]["figure"] = {"kind": "graphviz", "source": "digraph {}", "caption": "别的图类型"}
    FakeApi(page, payload)
    open_api(page, ar_server)

    assert figure_state(page, "p024") == "fallback"
    assert "不认识的图类型" in figure(page, "p024").inner_text()
    assert dbg(page)["figureScript"]["injected"] == 0


# =================================================== 4. 无图时什么也不做

def test_document_without_figures_renders_no_figure_containers(page, ar_server, doc):
    payload = copy.deepcopy(doc)
    for change in payload["changes"]["changes"]:
        change.pop("figure", None)
        change.pop("figure_error", None)
    FakeApi(page, payload)
    open_api(page, ar_server)

    assert page.locator('[data-role="figure"]').count() == 0
    assert dbg(page)["figures"] == []
    assert dbg(page)["figureScript"]["injected"] == 0
    assert page.locator("#rewritten .block").count() == 25
    assert dbg(page)["errors"] == []
