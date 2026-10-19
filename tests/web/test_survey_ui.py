"""读前背景问卷的无头验证（playwright 驱动本机 Edge；夹具模式 + 路由级假后端）。

覆盖 PLAN-background-survey.md §12「前端 1–4 条」与 §9 / §11 的前端纪律：

1. 表单渲染 + 条件题联动（选「初中及以下」时大学题收起）；
2. 提交 → 可见成功说明 + 「改写产物要重跑」提示；跳过 → 可见横幅；
3. `?survey=1` 夹具模式下可填、提交后写明「夹具模式不保存」；
4. `__arDebug` 标志与就绪等待沿用现有纪律（只等显式标志，不 sleep），
   每个用例的 autouse「无 JS 报错」断言（conftest.py:151-156）必须通过。

另外两条防「前后端走偏」的断言（2026-10-08 后端结算后补，见 §「键集对照」）：

- 前端**没有**第二份手写题表：夹具题目来自 `samples/<id>/survey-questions.json`，
  本文件把它与后端的 `ar.survey.QUESTIONS` 逐项对照（枚举、条件、校验约束全比）；
- **提交体的键集**与 `GET /api/survey` 的 `questions[].field` 键集对照，少一个键/多一个键即失败
  （`test_submitted_answer_keys_match_backend_question_fields`）——上一版字段名错位就是这么漏过去的。

本文件**不改动** tests/web/conftest.py：夹具用例走 ar_server 的静态服务，
正式模式用例用 `page.route` 在浏览器侧挂一层假后端（`/api/doc`、`/api/profile`、`GET/POST /api/survey`）。
真服务联调用例仍留在 test_reader_features.py。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = PROJECT / "samples" / FIXTURE_ID
DOC_PATH = FIXTURE_DIR / "doc.json"
QUESTIONS_PATH = FIXTURE_DIR / "survey-questions.json"

SURVEY_PARAM = "&survey=1"

# 夹具响应体 `samples/<id>/survey.json` 的 URL 匹配式。
# 用正则而不是 `**/samples/**/survey.json`：后者会把 `survey-questions.json` 一并命中。
FIXTURE_RESPONSE_GLOB = re.compile(r"/samples/[^/]+/survey\.json$")

# 夹具题目文件里的题号（= 后端 ar.survey.QUESTIONS 的 qid 顺序）
QIDS = ["Q1", "Q2", "Q3a", "Q3b", "Q4", "Q4b", "Q5", "Q6"]

# 假后端回给前端的字段名（= 后端 ANSWER_SLOTS 的值，2026-10-08 对齐）
CONTRACT_FIELDS = [
    "education", "high_school_electives", "discipline", "major",
    "strengths", "strengths_note", "reading_materials", "preference",
]

# 上一版前端用错过的旧名（2026-10-08）：这几个一旦回到提交体里，后端就是 400
STALE_FIELDS = ["materials", "explanation"]


def backend_questions() -> list:
    """后端的题目定义（`src/ar/survey.py`）——夹具题目文件的来源，比对用。

    后端还没落地时 skip；其余用例不依赖它。
    """
    sys.path.insert(0, str(PROJECT / "src"))
    survey = pytest.importorskip("ar.survey", reason="后端 ar.survey 还没落地")
    return survey.QUESTIONS


@pytest.fixture(scope="module")
def backend_qs() -> list:
    return backend_questions()


# ------------------------------------------------------------------ 夹具

@pytest.fixture(scope="module")
def fixture_doc() -> dict:
    return json.loads(DOC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_questions() -> dict:
    """夹具模式实际读到的那份题目（后端 QUESTIONS 的导出）。"""
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 工具

@pytest.fixture(autouse=True)
def survey_fixture_response_off(page):
    """默认把夹具响应体挡住：只有用例显式打开它（`?survey=fixture`）时才该被读到。

    这样「页面在没有那份样例数据时照常工作」也一直在被测。
    """
    def handle(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"version": 1, "questions": []}))

    # 精确匹配 survey.json（`survey-questions.json` 会被通配符一并命中，所以必须分开）
    page.route(FIXTURE_RESPONSE_GLOB, handle)


def wait_ready(page, timeout: int = 15000) -> dict:
    """等页面里的显式就绪标志——不许 sleep（沿用 test_reader_ui.py 的口径）。"""
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true",
        timeout=timeout,
    )
    debug = page.evaluate("() => globalThis.__arDebug")
    assert debug["jsLoaded"] is True, "模块没加载起来（看 __arDebug.errors）"
    return debug


def dbg(page) -> dict:
    return page.evaluate("() => globalThis.__arDebug")


def open_fixture(page, ar_server, extra: str = SURVEY_PARAM) -> str:
    """夹具模式打开**阅读器页**（2026-10-15 拆页后是 `web/reader.html`；`web/index.html` 是启动页）。"""
    url = f"{ar_server}/web/reader.html?fixture={FIXTURE_ID}{extra}"
    page.goto(url)
    wait_ready(page)
    return url


def open_api(page, ar_server, extra: str = "") -> str:
    """正式模式走假后端（page.route 在浏览器侧拦 /api/*）；同样是阅读器页。"""
    url = f"{ar_server}/web/reader.html?source={FIXTURE_ID}{extra}"
    page.goto(url)
    wait_ready(page)
    return url


def wait_survey_open(page, timeout: int = 10000):
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.surveyOpen === true",
        timeout=timeout,
    )
    return page.locator('[data-role="survey-card"]')


def wait_survey_closed(page, timeout: int = 10000) -> None:
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.surveyOpen === false",
        timeout=timeout,
    )


def q(page, qid: str):
    return page.locator(f'[data-role="survey-question"][data-qid="{qid}"]')


def pick(page, qid: str, *values: str) -> None:
    for value in values:
        page.locator(f'[data-qid="{qid}"][data-value="{value}"]').check()


def fill_text(page, qid: str, text: str) -> None:
    page.locator(f'[data-qid="{qid}"][data-role="survey-text"], '
                 f'[data-qid="{qid}"][data-role="survey-note"]').fill(text)


def events(page, kind: str) -> list:
    return [e for e in dbg(page)["surveyEvents"] if e["type"] == kind]


def banner_texts(page) -> list:
    banners = page.locator('[data-role="banner"]')
    return [banners.nth(i).text_content() for i in range(banners.count())]


def assert_no_page_errors(page, ar_errors) -> None:
    assert dbg(page)["errors"] == [], dbg(page)["errors"]
    assert [e for e in ar_errors if "favicon" not in e] == []


def stub_fixture_questions_missing(page):
    """让夹具题目读不到内容，用来验证「拿不到题目」的可见降级。

    回的是 200 + 空 `questions[]`（而不是 404）：前端会按「文件里没有题目」处理，
    还能避开静态服务 404 在控制台留下的 `Failed to load resource` 噪音
    （那是被我们故意打断的请求，与「页面有没有 JS 报错」这条纪律混在一起会误伤）。
    """
    def handle(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"version": 1, "questions": []}))

    page.route("**/samples/**/survey-questions.json", handle)


def fake_backend(page, fixture_doc, *, survey: dict, profile: dict | None = None,
                 survey_post: dict | None = None, survey_status: int = 200,
                 followup_post: dict | None = None, followup_status: int = 200,
                 followup_save_status: int | None = None,
                 doc_payload: dict | None = None):
    """挂一层路由级的假后端；返回请求记录列表（可断言前端到底发了什么）。

    两个端点分工（契约 §8）：
      - `POST /api/survey/followup`：**生成**追问问题；
      - `POST /api/survey`：写画像——固定题作答、整场跳过、以及 `{followups: [...]}` 的追问作答。
    所以这里对 `POST /api/survey` 按请求体分流：带 `followups` 的当「存追问答案」，
    `followup_save_status` 只影响这一类（用来验证「答案没保存成功时卡片不关、字还在」）。
    """
    seen: list = []

    def handle(route, request):
        url = request.url
        method = request.method
        body_text = (request.post_data or "") if method == "POST" else None
        seen.append({"url": url, "method": method, "body": body_text})
        path = url.split("/api/", 1)[-1].split("?")[0]

        if path == "survey/followup" and method == "POST":
            if followup_status != 200:
                route.fulfill(status=followup_status, content_type="application/json", body="{}")
                return
            body = followup_post or {"questions": [], "text": "", "degraded": False,
                                     "prompt_version": "survey-v1"}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body, ensure_ascii=False))
            return
        if path == "survey" and method == "GET":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(survey, ensure_ascii=False))
            return
        if path == "survey" and method == "POST":
            if "followups" in (body_text or ""):
                # 存追问答案那一次：与固定题提交分开打桩
                if followup_save_status and followup_save_status != 200:
                    route.fulfill(status=followup_save_status,
                                  content_type="application/json", body="{}")
                    return
                saved = {"ok": True, "background": {"followups": []}, "sketch": "读者背景：…",
                         "degraded": []}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(saved, ensure_ascii=False))
                return
            if survey_status != 200:
                route.fulfill(status=survey_status, content_type="application/json",
                              body=json.dumps({"error": "InputError",
                                               "message": "答案 high_school_electives 必须正好 3 项"},
                                              ensure_ascii=False))
                return
            body = survey_post or {"ok": True, "background": {}, "sketch": "", "degraded": []}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body, ensure_ascii=False))
            return
        if path == "profile":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(profile or {"profile": {"reader": "local-default"},
                                                      "degraded": []}, ensure_ascii=False))
            return
        if path.startswith("doc/"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(doc_payload if doc_payload is not None else fixture_doc,
                                          ensure_ascii=False))
            return
        route.fulfill(status=404, content_type="application/json", body="{}")

    page.route("**/api/**", handle)
    return seen


def survey_payload(questions: list, **over) -> dict:
    payload = {"version": 1, "questions": questions, "answers": None, "skipped": False, "degraded": []}
    payload.update(over)
    return payload


# =================================================== 0. 题源单一 + 键集对照

def test_fixture_questions_file_matches_backend_definition(fixture_questions, backend_qs):
    """夹具题目的**来源**是后端的 QUESTIONS：枚举、条件、校验约束逐项比，不许前端自己抄一份。"""
    fixture_qs = fixture_questions["questions"]
    assert fixture_questions["version"] == 1
    assert [q["qid"] for q in fixture_qs] == [q["qid"] for q in backend_qs] == QIDS

    for mine, theirs in zip(fixture_qs, backend_qs):
        assert mine["qid"] == theirs["qid"]
        assert mine["field"] == theirs["field"], f'{mine["qid"]} 的字段名与后端不一致'
        assert mine["kind"] == theirs["kind"]
        assert mine["label"] == theirs["label"]
        assert mine["when"] == theirs["when"], f'{mine["qid"]} 的出现条件与后端不一致'
        assert mine.get("alsoWhen") == theirs.get("alsoWhen"), f'{mine["qid"]} 的二级条件与后端不一致'
        assert mine["exactly"] == theirs["exactly"]
        assert mine["atLeast"] == theirs["atLeast"]
        assert mine["exclusive"] == theirs["exclusive"]
        assert mine["maxLength"] == theirs["maxLength"]
        assert mine["placeholder"] == theirs["placeholder"]
        assert mine.get("hint") == theirs.get("hint")
        assert mine["options"] == theirs["options"], f'{mine["qid"]} 的选项与后端不一致'

    # 字段名清单：这一份是「提交体该有哪些键」的唯一依据
    assert [q["field"] for q in fixture_qs] == CONTRACT_FIELDS
    for stale in STALE_FIELDS:
        assert stale not in [q["field"] for q in fixture_qs], f"{stale} 是上一版用错的旧名"


def test_conditional_rules_cover_every_value_the_backend_excludes(backend_qs, fixture_questions):
    """条件题规则以 `requires`（权威）为准：Q3b 在 Q3a = none **和** unsure 时都要收起。

    2026-10-08 的经过：后端 Q3b 的 `requires` 是 `not_in: ["none", "unsure"]`，
    而给前端的别名当时只取第一个值（`none`）——照别名实现的一方会在「不确定」时多显示一个
    随后被 `question_applies` 丢掉的输入框。前端因此改成直接读权威 `requires`
    （`ruleFromRequires`），后端也把别名补齐成 `values: [全部被排除值]`。这条断言把两头都钉住。
    """
    q3b = [q for q in backend_qs if q["qid"] == "Q3b"][0]
    requirement = q3b["requires"][1]
    assert requirement["question"] == "Q3a"
    assert set(requirement["not_in"]) == {"none", "unsure"}

    alias = q3b["alsoWhen"]
    assert set(alias.get("values") or [alias.get("value")]) == {"none", "unsure"}, \
        "别名必须带全被排除值（只报第一个值就会漏掉「不确定」）"

    # 夹具文件与后端逐字一致（本文件已由后端 QUESTIONS 导出）
    mine = [q for q in fixture_questions["questions"] if q["qid"] == "Q3b"][0]
    assert mine["alsoWhen"] == alias


def test_submitted_answer_keys_match_backend_question_fields(page, ar_server, fixture_doc,
                                                             fixture_questions):
    """**键集对照**：提交体的键必须与 `GET /api/survey` 的 `questions[].field` 一一对上。

    这条是 2026-10-08 的补钉：上一版前端把 `reading_materials` / `preference` 写成了
    `materials` / `explanation`，`page.route` 打桩测不出来（桩不校验键名），
    后端一上手就是 400。所以这里拿**后端自己的题目定义**当标准答案，
    前端渲染出来的题目字段与真正 POST 出去的键集都要对得上：缺一个、多一个都失败。
    """
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    # 前端拿到的题目清单（`__arDebug.surveyQuestionsRaw`）= 后端给的 field 清单
    raw = dbg(page)["surveyQuestionsRaw"]
    expected_fields = [q["field"] for q in fixture_questions["questions"]]
    assert [item["field"] for item in raw] == expected_fields
    assert expected_fields == CONTRACT_FIELDS

    # 渲染出来的输入框 data-field：每个字段都有控件（自由文本也算）
    rendered = page.eval_on_selector_all(
        '[data-role="survey-question"] [data-field]',
        "els => [...new Set(els.map(e => e.dataset.field))]",
    )
    assert sorted(rendered) == sorted(expected_fields)

    # 填满一份能过的答案（字段名全部走后端那一套），提交
    pick(page, "Q1", "undergraduate")
    pick(page, "Q2", "physics", "chemistry", "biology")
    pick(page, "Q3a", "economics")
    fill_text(page, "Q3b", "国际经济与贸易")
    pick(page, "Q4", "economics_finance")
    fill_text(page, "Q4b", "读过《资本论》第一卷前两章")
    pick(page, "Q5", "textbook")
    pick(page, "Q6", "example")
    page.click('[data-role="survey-submit"]')
    # 提交成功后卡片停在「已完成 + 追问入口」这一步（契约 §4 的「问一次」），所以这里不关卡片
    page.wait_for_function(
        "() => globalThis.__arDebug.surveyPhase === 'done'", timeout=8000,
    )

    posts = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(posts) == 1, seen
    body = json.loads(posts[0]["body"])
    submitted = set(body["answers"])

    # ⚠ 提交体里**不许有 version**：后端 validate_answers 只认八个字段槽位，
    #   多给一个键就是 InputError → 400（已实测：`答案里有未知字段 version`）。
    assert submitted == set(expected_fields), (
        f"提交体键集与后端 questions[].field 对不上：多了 {submitted - set(expected_fields)}，"
        f"少了 {set(expected_fields) - submitted}"
    )
    for stale in STALE_FIELDS:
        assert stale not in body["answers"], f"提交体里还有旧字段名 {stale}"


# =================================================== 1. 表单渲染

def test_fixture_survey_form_renders_backend_questions(page, ar_server, ar_errors,
                                                       fixture_questions):
    """`?survey=1` 夹具模式：题目按后端那份定义渲染出来（题号 / 选项 / 约束逐项对得上）。"""
    open_fixture(page, ar_server)
    card = wait_survey_open(page)

    expect(card).to_be_visible()
    assert page.locator('[data-role="survey-layer"]').count() == 1

    qids = page.eval_on_selector_all(
        '[data-role="survey-question"]', "els => els.map(e => e.dataset.qid)"
    )
    assert qids == QIDS
    assert dbg(page)["surveySeen"] == QIDS

    for question in fixture_questions["questions"]:
        if question["kind"] not in ("single", "multi"):
            continue
        got = page.eval_on_selector_all(
            f'[data-role="survey-question"][data-qid="{question["qid"]}"]'
            ' input[data-value]',
            "els => els.map(e => e.value)",
        )
        assert got == [o["value"] for o in question["options"]], \
            f'{question["qid"]} 的选项与后端定义不一致'

    # 自由文本两题：单行专业 + 多行补充说明，长度上限来自后端（60 / 200）
    assert page.locator('input[data-role="survey-text"][data-qid="Q3b"]').get_attribute("maxlength") == "60"
    assert page.locator('textarea[data-role="survey-note"][data-qid="Q4b"]').get_attribute("maxlength") == "200"

    # 提交/跳过/关闭都在，且**没有 <form>**（默认提交会刷新页面，不许用）
    assert page.locator('[data-role="survey-submit"]').count() == 1
    assert page.locator('[data-role="survey-skip"]').count() == 1
    assert page.locator('[data-role="survey-close"]').count() == 1
    assert page.locator('[data-role="survey-card"] form').count() == 0

    assert card.get_attribute("data-fixture") == "true"
    debug = dbg(page)
    assert debug["survey"]["openReason"] == "fixture-explicit"
    assert debug["survey"]["version"] == 1
    assert debug["surveyQuestionSource"] == "fixture"
    assert debug["surveyBootReason"] == "fixture-explicit"
    assert_no_page_errors(page, ar_errors)


def test_fixture_mode_does_not_pop_the_survey_without_the_flag(page, ar_server):
    """夹具模式默认不弹问卷（没有服务端画像可比），也不发任何 /api/survey 请求。"""
    open_fixture(page, ar_server, extra="")

    debug = dbg(page)
    assert debug["surveyOpen"] is False
    assert debug["surveyBootReason"] == "fixture-mode"
    assert debug["survey"] is None
    assert debug["surveyEvents"] == []
    assert page.locator('[data-role="survey-layer"]').count() == 0
    assert page.locator("#rewritten .block").count() == 25


def test_missing_fixture_questions_degrade_visibly(page, ar_server, ar_errors):
    """夹具题目文件不在时不静默失败：正文照读，横幅说明缺了什么。"""
    stub_fixture_questions_missing(page)
    open_fixture(page, ar_server)

    page.wait_for_function(
        "() => globalThis.__arDebug.surveyBootReason === 'load-failed'", timeout=8000,
    )
    debug = dbg(page)
    assert debug["surveyOpen"] is False
    assert any(d["kind"] == "survey-load" for d in debug["degradations"])
    assert any("拿不到夹具问卷题目" in t for t in banner_texts(page))
    assert page.locator("#rewritten .block").count() == 25
    assert_no_page_errors(page, ar_errors)


def test_fixture_response_sample_backfills_and_warns_about_version(page, ar_server):
    """`?survey=fixture` 用 `samples/<id>/survey.json`（= GET /api/survey 的响应体）覆盖两条分支：
    回填已存答案、「问卷已更新」提示按 `answered_version !== version` 判定。"""
    sample = json.loads((FIXTURE_DIR / "survey.json").read_text(encoding="utf-8"))
    assert set(sample) >= {"version", "answered_version", "questions", "answers", "skipped"}
    assert sample["answered_version"] != sample["version"], "样例要覆盖「版本不一致」这一支"

    page.unroute(FIXTURE_RESPONSE_GLOB)
    page.route(FIXTURE_RESPONSE_GLOB,
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps(sample, ensure_ascii=False)))
    open_fixture(page, ar_server, extra="&survey=fixture")
    wait_survey_open(page)

    debug = dbg(page)
    assert debug["surveyQuestionSource"] == "fixture-response"
    assert debug["surveyFixtureResponse"]["answered_version"] == sample["answered_version"]

    # 回填：学历/学科门类勾上，依赖它们的条件题也跟着出现
    assert page.locator('[data-role="survey-question"][data-qid="Q1"] input[value="undergraduate"]').is_checked()
    assert page.locator('[data-role="survey-question"][data-qid="Q3a"] input[value="economics"]').is_checked()
    expect(q(page, "Q2")).to_be_visible()
    expect(q(page, "Q3b")).to_be_visible()
    # 已存的自由文本也回填
    assert page.locator('input[data-role="survey-text"][data-qid="Q3b"]').input_value() == sample["answers"]["major"]

    # 版本提示：可见横幅 + 可断言的降级项
    assert any("问卷已更新" in t for t in banner_texts(page)), banner_texts(page)
    assert any(d["kind"] == "survey-version" for d in debug["degradations"])
    # 版本号只来自响应，不进浏览器本地存储
    assert page.evaluate("() => Object.keys(window.localStorage)") == ["ar.settings.v1"]

# =================================================== 2. 条件题联动

def test_conditional_questions_follow_education(page, ar_server):
    """条件题规则（契约 §3）：选「初中及以下」时 Q2/Q3a/Q3b 收起，选回本科再出现。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    # 初始（未答学历）：依赖学历的三题都不出现
    for qid in ("Q2", "Q3a", "Q3b"):
        expect(q(page, qid)).to_be_hidden()
        assert dbg(page)["surveyVisible"][qid] is False

    pick(page, "Q1", "undergraduate")
    for qid in ("Q2", "Q3a", "Q3b"):
        expect(q(page, qid)).to_be_visible()

    # Q3b 是二级条件：Q3a 选「没读大学 / 不确定」时要收起
    pick(page, "Q3a", "none")
    expect(q(page, "Q3b")).to_be_hidden()
    pick(page, "Q3a", "unsure")
    expect(q(page, "Q3b")).to_be_hidden()
    pick(page, "Q3a", "economics")
    expect(q(page, "Q3b")).to_be_visible()

    # 学历改回「初中及以下」：大学相关的三题一起消失，且答案里不再带这些字段
    pick(page, "Q1", "junior_or_below")
    for qid in ("Q2", "Q3a", "Q3b"):
        expect(q(page, qid)).to_be_hidden()
    debug = dbg(page)
    assert debug["surveyVisible"]["Q1"] is True
    for qid in ("Q2", "Q3a", "Q3b"):
        assert debug["surveyVisible"][qid] is False
    assert debug["surveyAnswers"] == {"education": "junior_or_below"}

    # 显示态是 JS 联动出来的（不是 CSS 藏字）：可见题块 = 学历 / 特长 / 补充 / 常读 / 偏好
    assert page.eval_on_selector_all(
        '[data-role="survey-question"]',
        "els => els.filter(e => !e.hidden).map(e => e.dataset.qid)",
    ) == ["Q1", "Q4", "Q4b", "Q5", "Q6"]

    last = events(page, "change")[-1]
    assert last["qid"] == "Q1" and last["value"] == "junior_or_below"


def test_exactly_three_electives_is_validated_before_submitting(page, ar_server):
    """「正好 3 项」在前端先拦一次（后端还会复算），互斥项与多选互斥。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "high_school")
    pick(page, "Q2", "physics", "chemistry")
    page.click('[data-role="survey-submit"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_be_visible()
    assert "正好选 3 项" in status.text_content()
    assert dbg(page)["surveyOpen"] is True, "校验没过就不该关掉表单"
    assert events(page, "submit-request") == []
    assert events(page, "invalid"), "校验失败要留可断言的 invalid 事件"

    # 补满第 3 项后可以提交（夹具模式 → 走「不保存」那条路）
    pick(page, "Q2", "biology")
    page.click('[data-role="survey-submit"]')
    wait_survey_closed(page)
    assert "biology" in dbg(page)["surveyAnswers"]["high_school_electives"]

    # 互斥项：勾「记不清」会把三个选考科目全部让位
    open_fixture(page, ar_server, extra=SURVEY_PARAM)
    wait_survey_open(page)
    pick(page, "Q1", "high_school")
    pick(page, "Q2", "physics", "chemistry", "biology")
    pick(page, "Q2", "forgotten")
    assert page.eval_on_selector_all(
        '[data-role="survey-question"][data-qid="Q2"] input:checked',
        "els => els.map(e => e.value)",
    ) == ["forgotten"]


# =================================================== 3. 夹具模式提交

def test_fixture_submit_is_visible_and_says_it_does_not_save(page, ar_server, ar_inject,
                                                             ar_errors, fixture_questions):
    """契约 §12 第 3 条：夹具模式可填、提交后写明「夹具模式不保存」，且没有真的 POST。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "undergraduate")
    pick(page, "Q2", "physics", "chemistry", "biology")
    pick(page, "Q3a", "economics")
    fill_text(page, "Q3b", "国际经济与贸易")
    pick(page, "Q4", "economics_finance", "programming_computer")
    fill_text(page, "Q4b", "读过《资本论》第一卷前两章")
    pick(page, "Q5", "textbook", "papers")
    pick(page, "Q6", "example")
    page.click('[data-role="survey-submit"]')

    wait_survey_closed(page)
    texts = banner_texts(page)
    assert any("夹具模式不保存" in t for t in texts), texts
    # 表单连同整层一起撤掉：不留残影
    assert page.locator('[data-role="survey-layer"]').count() == 0
    assert page.locator('[data-role="survey-card"]').count() == 0

    done = events(page, "submit-done")
    assert done and done[-1]["kind"] == "fixture"
    assert "夹具模式不保存" in done[-1]["message"]
    answers = dbg(page)["surveyAnswers"]
    assert "version" not in answers, "提交体不许带 version（后端只认八个字段槽位）"
    assert answers["education"] == "undergraduate"
    assert answers["high_school_electives"] == ["physics", "chemistry", "biology"]
    assert answers["discipline"] == "economics"
    assert answers["major"] == "国际经济与贸易"
    assert answers["strengths"] == ["economics_finance", "programming_computer"]
    assert answers["reading_materials"] == ["textbook", "papers"]
    assert answers["preference"] == "example"

    # 夹具模式**没有**打任何 /api 请求（连 /api/survey 都没碰）
    assert not any(p.startswith("/api") for p in ar_inject.hits), ar_inject.hits
    assert_no_page_errors(page, ar_errors)


# =================================================== 4. 跳过 + Esc

def test_skip_writes_skipped_and_shows_banner_in_api_mode(page, ar_server, fixture_doc,
                                                          fixture_questions):
    """契约 §12 第 2 条：跳过 → 可见横幅「未提供背景，按通用读者处理」+ skipped=true 落盘。"""
    seen = fake_backend(page, fixture_doc,
                        survey=survey_payload(fixture_questions["questions"]))
    open_api(page, ar_server)
    wait_survey_open(page)

    page.click('[data-role="survey-skip"]')
    wait_survey_closed(page)

    posts = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(posts) == 1, seen
    assert json.loads(posts[0]["body"]) == {"skipped": True}

    texts = banner_texts(page)
    assert any("按通用读者处理" in t for t in texts), texts
    skip_done = events(page, "skip-done")
    assert skip_done and skip_done[-1]["reason"] == "skip-button"
    assert dbg(page)["surveyVisible"] != {}          # 表单确实渲染过
    assert page.locator('[data-role="survey-layer"]').count() == 0


def test_escape_equals_skip_and_focus_returns_to_article(page, ar_server, fixture_doc,
                                                         fixture_questions):
    """Esc 与「跳过」同效（未答过时直接跳过）；关闭后焦点回到正文栏。"""
    fake_backend(page, fixture_doc, survey=survey_payload(fixture_questions["questions"]))
    open_api(page, ar_server)
    wait_survey_open(page)

    page.keyboard.press("Escape")
    wait_survey_closed(page)

    done = events(page, "skip-done")
    assert done and done[-1]["reason"] == "escape"
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "rewritten"
    assert page.locator('[data-role="survey-layer"]').count() == 0


def test_escape_works_after_focus_leaves_the_card(page, ar_server, fixture_doc,
                                                  fixture_questions):
    """**回归**（2026-10-09 复核抓到的缺口）：点过正文、焦点离开卡片之后，`Esc` 仍与「跳过」同效。

    问卷设计上不吞正文点击，读者点一下正文焦点就落到 `#rewritten`（`tabindex="-1"`）——
    此前 `Esc` 只在卡片上监听，加上 main.js 在 `isSurveyOpen()` 时让路，这一下按 `Esc` 完全没反应。
    现在 `Esc` 挂在 `document` 上并复用「跳过」那条路径，所以这里两段都覆盖：
    ① 未答过时一次 `Esc` 直接跳过；② 答过一部分时先弹一次确认、再按一次才真跳过（与点按钮同效）。
    """
    seen = fake_backend(page, fixture_doc, survey=survey_payload(fixture_questions["questions"]))
    open_api(page, ar_server)
    wait_survey_open(page)

    # ① 点正文把焦点带走
    page.click("#blk-p002 [data-role='block-text']")
    focus = page.evaluate(
        "() => ({ tag: document.activeElement && document.activeElement.tagName,"
        " id: document.activeElement && document.activeElement.id,"
        " inCard: Boolean(document.activeElement"
        " && document.activeElement.closest('[data-role=\"survey-card\"]')) })"
    )
    assert focus["inCard"] is False, f"这一步就是要让焦点离开卡片，实际 {focus}"
    assert dbg(page)["surveyOpen"] is True

    page.keyboard.press("Escape")
    wait_survey_closed(page)
    assert events(page, "skip-done")[-1]["reason"] == "escape"
    posts = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(posts) == 1 and json.loads(posts[0]["body"]) == {"skipped": True}
    assert page.locator('[data-role="survey-layer"]').count() == 0

    # ② 重新打开并答一题，焦点再次离开卡片后按两次 Esc（第一次＝确认，第二次＝真跳过）
    open_api(page, ar_server, extra="&survey=1")
    wait_survey_open(page)
    pick(page, "Q1", "master")
    page.click("#blk-p002 [data-role='block-text']")
    assert page.evaluate(
        "() => !document.activeElement.closest('[data-role=\"survey-card\"]')"
    ) is True

    page.keyboard.press("Escape")
    expect(page.locator('[data-role="survey-status"]')).to_be_visible()
    assert "再点一次" in page.locator('[data-role="survey-status"]').text_content()
    assert dbg(page)["surveyOpen"] is True, "第一次 Esc 只确认，不关卡片"

    page.keyboard.press("Escape")
    wait_survey_closed(page)
    assert [e["reason"] for e in events(page, "skip-done")][-1] == "escape"


def test_filled_form_asks_once_before_skipping(page, ar_server, fixture_doc, fixture_questions):
    """正式模式：已经答过一部分再点「跳过」，先说清楚会丢什么，再点一次才真的跳过。

    （夹具模式不写盘，「跳过」只对本页生效，所以那里不弹确认——版本差异的可见说明在横幅里。）
    """
    seen = fake_backend(page, fixture_doc,
                        survey=survey_payload(fixture_questions["questions"]))
    open_api(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "master")
    page.click('[data-role="survey-skip"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_be_visible()
    assert "再点一次" in status.text_content()
    assert dbg(page)["surveyOpen"] is True
    assert events(page, "skip-request") == [], "还没确认就不该发请求"

    page.click('[data-role="survey-skip"]')
    wait_survey_closed(page)
    assert events(page, "skip-done")
    posts = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(posts) == 1 and json.loads(posts[0]["body"]) == {"skipped": True}


# =================================================== 5. 正式模式：首次打开判定

def test_api_first_visit_opens_survey_and_writes_background(page, ar_server, fixture_doc,
                                                            fixture_questions, ar_errors):
    """契约 §12 第 2 条：提交 → 可见成功说明 + 「改写产物要重跑 ar rewrite 才会用上」
    + 同一张卡片换出「让模型追问几个细节（可选）」（契约 §4 的「问一次」）。"""
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {"education": "undergraduate"},
                     "sketch": "读者背景：本科学历。", "degraded": []},
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    debug = dbg(page)
    assert debug["survey"]["openReason"] == "api-first-visit"
    assert debug["surveyQuestionSource"] == "api"
    assert debug["surveyUrl"].endswith("/api/survey")
    # 「首次打开」的判定取服务端画像：确实先问了 GET /api/survey
    assert any(s["method"] == "GET" and s["url"].endswith("/api/survey") for s in seen)

    pick(page, "Q1", "undergraduate")
    pick(page, "Q3a", "economics")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')

    posts = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(posts) == 1, seen
    body = json.loads(posts[0]["body"])
    assert "version" not in body["answers"], "提交体不许带 version（后端只认八个字段槽位）"
    assert body["answers"]["education"] == "undergraduate"
    assert body["answers"]["discipline"] == "economics"
    assert body["answers"]["strengths"] == ["none"]
    assert "high_school_electives" not in body["answers"], "没答的条件题不许写进答案"
    assert "reading_materials" not in body["answers"], "没答的条件题不许写进答案"

    texts = banner_texts(page)
    assert any("背景已保存" in t for t in texts), texts
    assert any("ar rewrite" in t and "重跑" in t for t in texts), texts

    done = events(page, "submit-done")
    assert done and done[-1]["kind"] == "saved"
    assert "已保存" in done[-1]["message"]

    # 提交成功后卡片**不关**：换到「已完成 + 追问入口」这一步（契约 §4 的「问一次」）
    assert dbg(page)["surveyOpen"] is True
    assert dbg(page)["surveyPhase"] == "done"
    assert events(page, "followups-offered")
    expect(page.locator('[data-role="survey-phase-title"]')).to_have_text("背景已保存")
    expect(page.locator('[data-role="survey-followup-start"]')).to_be_visible()
    expect(page.locator('[data-role="survey-submit"]')).to_be_hidden()

    page.click('[data-role="survey-finish"]')
    wait_survey_closed(page)
    assert page.locator('[data-role="survey-layer"]').count() == 0
    assert_no_page_errors(page, ar_errors)


def test_api_submit_failure_keeps_answers_and_says_why(page, ar_server, fixture_doc,
                                                       fixture_questions):
    """提交失败（400）不静默：表单留在原地、答案不丢、状态区写明原因。"""
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_status=400,
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "college")
    pick(page, "Q3a", "engineering")
    pick(page, "Q4", "engineering_tech")
    fill_text(page, "Q3b", "机械工程")
    page.click('[data-role="survey-submit"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_have_attribute("data-state", "error")
    assert "背景没能保存" in status.text_content()
    assert "400" in status.text_content()
    assert dbg(page)["surveyOpen"] is True
    # 答案还在表单里（而不是被清掉）
    assert page.locator('[data-role="survey-question"][data-qid="Q3a"] input:checked').count() == 1
    assert "机械工程" in page.locator('input[data-role="survey-text"][data-qid="Q3b"]').input_value()
    assert events(page, "submit-error")
    assert any(s["method"] == "POST" for s in seen)


def test_api_skipped_profile_suppresses_popup_next_visit(page, ar_server, fixture_doc,
                                                         fixture_questions):
    """已跳过（`skipped: true`）→ 不再弹问卷，也不挂那条「本次不再打扰」的说明横幅
    （2026-10-15 起整条去掉；原因只留在 `__arDebug.surveyBootReason`）。"""
    fake_backend(page, fixture_doc,
                 survey=survey_payload(fixture_questions["questions"], skipped=True))
    open_api(page, ar_server)

    page.wait_for_function(
        "() => globalThis.__arDebug.surveyBootReason === 'skipped-before'", timeout=8000,
    )
    debug = dbg(page)
    assert debug["surveyBootReason"] == "skipped-before"     # 判定照旧只看服务端画像
    assert debug["surveyOpen"] is False
    assert debug["errors"] == []
    assert page.locator('[data-role="survey-layer"]').count() == 0
    # 「不打扰」＝不弹横幅：关于问卷没弹的那两句（含 `?survey=1` 这条出路）一个字都不许挂
    # （夹具里 p016 自带一条配图降级横幅，所以按文案断言，不写「容器为空」）
    texts = banner_texts(page)
    assert not any("?survey=1" in t for t in texts), texts
    assert not any("不再打扰" in t for t in texts), texts
    assert not any("跳过" in t and "按通用读者处理" in t for t in texts), texts


def test_api_answered_profile_suppresses_popup_but_explicit_flag_opens_with_backfill(
    page, ar_server, fixture_doc, fixture_questions,
):
    """已有背景（`answers` 非空）→ 不再自动弹（**也不弹那条说明横幅**）；`?survey=1` 仍可打开并回填已存答案。"""
    fake_backend(page, fixture_doc,
                 survey=survey_payload(fixture_questions["questions"],
                                       answers={"version": 1, "education": "master"}))
    open_api(page, ar_server)
    page.wait_for_function(
        "() => globalThis.__arDebug.surveyBootReason === 'already-answered'", timeout=8000,
    )
    debug = dbg(page)
    assert debug["surveyBootReason"] == "already-answered"
    assert debug["surveyOpen"] is False
    assert debug["errors"] == []
    # 2026-10-15 起「不打扰」＝不弹说明横幅（修好前这里挂着那条含 `?survey=1` 的 warn 横幅；
    # 夹具里 p016 自带一条配图降级横幅，所以按文案断言，不写「容器为空」）
    texts = banner_texts(page)
    assert not any("?survey=1" in t for t in texts), texts
    assert not any("不再打扰" in t for t in texts), texts
    assert not any("已答过" in t for t in texts), texts

    open_api(page, ar_server, extra="&survey=1")
    wait_survey_open(page)
    assert dbg(page)["survey"]["openReason"] == "api-explicit"
    assert page.locator('[data-role="survey-question"][data-qid="Q1"] input:checked').count() == 1
    assert page.locator('[data-role="survey-question"][data-qid="Q1"] input[value="master"]').is_checked()
    # 回填了学历 → 依赖它的 Q2/Q3a/Q3b 要立刻跟着出现（不能停在「全藏」状态）
    for qid in ("Q2", "Q3a", "Q3b"):
        expect(q(page, qid)).to_be_visible()


def test_api_answered_version_triggers_update_notice(page, ar_server, fixture_doc,
                                                     fixture_questions):
    """「问卷已更新」的判定 = `answered_version !== null && answered_version !== version`。

    两个值都取 `GET /api/survey` 顶层；版本号不进浏览器本地存储。
    """
    fake_backend(page, fixture_doc,
                 survey=survey_payload(fixture_questions["questions"],
                                       version=2, answered_version=1))
    open_api(page, ar_server)
    wait_survey_open(page)

    debug = dbg(page)
    assert any(d["kind"] == "survey-version" for d in debug["degradations"])
    assert any("问卷已更新" in t and "v1" in t and "v2" in t for t in banner_texts(page)), \
        banner_texts(page)
    assert page.evaluate("() => Object.keys(window.localStorage)") == ["ar.settings.v1"]


def test_api_same_answered_version_has_no_update_notice(page, ar_server, fixture_doc,
                                                        fixture_questions):
    """`answered_version === version` 时不提示（同版重填不该被吓一跳）。"""
    fake_backend(page, fixture_doc,
                 survey=survey_payload(fixture_questions["questions"],
                                       version=1, answered_version=1))
    open_api(page, ar_server)
    wait_survey_open(page)

    debug = dbg(page)
    assert not any(d["kind"] == "survey-version" for d in debug["degradations"])
    assert not any("问卷已更新" in t for t in banner_texts(page))


def test_api_survey_load_failure_degrades_visibly(page, ar_server, fixture_doc):
    """拿不到题目（GET /api/survey 失败）→ 不弹问卷，但留可见降级，阅读照常。"""
    def handle(route, request):
        if "/api/survey" in request.url:
            route.fulfill(status=500, content_type="application/json", body="{}")
            return
        if "/api/profile" in request.url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"profile": {}, "degraded": []}))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(fixture_doc, ensure_ascii=False))

    page.route("**/api/**", handle)
    open_api(page, ar_server)
    page.wait_for_function(
        "() => globalThis.__arDebug.surveyBootReason === 'load-failed'", timeout=8000,
    )
    debug = dbg(page)
    assert debug["surveyOpen"] is False
    assert any(d["kind"] == "survey-load" for d in debug["degradations"])
    assert any("问卷" in t for t in banner_texts(page))
    assert page.locator("#rewritten .block").count() == 25


# =================================================== 6. 模型追问（契约 §4）

def test_api_followup_offers_entry_click_render_answer_and_save(page, ar_server, fixture_doc,
                                                               fixture_questions):
    """契约 §4 主路径：提交成功后出「让模型追问几个细节（可选）」→ 点它拿 3 问并渲染
    （每题带后端的 hint）→ 作答 → 保存时把 `[{q, a}]` 放进 `followups`（**不进八个槽位**）。"""
    followup_body = {
        "questions": [
            {"q": "你说熟悉经济与金融——读过《资本论》原文吗？", "hint": "读过哪几章 / 只读过二手介绍"},
            {"q": "高中选考物理化学生物，统计课学过吗？", "hint": "学过 / 自学过 / 没学过"},
            {"q": "常读学术论文，读的是哪个领域？", "hint": "经济学 / 其它"},
        ],
        "text": "", "degraded": False, "prompt_version": "survey-v1",
        "model": "deepseek/deepseek-v4.1-flash",
    }
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
        followup_post=followup_body,
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "undergraduate")
    pick(page, "Q3a", "economics")
    pick(page, "Q4", "economics_finance")
    page.click('[data-role="survey-submit"]')
    expect(page.locator('[data-role="survey-followup-start"]')).to_be_visible()
    assert dbg(page)["surveyPhase"] == "done"

    page.click('[data-role="survey-followup-start"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.surveyPhase === 'followup'", timeout=8000,
    )
    assert page.locator('[data-role="survey-followup-item"]').count() == 3
    assert dbg(page)["surveyFollowupQuestions"] == [q["q"] for q in followup_body["questions"]]
    # 每题都带后端给的 hint（不是前端编的）
    assert page.locator('[data-role="survey-followup-hint"]').first.text_content() \
        == f'提示：{followup_body["questions"][0]["hint"]}'
    assert page.locator('[data-role="survey-followup-q"]').nth(1).text_content() \
        == f'2. {followup_body["questions"][1]["q"]}'

    # 只答两题、第三题留空：空答不提交
    texts = page.locator('[data-role="survey-followup-answer"]')
    texts.nth(0).fill("读过第一章")
    texts.nth(1).fill("自学过一点")
    page.click('[data-role="survey-followup-save"]')
    wait_survey_closed(page)

    # 端点分工（2026-10-08 裁决）：`/api/survey/followup` 只生成问题；
    # 保存答案走 `POST /api/survey` 带 `{followups: [...]}`。
    generate = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey/followup")]
    assert len(generate) == 1, seen
    assert json.loads(generate[0]["body"]) == {}, "生成问题这一步的请求体要空"

    saves = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(saves) == 2, seen              # 一次存固定题、一次存追问答案
    fixed_body, save_body = (json.loads(s["body"]) for s in saves)
    assert set(fixed_body) == {"answers"}, fixed_body
    assert save_body == {"followups": [
        {"q": followup_body["questions"][0]["q"], "a": "读过第一章"},
        {"q": followup_body["questions"][1]["q"], "a": "自学过一点"},
    ]}, save_body
    # 追问答案**不许**混进八个槽位字段、也不许带 version（多一个键后端就 400）
    slot_fields = {q["field"] for q in fixture_questions["questions"]}
    assert not (set(save_body) & slot_fields)
    assert set(save_body) == {"followups"}
    assert "version" not in save_body
    assert not (set(save_body["followups"][0]) & slot_fields)

    done = events(page, "followups-done")
    assert done and done[-1]["saved"] is True and len(done[-1]["answers"]) == 2
    assert any("已记进画像" in t for t in banner_texts(page)), banner_texts(page)
    assert page.locator('[data-role="survey-layer"]').count() == 0


def test_api_followup_model_not_configured_degrades_visibly_and_background_kept(
    page, ar_server, fixture_doc, fixture_questions,
):
    """契约 §4/§11：模型未配置 → 200 + `degraded` 文案可见、固定题答案不受影响、这一步可跳过。"""
    degrade_text = ("模型未配置，暂时不能生成追问。设置环境变量 AR_API_KEY，"
                    "或在 config.local.json 里填 api_key，再点一次即可（背景已保存，不受影响）。")
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
        followup_post={"questions": [], "text": degrade_text, "degraded": True,
                       "reason": "model_not_configured", "prompt_version": "survey-v1"},
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "master")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')
    page.click('[data-role="survey-followup-start"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_have_attribute("data-state", "degraded")
    assert "模型未配置" in status.text_content()
    assert page.locator('[data-role="survey-followup-item"]').count() == 0
    assert any(d["kind"] == "survey-followup" for d in dbg(page)["degradations"])
    assert any("模型未配置" in t for t in banner_texts(page)), banner_texts(page)

    # 固定题答案照常保存过（那次 POST 成功），这一步允许整场跳过（不会发带 followups 的保存请求）
    submits = [s for s in seen if s["method"] == "POST" and s["url"].endswith("/api/survey")]
    assert len(submits) == 1 and json.loads(submits[0]["body"])["answers"]["education"] == "master"
    assert not [s for s in seen if "followups" in (s["body"] or "")], "没作答就不该发保存请求"
    page.click('[data-role="survey-finish"]')
    wait_survey_closed(page)
    assert page.locator('[data-role="survey-layer"]').count() == 0


def test_api_followup_http_failure_is_visible_and_does_not_close_the_card(
    page, ar_server, fixture_doc, fixture_questions,
):
    """追问请求本身失败（非 200）也不静默：卡片留在原地、错误可见、可重试或「完成」离开。"""
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
        followup_status=500,
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "doctor")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')
    page.click('[data-role="survey-followup-start"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_have_attribute("data-state", "error")
    assert "追问请求失败" in status.text_content() and "500" in status.text_content()
    assert dbg(page)["surveyOpen"] is True, "追问失败不该把卡片关掉"
    assert events(page, "followup-error")

    page.click('[data-role="survey-finish"]')
    wait_survey_closed(page)
    assert any(s["url"].endswith("/api/survey/followup") for s in seen)


def test_api_followup_answer_save_failure_keeps_text(page, ar_server, fixture_doc,
                                                     fixture_questions):
    """存追问答案失败时**不关卡片**：刚敲的字还在，可以重试或「不用了」离开。"""
    body = {"questions": [{"q": f"追问 {i}", "hint": "随便说说"} for i in range(1, 4)],
            "text": "", "degraded": False, "prompt_version": "survey-v1"}
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
        followup_post=body,
        followup_save_status=500,          # 取问题成功，存答案失败
    )
    open_api(page, ar_server)
    wait_survey_open(page)
    pick(page, "Q1", "college")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')
    page.click('[data-role="survey-followup-start"]')
    page.wait_for_function("() => globalThis.__arDebug.surveyPhase === 'followup'", timeout=8000)

    page.locator('[data-role="survey-followup-answer"]').first.fill("一段没保存成功的回答")
    page.click('[data-role="survey-followup-save"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_have_attribute("data-state", "error")
    assert "没能保存" in status.text_content()
    assert dbg(page)["surveyOpen"] is True
    assert page.locator('[data-role="survey-followup-answer"]').first.input_value() == "一段没保存成功的回答"
    assert events(page, "followups-save-error")
    # 那一次保存打的是 `POST /api/survey`（带 followups），不是 `/api/survey/followup`
    failed_saves = [s for s in seen if s["method"] == "POST"
                    and s["url"].endswith("/api/survey") and "followups" in (s["body"] or "")]
    assert len(failed_saves) == 1 and json.loads(failed_saves[0]["body"])["followups"][0]["a"] \
        == "一段没保存成功的回答"

    # 「不用了」可以离开（固定题的背景不受影响）
    page.click('[data-role="survey-followup-skip"]')
    wait_survey_closed(page)
    assert any(s["method"] == "POST" for s in seen)


def test_fixture_submit_finishes_without_followup_entry(page, ar_server, ar_errors):
    """夹具模式提交后没有追问入口（那一步需要后端）：留可断言的说明，不发任何 /api 请求。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    pick(page, "Q1", "master")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')
    wait_survey_closed(page)
    assert not events(page, "followups-offered")
    assert events(page, "followups-unavailable")
    assert any("夹具模式不保存" in t for t in banner_texts(page))
    assert_no_page_errors(page, ar_errors)


def test_fixture_sample_renders_into_the_followup_phase(page, ar_server):
    """夹具样例（`survey-followup.json`）能按后端响应形状渲染成追问输入区（每题带 hint）。

    夹具模式提交后没有追问阶段（那一步要后端），所以这里用动态 import 取到模块的
    `renderFollowupPhase`，把样例**直接**喂给同一条渲染路径——夹具模式与正式模式共用它。
    """
    sample = json.loads((FIXTURE_DIR / "survey-followup.json").read_text(encoding="utf-8"))
    assert 3 <= len(sample["questions"]) <= 5, "样例要是 3–5 问（契约 §4）"
    assert all(item["q"] and item["hint"] for item in sample["questions"])

    open_fixture(page, ar_server)
    wait_survey_open(page)

    page.evaluate(
        """async (questions) => {
            const mod = await import('./js/survey.js');
            const card = document.querySelector('[data-role="survey-card"]');
            mod.renderFollowupPhase(card, questions);
        }""",
        sample["questions"],
    )

    rows = page.locator('[data-role="survey-followup-item"]')
    assert rows.count() == len(sample["questions"])
    assert page.locator('[data-role="survey-followup-q"]').first.text_content() \
        == f'1. {sample["questions"][0]["q"]}'
    assert page.locator('[data-role="survey-followup-hint"]').first.text_content() \
        == f'提示：{sample["questions"][0]["hint"]}'
    assert page.locator('[data-role="survey-followup-answer"]').count() == len(sample["questions"])
    assert dbg(page)["surveyPhase"] == "followup"
    expect(page.locator('[data-role="survey-followup-save"]')).to_be_visible()
    expect(page.locator('[data-role="survey-skip"]')).to_be_hidden()

    # 同一份样例也走一遍「真实取数」那条路：api.js 的 loadFixtureFollowups 读的就是这个文件
    loaded = page.evaluate(
        """async () => {
            const api = await import('./js/api.js');
            const { payload, url } = await api.loadFixtureFollowups({ mode: 'fixture', sourceId: 'capital-v1-ch01' });
            return { url, questions: payload.questions };
        }""",
    )
    assert loaded["url"].endswith("/samples/capital-v1-ch01/survey-followup.json")
    assert loaded["questions"] == sample["questions"]


def test_skip_failure_keeps_the_form_and_says_it_will_ask_again(page, ar_server, fixture_doc,
                                                               fixture_questions):
    """跳过没落盘时**不关卡片**：可以再点一次重试，并写明「下次打开还会再问你」（上级裁决）。"""
    seen = fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_status=500,                 # 跳过那一次也回 500
    )
    open_api(page, ar_server)
    wait_survey_open(page)

    page.click('[data-role="survey-skip"]')

    status = page.locator('[data-role="survey-status"]')
    expect(status).to_have_attribute("data-state", "error")
    text = status.text_content()
    assert "跳过没有保存成功" in text
    assert "再点一次" in text
    assert "下次打开" in text and "还会再问" in text
    assert dbg(page)["surveyOpen"] is True, "没落盘就不该关掉表单"
    assert dbg(page)["surveyPhase"] == "questions"
    assert page.locator('[data-role="survey-skip"]').is_visible()
    assert events(page, "skip-error")
    assert not events(page, "skip-done")
    assert any(s["method"] == "POST" for s in seen)


# =================================================== 7. 不挡阅读 / 键盘 / 残留

def test_open_survey_does_not_block_scrolling_or_swallow_clicks(page, ar_server):
    """问卷打开时正文照常可滚、可点：层与卡片的空白都不吃指针事件（契约 §9）。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    assert page.eval_on_selector(
        '[data-role="survey-layer"]', "el => getComputedStyle(el).pointerEvents"
    ) == "none", "遮罩层不许吞正文点击"
    # 卡片整体也不吃事件（否则卡片范围内的正文点击会进死区），只有里面的控件收事件
    assert page.eval_on_selector(
        '[data-role="survey-card"]', "el => getComputedStyle(el).pointerEvents"
    ) == "none"
    assert page.eval_on_selector(
        '[data-role="survey-lead"]', "el => getComputedStyle(el).pointerEvents"
    ) == "none"
    assert page.eval_on_selector(
        'input[data-qid="Q1"]', "el => getComputedStyle(el).pointerEvents"
    ) == "auto"
    assert page.eval_on_selector(
        '[data-role="survey-submit"]', "el => getComputedStyle(el).pointerEvents"
    ) == "auto"

    # 卡片正中（标题与说明之间）这一点的命中测试要落回正文，而不是卡片
    probe = page.evaluate(
        """() => {
            const card = document.querySelector('[data-role="survey-card"]');
            const r = card.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + 6);
            return hit ? (hit.closest('[data-role="survey-card"]') ? 'card' : 'page') : 'none';
        }""",
    )
    assert probe == "page", "卡片上方那一条不许成为点击死区"

    # 卡片下方（仍在层内）滚轮照常滚正文
    box = page.locator('[data-role="survey-card"]').bounding_box()
    page.mouse.move(30, box["y"] + box["height"] + 40)
    before = page.evaluate("() => window.scrollY")
    page.mouse.wheel(0, 600)
    page.wait_for_function("(y) => window.scrollY > y", arg=before, timeout=5000)

    # 点正文段落照样能收到事件；问卷不因此关闭，也不顺带弹讲解卡片
    page.evaluate("() => window.scrollTo(0, 0)")
    page.click("#blk-p002 [data-role='block-text']")
    assert dbg(page)["surveyOpen"] is True
    assert dbg(page)["cardOpen"] is False


def test_wheel_over_survey_questions_scrolls_only_the_question_area(page, ar_server):
    """指针落在问卷卡片上滚动：只有题区滚，正文一动不动；停在边界也不再带动正文（契约 §9）。

    回归缺陷（2026-10-09 用户实测「背景问卷的滚动会穿透」）：题区是卡片里唯一的滚动容器，
    却跟标题、说明一起被置了 `pointer-events: none`（那一组不能动，否则卡片这块矩形会吃掉
    正文点击）。命中测试跳过 `pointer-events: none` 的元素 ⇒ 指针落在题区的空白/间隙上时
    滚轮直接落到背后正文上，题区自己永远收不到滚轮；滚到边界后正文接着跑。
    修法见 `web/js/survey.js` 的 `handleWheel`（文档捕获阶段 + 坐标命中判定）。

    这一条**自己把题区压成可滚的**：夹具这版题区刚好不溢出（`scrollHeight == clientHeight`），
    滚轮本来也无处可滚。压成 60px 之后「滚轮落在题区上」才真正成立。

    另一处口径：真实滚轮事件（`page.mouse.wheel`）在题区上有时会命中题区里的控件行，
    那样原生滚动也会生效、测不出「是不是 JS 拦下的」。所以「指针落在题区空白/间隙上」
    这一档用**派生事件**盖（`dispatchEvent` 的返回值＝有没有 `preventDefault()`），
    真实滚轮则负责「题区滚 / 正文不动 / 边界不穿」这条轨迹。
    """
    open_fixture(page, ar_server)
    wait_survey_open(page)

    body = page.locator('[data-role="survey-body"]')
    assert body.count() == 1
    body.evaluate("(el) => { el.style.maxHeight = '60px'; }")
    page.wait_for_timeout(120)         # 让 max-height 的重排落定，再量几何

    geometry = page.evaluate(
        """() => {
            const card = document.querySelector('[data-role="survey-card"]');
            const body = document.querySelector('[data-role="survey-body"]');
            const c = card.getBoundingClientRect();
            const b = body.getBoundingClientRect();
            return {
                bodyX: b.left + b.width / 2,
                bodyY: b.top + b.height / 2,
                cardX: c.left + 40,
                cardY: c.top + 6,          // 卡片顶部那一条（标题行，契约要求点击穿过去）
                maskX: Math.max(1, c.left - 8),
                maskY: Math.min(c.bottom + 40, window.innerHeight - 5),
                overscroll: getComputedStyle(body).overscrollBehaviorY,
                maxScroll: (() => {
                    body.scrollTop = 1e5;
                    const max = body.scrollTop;
                    body.scrollTop = 0;
                    return max;
                })(),
            };
        }"""
    )
    assert geometry["overscroll"] == "contain", "题区要挡住滚到边界后的滚动链"
    assert geometry["maxScroll"] > 20, f"题区可滚距离太小（{geometry['maxScroll']}px），测不出东西"
    assert page.evaluate(
        """([x, y]) => Boolean(document.elementFromPoint(x, y)
             .closest('[data-role="survey-card"]'))""",
        [geometry["cardX"], geometry["cardY"]],
    ) is False, "卡片顶部那条不许成为点击死区（契约要求正文照常可点）"

    page.evaluate("() => { window.scrollTo(0, 0); }")

    def snapshot() -> dict:
        return page.evaluate(
            """() => {
                const body = document.querySelector('[data-role="survey-body"]');
                return { bodyTop: body.scrollTop, windowY: window.scrollY };
            }"""
        )

    def wheel(x: float, y: float, dy: int, times: int = 1) -> None:
        """滚轮一律显式先 move 到落点再滚（鼠标位置是这条用例唯一的变量，要确定）。"""
        page.mouse.move(x, y)
        for _ in range(times):
            page.mouse.wheel(0, dy)
            page.wait_for_timeout(60)
        page.wait_for_timeout(150)

    # 断言一次报全（`assert` 会卡在第一条上，只看得到「题区没滚」这半个症状）
    problems: list[str] = []

    # ① 题区正中往下滚：题区滚，正文不动
    wheel(geometry["bodyX"], geometry["bodyY"], 60)
    after = snapshot()
    if after["bodyTop"] <= 0:
        problems.append("滚轮落在题区上，题区自己没跟着滚（滚轮被别处收走了）")
    if after["windowY"] != 0:
        problems.append(f"题区滚动带动了背后正文：window.scrollY = {after['windowY']}")

    # ② 一路滚到底、再继续滚：正文仍然一动不动
    wheel(geometry["bodyX"], geometry["bodyY"], 600, times=3)
    at_bottom = snapshot()
    if at_bottom["bodyTop"] < geometry["maxScroll"] - 1:
        problems.append(
            f"题区该滚到底了，实际 scrollTop = {at_bottom['bodyTop']}"
            f"（上限 {geometry['maxScroll']}）"
        )
    if at_bottom["windowY"] != 0:
        problems.append(f"题区滚到底后继续滚带动了正文：window.scrollY = {at_bottom['windowY']}")

    # ③ 反方向：滚到顶之后继续往上滚，正文同样不许动
    wheel(geometry["bodyX"], geometry["bodyY"], -600, times=3)
    at_top = snapshot()
    if at_top["bodyTop"] != 0:
        problems.append(f"题区该滚回顶端了，实际 scrollTop = {at_top['bodyTop']}")
    if at_top["windowY"] != 0:
        problems.append(f"题区滚到顶后继续滚带动了正文：window.scrollY = {at_top['windowY']}")

    # ④ 卡片上非题区的位置（标题行）：滚轮也归问卷，正文不许被带走
    wheel(geometry["cardX"], geometry["cardY"], 600)
    on_header = snapshot()
    if on_header["windowY"] != 0:
        problems.append(f"指针在卡片标题行上滚动带动了正文：window.scrollY = {on_header['windowY']}")

    # ⑤ 「指针正好落在题区的**空白/间隙**上」这一种也要盖住：那时事件目标就是题区自己
    #    （`pointer-events: none` → 命中测试没有元素可给，`event.target` 直接是题区）。
    #    派生的滚轮事件标 `cancelable`，这样 `dispatchEvent` 的返回值就是
    #    「`handleWheel` 有没有拦下默认动作」——用户在题区间隙上滚动时走的正是这一步。
    page.evaluate("() => { document.querySelector('[data-role=\"survey-body\"]').scrollTop = 0; }")
    landed = page.evaluate(
        """() => {
            const body = document.querySelector('[data-role="survey-body"]');
            const r = body.getBoundingClientRect();
            const event = new WheelEvent('wheel', {
                deltaY: 40, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                bubbles: true, cancelable: true,
            });
            const notCanceled = body.dispatchEvent(event);   // false ＝ 默认动作被拦下
            return { notCanceled, bodyTop: body.scrollTop };
        }"""
    )
    if landed["notCanceled"]:
        problems.append("滚轮落在题区上却没被拦下（默认动作照旧滚正文）")
    if landed["bodyTop"] != 40:
        problems.append(f"题区没按滚轮量滚动：scrollTop = {landed['bodyTop']}（期望 40）")

    assert problems == [], "问卷题区滚动缺陷：\n- " + "\n- ".join(problems)

    # ⑤ 遮罩层上（卡片之外）滚轮：正文照常滚 —— 既有契约，修复不许破
    page.mouse.move(geometry["maskX"], geometry["maskY"])
    before = page.evaluate("() => window.scrollY")
    page.mouse.wheel(0, 600)
    page.wait_for_function("(y) => window.scrollY > y", arg=before, timeout=5000)

    # ⑥ 控件照旧可点（滚轮拦截不许把题区变成点不动）
    page.evaluate("() => window.scrollTo(0, 0)")
    page.locator('input[data-qid="Q1"]').first.check()
    assert page.locator('input[data-qid="Q1"]').first.is_checked()
    assert dbg(page)["surveyOpen"] is True


def test_wheel_over_survey_textarea_scrolls_it_and_never_the_page(page, ar_server):
    """指针压在问卷的作答框（`<textarea>`）上滚动：**先让 textarea 自己滚**，滚到头才轮到题区；
    全程 `window.scrollY` 恒 0（契约 §9 的不变量）。

    回归（2026-10-09 复核抓到）：第一版 `handleWheel` 对 `textarea` 是「先 `preventDefault()`
    再 return」，而 `preventDefault()` 取消的是**整条链的默认滚动动作** —— textarea 里
    放不下的长文本就滚不动了（只能拖滚动条 / 用键盘），修复前它是能滚的。
    固定题里 Q3b / Q4b 都是 `rows=3` 的 `<textarea>`，答案长一点就会溢出，所以这条不是冷门路径。

    造现场的三个动作（都在测试里做完，页面代码没有测试后门）：
      - 题区压到 90px ⇒ 它自己成了滚动区，而且能整块容下 63px 高的 textarea；
      - 题区尾部插一个 220px 的占位块 ⇒ 「textarea 滚不动之后归题区」有地方可滚，量得到；
      - Q4b 填 60 行长文本 ⇒ textarea 自己可滚（可滚距离 ≈ 969px）。
    """
    open_fixture(page, ar_server)
    wait_survey_open(page)

    page.evaluate(
        """() => {
            const body = document.querySelector('[data-role="survey-body"]');
            body.style.maxHeight = '90px';
            const spacer = document.createElement('div');
            spacer.dataset.role = 'test-spacer';
            spacer.style.height = '220px';
            body.appendChild(spacer);
        }"""
    )
    note = page.locator('[data-role="survey-note"][data-qid="Q4b"]')
    assert note.count() == 1, "Q4b(补充说明) 该是一个 textarea"
    assert note.evaluate("(el) => el.tagName") == "TEXTAREA"
    note.evaluate(
        """(el) => {
            el.value = Array.from({ length: 60 },
                (_, i) => `第 ${i + 1} 行：这是一段一屏放不下的作答`).join('\\n');
        }"""
    )
    note.scroll_into_view_if_needed()
    page.wait_for_timeout(150)

    geometry = page.evaluate(
        """() => {
            const body = document.querySelector('[data-role="survey-body"]');
            const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
            const card = document.querySelector('[data-role="survey-card"]');
            const b = body.getBoundingClientRect();
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2;
            const y = r.top + r.height / 2;
            const hit = document.elementFromPoint(x, y);
            return {
                x, y,
                hitIsTextarea: hit === el,
                hitDesc: hit
                    ? `${hit.tagName}[${hit.dataset ? (hit.dataset.qid || hit.dataset.role) : ''}]`
                    : 'none',
                textMax: el.scrollHeight - el.clientHeight,
                inView: r.top >= b.top - 1 && r.bottom <= b.bottom + 1,
                bodyTop: body.scrollTop,
                bodyMax: body.scrollHeight - body.clientHeight,
                cardTop: card.getBoundingClientRect().top,
            };
        }"""
    )
    assert geometry["hitIsTextarea"], (
        f"指针该压在 textarea 上（它是 `pointer-events: auto` 的控件），实际命中 {geometry['hitDesc']}"
    )
    assert geometry["inView"], "textarea 要整个落在题区的可见窗口里，否则滚轮打不到它"
    assert geometry["textMax"] > 40, f"textarea 得真的能滚（可滚 {geometry['textMax']}px），否则测不出东西"
    assert geometry["bodyMax"] - geometry["bodyTop"] > 80, (
        f"占位块没给出「题区接手」的余量：bodyTop {geometry['bodyTop']} / bodyMax {geometry['bodyMax']}"
    )

    def snapshot() -> dict:
        return page.evaluate(
            """() => {
                const body = document.querySelector('[data-role="survey-body"]');
                const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
                return { textTop: el.scrollTop, textMax: el.scrollHeight - el.clientHeight,
                         bodyTop: body.scrollTop, windowY: window.scrollY };
            }"""
        )

    def wheel(dy: int, times: int = 1) -> None:
        page.mouse.move(geometry["x"], geometry["y"])
        for _ in range(times):
            page.mouse.wheel(0, dy)
            page.wait_for_timeout(60)
        page.wait_for_timeout(150)

    page.evaluate(
        """() => {
            window.scrollTo(0, 0);
            document.querySelector('[data-role="survey-note"][data-qid="Q4b"]').scrollTop = 0;
        }"""
    )
    body_start = snapshot()["bodyTop"]

    problems: list[str] = []

    # ① 往下滚：textarea 自己滚（原生滚动不许被 preventDefault 取消），题区与正文都不动
    wheel(60)
    after = snapshot()
    if after["textTop"] <= 0:
        problems.append("指针压在 textarea 上，textarea 自己没滚（它的原生滚动被 preventDefault 取消了）")
    if after["windowY"] != 0:
        problems.append(f"textarea 滚动带动了背后正文：window.scrollY = {after['windowY']}")
    if after["bodyTop"] != body_start:
        problems.append(f"textarea 还能滚时题区不该跟着动：bodyTop {body_start} → {after['bodyTop']}")

    # ② 反过来往上滚：同样归 textarea
    wheel(-60)
    back = snapshot()
    if back["textTop"] != 0:
        problems.append(f"textarea 该滚回顶端了，实际 scrollTop = {back['textTop']}")
    if back["windowY"] != 0 or back["bodyTop"] != body_start:
        problems.append(
            f"textarea 往上滚时别人动了：window.scrollY = {back['windowY']},"
            f" bodyTop {body_start} → {back['bodyTop']}"
        )

    # ②b 「textarea 还能滚」这一档**不许拦**：拦了就等于抢走它的原生滚动（不同浏览器对
    #     `preventDefault()` 会不会连控件自己的滚动一起取消，行为并不一致，所以这里量的是
    #     我们自己的判定：事件**没有**被 `preventDefault()`）。
    left_to_browser = page.evaluate(
        """() => {
            const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
            el.scrollTop = 0;                          // 还能往下滚；题区保持原位（textarea 仍可见）
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + 5, r.top + 5);
            const event = new WheelEvent('wheel', {
                deltaY: 40, clientX: r.left + 5, clientY: r.top + 5,
                bubbles: true, cancelable: true,
            });
            const notCanceled = el.dispatchEvent(event);   // true ＝ 没拦，留给 textarea 自己滚
            return { notCanceled, textTop: el.scrollTop, hitIsTextarea: hit === el };
        }"""
    )
    assert left_to_browser["hitIsTextarea"], "前置条件坏了：这一档的坐标不在 textarea 可见处"
    if left_to_browser["notCanceled"] is False:
        problems.append("textarea 还能滚，滚轮却被 preventDefault 拦下了（它的原生滚动被抢走）")

    # ③ textarea 滚不动时要**归题区**（不是被吞掉、更不能落到正文）：派生事件直接打在
    #    textarea 上（坐标取它可见的矩形，与真实指针位置同口径 —— 题区保持在 textarea 可见
    #    的滚动位置，这样坐标落在卡片矩形里），`dispatchEvent` 的返回值 ＝ 有没有被
    #    `preventDefault()`。两档：已在底 / 本来就没溢出。
    for scenario, value in (("已在底", "long"), ("本来没溢出", "short")):
        handed = page.evaluate(
            """([value, bodyStart]) => {
                const body = document.querySelector('[data-role="survey-body"]');
                const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
                if (value === 'short') el.value = '一行字';
                body.scrollTop = bodyStart;               // 题区停在 textarea 可见的位置
                el.scrollTop = 1e5;                       // 已在底；短文本时天然是 0
                const r = el.getBoundingClientRect();
                window.scrollTo(0, 0);
                const event = new WheelEvent('wheel', {
                    deltaY: 80, clientX: r.left + 5, clientY: r.top + 5,
                    bubbles: true, cancelable: true,
                });
                const notCanceled = el.dispatchEvent(event);   // false ＝ 默认动作被拦下
                return { notCanceled, bodyTop: body.scrollTop, textTop: el.scrollTop,
                         textMax: el.scrollHeight - el.clientHeight, windowY: window.scrollY };
            }""",
            [value, body_start],
        )
        if handed["notCanceled"]:
            problems.append(f"textarea（{scenario}）上滚轮没被拦下：默认动作会接着滚正文")
        if handed["bodyTop"] <= body_start:
            problems.append(
                f"textarea（{scenario}）滚不动时，这一下滚轮该归题区："
                f"bodyTop 没动（{body_start} → {handed['bodyTop']}）"
            )
        if handed["windowY"] != 0:
            problems.append(f"textarea（{scenario}）这一下带动了正文：window.scrollY = {handed['windowY']}")

    # ④ 真实滚轮一路滚到底：textarea 到上限，正文始终一动不动
    #    （textarea 满之后剩下的滚动量由题区接手，题区把 textarea 滚出可见窗口是正常的，
    #     所以这一档只断言 textarea 到顶 + 正文不动，不再指望指针还停在它上面）
    page.evaluate(
        """() => {
            const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
            el.value = Array.from({ length: 60 },
                (_, i) => `第 ${i + 1} 行：这是一段一屏放不下的作答`).join('\\n');
            el.scrollTop = 0;
            document.querySelector('[data-role="survey-body"]').scrollTop = 0;
            el.scrollIntoView({ block: 'nearest' });
            window.scrollTo(0, 0);
        }"""
    )
    page.wait_for_timeout(150)
    geometry = page.evaluate(
        """() => {
            const el = document.querySelector('[data-role="survey-note"][data-qid="Q4b"]');
            const r = el.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                     hitIsTextarea: document.elementFromPoint(r.left + r.width / 2,
                                                              r.top + r.height / 2) === el };
        }"""
    )
    if not geometry["hitIsTextarea"]:
        problems.append("复位之后指针没回到 textarea 上（用例自身的前置条件坏了）")
    else:
        wheel(600, times=4)
        at_bottom = snapshot()
        if at_bottom["textTop"] < at_bottom["textMax"] - 1:
            problems.append(
                f"textarea 该滚到底了，实际 scrollTop = {at_bottom['textTop']}"
                f"（上限 {at_bottom['textMax']}）"
            )
        if at_bottom["windowY"] != 0:
            problems.append(f"textarea 滚到底后继续滚带动了正文：window.scrollY = {at_bottom['windowY']}")

    assert problems == [], "问卷 textarea 滚动缺陷：\n- " + "\n- ".join(problems)


def test_survey_close_button_and_layer_cleanup(page, ar_server):
    """× 与「跳过」同效；关闭后层被移除、焦点回正文、正文可读（无残影）。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    page.click('[data-role="survey-close"]')
    wait_survey_closed(page)
    assert events(page, "skip-done")[-1]["reason"] == "close"
    assert page.locator('[data-role="survey-layer"]').count() == 0
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "rewritten"
    assert page.locator("#rewritten .block").count() == 25


def test_survey_is_keyboard_reachable(page, ar_server):
    """键盘可达：打开时焦点落在表单里，Tab 能一路走到「跳过 / 保存背景」。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)

    focused = page.evaluate(
        "() => Boolean(document.activeElement"
        " && document.activeElement.closest('[data-role=\"survey-card\"]'))"
    )
    assert focused, "打开问卷时焦点要进入表单"

    seen = set()
    for _ in range(40):
        page.keyboard.press("Tab")
        seen.add(page.evaluate(
            "() => document.activeElement"
            " && (document.activeElement.dataset.role || document.activeElement.id"
            " || document.activeElement.value)"
        ))
    assert seen, "Tab 走不动"
    assert "survey-submit" in seen and "survey-skip" in seen, f"Tab 到不了问卷按钮：{seen}"


def test_fixture_reload_does_not_reopen_without_the_flag(page, ar_server):
    """夹具模式再来一次仍按 URL 开关决定（没有服务端画像可判）——行为可预期、不静默。"""
    open_fixture(page, ar_server)
    wait_survey_open(page)
    page.click('[data-role="survey-skip"]')
    wait_survey_closed(page)

    open_fixture(page, ar_server, extra="")
    debug = dbg(page)
    assert debug["surveyOpen"] is False
    assert debug["surveyBootReason"] == "fixture-mode"
    assert debug["surveyEvents"] == []
