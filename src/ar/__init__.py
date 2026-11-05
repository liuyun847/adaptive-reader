"""adaptive-reader 后端包（骨架）。

模块分工：

- ``ingest``   摄取：本地文本 → ``source.md`` + ``source.meta.json``（段落切分规则在这里，M1）；
  两份产物都走 ``jsonio`` 的原子写（``source.md`` 按字节），坏目录**快速失败**
- ``anchors``  锚点校验：元数据与源文是否仍然对齐（``ar check`` 的前半段）
- ``paths``    路径约定（``PROJECT_ROOT`` / ``DEFAULT_DATA_DIR`` / ``DEFAULT_CONFIG_PATH`` 等）
- ``jsonio``   JSON / 文本 / **字节**读写与时间戳（UTF-8、``ensure_ascii=False``、缩进 2，原子写）；
  **全项目唯一的写盘实现处**：``ingest`` 的 ``source.md``、``profile.save_profile`` 的
  ``profile.yaml`` 都取这里的「临时文件 + 有界重试 + 可读错误」；
  **坏路径快速失败**：目标目录不可写时立刻抛 ``WriteError``（点名目录与原始 errno），
  不空转、不重试（``tempfile.mkstemp`` 会把 ``PermissionError`` 当「候选名被占用」一起吞掉、
  按 ``tempfile.TMP_MAX`` 空转下去——2026-10-14 本机实测那是 **2147483647**（21 亿级，
  Windows 上**近似无界**，不是常说的 10000）；本实现的重试上限是写死的 ``_TMP_NAME_ATTEMPTS = 32``）
- ``errors``   统一错误类型与退出码表（退出码的唯一实现处）
- ``config``   配置与密钥读取（``AR_API_KEY`` > ``config.local.json`` 的 ``api_key``）+ 脱敏；
  可选的 ``routes`` 段（多源回退链）也在这里汇入 ``Config``
- ``chain``    多源回退链：``Route`` 定义与校验、逐跳密钥现读（环境变量 > DSH 凭据文件 ``refs``）、
  按 provider 记账的冷却表、`order_routes` 三桶自动排序（形状照 DSH 插件 ``dsh-llm-auto``，不依赖 DSH 进程）
- ``quota``    额度探测：现查 Command Code 的 ``/alpha/billing/{credits,subscriptions}`` 与
  OpenCode Go 的 ``/usage``，给出「月度重置时刻 / 哪一档打满 / 冷到何时」——排序键与冷却终点都从这里来，
  **不需要人工填写任何时间**；查不到一律 fail-open（不冷却、进「未知」桶）
- ``model``    OpenAI 兼容流式模型客户端（httpx + httpx-sse，失败自动降级 stream_options）；
  配了 ``routes`` 时按链逐跳换源、换跳前摘掉历史思考
- ``llm``      统一模型调用层：重试策略、错误码白名单、逐次尝试记账、失败不抛
- ``tools``    模型工具层：六个具名工具的注册表、JSON Schema 与执行
- ``prompts``  提示词与版本号（回归基线：产物里记录 prompt_version）
- ``analyze``  概念清单抽取 + 契约校验（M2）；``format_illustration_block`` 供 ``rewrite`` 提示词用
- ``rewrite``  逐段决策 + 改写（M3：默认先串行预热第 1 批再并发其余批次；``per_paragraph`` 时
  一段一请求，``conversation`` 时同一上下文累积；校验不过带反馈重试；四个动作都已实现；
  两遍式 ``plan_first``（``plan-v2``：第一遍只打动作标、第二遍照计划改写；两遍共享同一段前缀）与逐段对话的
  上下文瘦身 ``conversation_digest``（摘要回填替代整段 JSON）也在这里）；
  局部重跑（``--only`` / ``--retry-failed`` / ``--from-signals``）与逐段合并器 ``merge_artifacts``、
  在线压缩 ``acompress_paragraph``（``POST /api/compress``，与 ``ar check`` 同一套事实锚口径 + 申报通道）都在这里；
  ``why.profile`` 的语义与兜底也在这里：模型给的引用逐条保留，漏键 / 给错形状 / 该给引用却给空数组
  一律可见（``meta.degraded``）并只照读者自评兜底补齐（读者本来没有自评时不误报）；
  **给错形状**包含「数组里混进非字符串项」——**整数也算**（2026-10-14 收紧：只接受字符串，
  不再把 ``[42]`` 静默转成 ``"42"`` 留下）；
  补齐过的段在本段产物上留结构化标记 ``changes[i].profile_refs_filled``（为真才出现、不进
  ``decisions.json``），页面那枚「兜底补齐」徽标只认它
- ``baseline`` 一次性整篇改写基线（``oneshot-v1``，只用于对照）：单次调用吃全文、吐全篇，
  只写 ``data/<id>/_eval/``，不碰三份主产物
- ``fidelity`` 事实锚校验（M3）：数字 / 引号引文 / 书名号《》+ 段缺失；
  **申报与口径**（§14.1）：``parse_declarations`` / ``compare_blocks_detailed`` 按
  ``keep_all``（严）/ ``allow_declared_drops``（松，已申报的缺失放行）处理；
  **多申报**（锚点一个没少却申报放弃了某项）单列 ``over_declared``，不改放行结论、但绝不静默
- ``profile``  读者画像 YAML 读写（自评写入 + 读前背景问卷的背景块 + 行为信号合并 ``applySignals``
  + 偏好读写与创建路径 ``set_preference`` / 事实锚口径 ``pref_fact_anchor``）；
  ``save_profile`` 走 ``jsonio.write_text``（原子写、坏目录抛 ``WriteError``）
- ``events``   行为事件流 ``profiles/<reader>/events.jsonl``：落盘 / 去重 / 事件 → 信号折算（不调模型）；
  另有回流**水位线** ``profiles/<reader>/feedback.json``（已折算的事件 id，供 ``ar feedback --apply`` 幂等）；
  ``append_events`` 是**唯一一处非原子写盘**（纯追加的日志不该整文件重写），失败同样抛
  ``WriteError``（点名目录 + errno），HTTP 侧照旧按「尽力而为」回 200 + ``ok:false``
- ``survey``   读前背景问卷：题目定义 / 答案校验 / 读者素描渲染 / 模型追问
- ``explain``  按需讲解（``POST /api/explain`` 的实现，降级必须可见）
- ``cache``    讲解与在线压缩共用的进程内 LRU（只缓存成功结果，键含画像指纹；``kind`` 一格分开两类）
- ``server``   HTTP 层：``/api/*`` + 托管 ``web/``（含偏好写入 ``POST /api/profile/preference``）
- ``cli``      命令行：八个冻结子命令（ingest / analyze / rewrite / check / serve / tools / feedback / routes）

尚未实现（后续里程碑）：按材料出辨识题与复述题；``rewrite --mode strict`` 严格档（接口已留位，
遇到明确报错）。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
