"""HTTP 层：``/api/*`` JSON 接口 + 托管 ``web/`` 静态目录（契约 §4）。

接口逐字按下表实现（前端只依赖这些）：

===========================  ==================================================
``GET  /``                    托管 ``web/index.html``
``GET  /api/doc/<source_id>`` ``{ meta, rewritten, changes, concepts, degraded }``
``GET  /api/profile``         ``{ profile, degraded }``（默认读者 local-default）
``POST /api/profile/rating``  ``{ concept_id, state, note? }`` → 更新画像并写回 YAML
``POST /api/explain``         ``{ source_id, paragraph, kind, selection?, selection_from? }``
                              → ``{ text, degraded, scope?, cached?, selection_truncated?, usage? }``
``GET  /api/health``          ``{ ok, model_configured, ... }``
===========================  ==================================================

约定：

- 每个响应都带 ``degraded``（或 ``degraded`` 字段）：缺失的产物、未配置的模型都会在这里说明，
  **不允许静默空白**。
- ``web/`` 目录可能还不存在（前端由另一路并行开发）：静态托管容忍目录缺失，
  只回一个写明原因的 404，不崩、不影响 ``/api/*``。
- ``/api/explain`` 在模型未配置或调用失败时返回 200 + ``degraded: true`` + 说明文本，
  让前端能直接把这段说明渲染进卡片。
- ``/api/explain`` 的追加字段分两类：**请求侧**的 ``scope``（``paragraph`` / ``selection``）与
  ``selection_truncated``（选中片段超 600 字符被截断）每次响应都给；**产出侧**的 ``cached``
  （进程内缓存命中，见 ``ar.cache``）与 ``usage``（本次模型调用的原始 token 账，含前缀缓存命中
  ``prompt_tokens_details.cached_tokens``；没调模型 / 缓存命中时是 ``{}``）只在成功响应里出现。
  旧键（``text`` / ``degraded`` / ``model`` / ``prompt_version`` / 失败时的 ``reason``）逐字不变、
  枚举不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .cache import ExplainCache
from .config import Config
from .errors import InputError
from .explain import aexplain
from .jsonio import read_json_if_exists, read_text_if_exists
from .model import ModelClient
from .paths import (
    DEFAULT_DATA_DIR,
    DEFAULT_PROFILES_DIR,
    DEFAULT_READER,
    DEFAULT_SAMPLES_DIR,
    DEFAULT_WEB_DIR,
    doc_dir,
)
from .profile import apply_rating, load_profile, save_profile
from .prompts import DEFAULT_SELECTION_FROM, SELECTION_SOURCES

__all__ = ["build_app"]


class _TolerantStaticFiles(StaticFiles):
    """静态托管：目录不存在时回带原因的 404，而不是抛 ``RuntimeError``。

    Starlette 的 ``StaticFiles`` 即便用 ``check_dir=False`` 建，也会在**每个请求**上跑
    ``check_config()``，目录一旦不存在就抛 ``RuntimeError``（500 并打断整个应用）。
    前端 ``web/`` 由另一路并行开发、随时可能出现，所以这里在进入父类逻辑前先检查一次：
    目录不存在就回可见的 404；目录后来出现了，无需重启也能照常托管。
    """

    def __init__(self, *, web_dir: Path) -> None:
        self._web_dir = web_dir
        super().__init__(directory=str(web_dir), html=True, check_dir=False)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self._web_dir.is_dir():
            response = _json(
                {
                    "error": "前端静态目录不存在，静态文件暂时无法托管",
                    "status": 404,
                    "web_dir": str(self._web_dir),
                    "hint": "前端（web/）由另一路（W2）提供；/api/* 不受影响",
                },
                status_code=404,
            )
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - 客户端给了坏 JSON，回 400 而不是 500
        raise InputError(f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise InputError("请求体必须是 JSON 对象")
    return body


def build_app(
    *,
    data_dir: Path | str = DEFAULT_DATA_DIR,
    profiles_dir: Path | str = DEFAULT_PROFILES_DIR,
    web_dir: Path | str = DEFAULT_WEB_DIR,
    samples_dir: Path | str | None = DEFAULT_SAMPLES_DIR,
    config: Config | None = None,
    reader: str = DEFAULT_READER,
    client_factory: Callable[[], Any] | None = None,
    explain_cache: ExplainCache | None = None,
) -> Starlette:
    """组装应用。测试可注入 ``client_factory``（返回带 ``achat`` 的假客户端）。

    ``explain_cache`` 默认**每个应用一个**（容量 128 的进程内 LRU）：一次进程内所有请求共用，
    进程重启即空；测试可以注入自己的实例来观察/清空缓存。
    """
    config = config or Config()
    data_root = Path(data_dir)
    profiles_root = Path(profiles_dir)
    web_root = Path(web_dir)
    samples_root = Path(samples_dir) if samples_dir is not None else None
    cache = explain_cache if explain_cache is not None else ExplainCache()

    def make_client() -> Any:
        if client_factory is not None:
            return client_factory()
        return ModelClient(config)

    # ------------------------------------------------------------------ 静态 --
    async def index(request: Request) -> Response:
        index_path = web_root / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html; charset=utf-8")
        return _json(
            {
                "error": "web/index.html 不存在，前端静态文件还没生成",
                "web_dir": str(web_root),
                "hint": "前端由另一路（W2）提供；后端接口不受影响，可直接访问 /api/health",
            },
            status_code=404,
        )

    # -------------------------------------------------------------------- API --
    async def health(request: Request) -> Response:
        return _json(
            {
                "ok": True,
                "model_configured": bool(config.model_configured),
                "model": config.model,
                "api_key_origin": config.api_key_origin,
                "web_static": (web_root / "index.html").is_file(),
            }
        )

    async def get_doc(request: Request) -> Response:
        source_id = request.path_params["source_id"]
        target = doc_dir(data_root, source_id)
        meta = read_json_if_exists(target / "source.meta.json")
        degraded: list[str] = []
        if meta is None:
            return _json(
                {
                    "error": f"找不到材料 {source_id}：{target / 'source.meta.json'} 不存在",
                    "hint": f"先运行 `uv run ar ingest <文件> --id {source_id}`",
                    "degraded": [f"材料 {source_id} 尚未摄取"],
                },
                status_code=404,
            )

        rewritten = read_text_if_exists(target / "rewritten.md")
        if rewritten is None:
            degraded.append("rewritten.md 不存在：改写（M3）尚未实现，主栏应显示原文并标「未改写」")
        changes = read_json_if_exists(target / "changes.json")
        if changes is None:
            degraded.append("changes.json 不存在：没有逐段变更记录，段落标记只能显示「未改写」")
        concepts = read_json_if_exists(target / "concepts.json")
        if concepts is None:
            degraded.append("concepts.json 不存在：概念清单未生成或抽取失败，当前为「无画像」模式")

        return _json(
            {
                "meta": meta,
                "rewritten": rewritten,
                "changes": changes,
                "concepts": concepts,
                "degraded": degraded,
            }
        )

    async def get_profile(request: Request) -> Response:
        profile, notes = load_profile(profiles_root, reader)
        return _json({"profile": profile, "degraded": notes})

    async def post_rating(request: Request) -> Response:
        body = await _json_body(request)
        concept_id = body.get("concept_id")
        state = body.get("state")
        note = body.get("note")
        if not isinstance(concept_id, str) or not concept_id.strip():
            raise InputError("concept_id 必须是非空字符串")
        if not isinstance(state, str):
            raise InputError("state 必须是字符串（known / partial / unknown）")
        if note is not None and not isinstance(note, str):
            raise InputError("note 必须是字符串")
        profile, _notes = load_profile(profiles_root, reader)
        updated = apply_rating(profile, concept_id.strip(), state, note)
        save_profile(profiles_root, reader, updated)
        return _json({"ok": True, "profile": updated, "degraded": []})

    async def post_explain(request: Request) -> Response:
        body = await _json_body(request)
        source_id = body.get("source_id")
        paragraph = body.get("paragraph")
        kind = body.get("kind") or "explain"
        selection = body.get("selection")
        selection_from = body.get("selection_from")
        if not isinstance(source_id, str) or not source_id.strip():
            raise InputError("source_id 必须是非空字符串")
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise InputError("paragraph 必须是非空字符串（段 id，如 p012）")
        if not isinstance(kind, str):
            raise InputError("kind 必须是字符串（explain / simpler / detail）")
        if selection is not None and not isinstance(selection, str):
            raise InputError("selection 必须是字符串（页面上选中的那段文字，可省略）")
        if selection_from is None or (isinstance(selection_from, str) and not selection_from.strip()):
            selection_from = DEFAULT_SELECTION_FROM  # 可选字段：缺省 / 空串都按默认值处理
        elif not isinstance(selection_from, str) or selection_from.strip() not in SELECTION_SOURCES:
            raise InputError(
                f"selection_from 只能是 {' / '.join(SELECTION_SOURCES)}（默认 {DEFAULT_SELECTION_FROM}）"
            )

        outcome = await aexplain(
            source_id.strip(),
            paragraph.strip(),
            kind.strip(),
            selection=selection,
            selection_from=selection_from.strip(),
            data_dir=data_root,
            profiles_dir=profiles_root,
            reader=reader,
            client=make_client(),
            cache=cache,
        )
        payload: dict[str, Any] = {"text": outcome.text, "degraded": outcome.degraded}
        if outcome.reason:
            payload["reason"] = outcome.reason
        # scope 与 selection_truncated 描述的是**这次请求要什么**（整段还是片段、片段有没有被截断），
        # 与模型是否可用无关，所以每次响应都给（前端拿 scope 确认「确实按片段讲的」）。
        payload["scope"] = outcome.scope
        if outcome.selection_truncated:
            payload["selection_truncated"] = True
        if not outcome.degraded:
            # 下面几个描述的是**这次产出了什么**：失败/降级时一律不出现
            payload["model"] = outcome.model
            payload["prompt_version"] = outcome.prompt_version
            # 原始 usage（含 prompt_tokens_details.cached_tokens 这类前缀缓存字段）；
            # 缓存命中（cached: true）或网关没回 usage 时是空对象 {}
            payload["usage"] = outcome.usage
            if outcome.cached:
                payload["cached"] = True
        return _json(payload)

    async def api_not_found(request: Request) -> Response:
        """未知的 /api/* 路径：回 JSON 404，不要落到静态托管的兜底里。"""
        return _json({"error": f"接口不存在：{request.url.path}", "status": 404}, status_code=404)

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/health", health, methods=["GET"]),
        Route("/api/profile", get_profile, methods=["GET"]),
        Route("/api/profile/rating", post_rating, methods=["POST"]),
        Route("/api/explain", post_explain, methods=["POST"]),
        Route("/api/doc/{source_id}", get_doc, methods=["GET"]),
        Route("/api/{rest:path}", api_not_found),  # 必须在静态托管之前
    ]
    if samples_root is not None:
        # 夹具模式（`?fixture=<id>` 读 samples/<id>/doc.json）也能走本服务：契约没要求这条，
        # 是给前端的便利——只读，且只暴露 samples/ 一个目录
        routes.append(Mount("/samples", app=_TolerantStaticFiles(web_dir=samples_root), name="samples"))
    # 静态前端（目录可能还不存在：见 _TolerantStaticFiles）；挂在最后，兜住其余路径
    routes.append(Mount("/", app=_TolerantStaticFiles(web_dir=web_root), name="web"))

    async def on_input_error(request: Request, exc: Exception) -> Response:
        return _json({"error": str(exc)}, status_code=400)

    async def on_http_exception(request: Request, exc: Exception) -> Response:
        status = getattr(exc, "status_code", 500)
        detail = "文件或路由不存在" if status == 404 else str(getattr(exc, "detail", exc))
        return _json(
            {"error": detail, "status": status, "path": request.url.path},
            status_code=status,
        )

    async def on_unhandled(request: Request, exc: Exception) -> Response:  # pragma: no cover - 兜底
        return _json({"error": f"服务内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    return Starlette(
        routes=routes,
        exception_handlers={
            InputError: on_input_error,
            HTTPException: on_http_exception,
            Exception: on_unhandled,
        },
    )
