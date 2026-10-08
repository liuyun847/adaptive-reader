"""adaptive-reader 后端包（骨架）。

模块分工：

- ``ingest``   摄取：本地文本 → ``source.md`` + ``source.meta.json``（段落切分规则在这里，M1）
- ``anchors``  锚点校验：元数据与源文是否仍然对齐（``ar check`` 的前半段）
- ``config``   配置与密钥读取（``AR_API_KEY`` > ``config.local.json`` 的 ``api_key``）+ 脱敏
- ``model``    OpenAI 兼容流式模型客户端（httpx + httpx-sse，失败自动降级 stream_options）
- ``prompts``  提示词与版本号（回归基线：产物里记录 prompt_version）
- ``analyze``  概念清单抽取 + 契约校验（M2）
- ``profile``  读者画像 YAML 读写（自评写入）
- ``explain``  按需讲解（``POST /api/explain`` 的实现，降级必须可见）
- ``server``   HTTP 层：``/api/*`` + 托管 ``web/``
- ``cli``      命令行：ingest / analyze / rewrite / check / serve

尚未实现（后续里程碑）：逐段决策 + 改写 + 事实锚（``rewrite`` / ``check`` 的后半段）。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
