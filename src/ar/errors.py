"""统一错误类型与进程退出码。

退出码约定（README 同步记载，测试依赖这几个值）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法 / 输入错误（文件不存在、source_id 非法、**校验所需产物缺失 / 不可读 / 为空**） |
| 3 | 功能尚未实现（`rewrite --mode strict` 严格档接口预留） |
| 4 | 校验失败（锚点、概念清单契约、事实锚；逐条列出问题并点名段 id / 概念 id） |
| 5 | 模型调用失败或模型未配置（整篇都没跑起来） |
| 6 | 模型失败导致**有段落未被改写**（产物可用但不完整；`rewrite` 与 `check` 同码） |
"""

from __future__ import annotations

__all__ = [
    "ArError",
    "InputError",
    "NotImplementedYet",
    "ValidationFailed",
    "ModelUnavailable",
    "ModelPartiallyFailed",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_NOT_IMPLEMENTED",
    "EXIT_VALIDATION",
    "EXIT_MODEL",
    "EXIT_MODEL_PARTIAL",
]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_VALIDATION = 4
EXIT_MODEL = 5
EXIT_MODEL_PARTIAL = 6


class ArError(Exception):
    """本项目所有可预期错误的基类：CLI 捕获后打印消息并返回 ``exit_code``。"""

    exit_code = 1


class InputError(ArError):
    """输入不可用：文件不存在、source_id 非法、JSON 无法解析等。"""

    exit_code = EXIT_USAGE


class NotImplementedYet(ArError):
    """该功能属于后续里程碑，当前明确不可用（绝不假装成功）。"""

    exit_code = EXIT_NOT_IMPLEMENTED


class ValidationFailed(ArError):
    """校验失败。``problems`` 每行一个问题，必须点名具体 id。"""

    exit_code = EXIT_VALIDATION

    def __init__(self, problems: list[str], headline: str = "校验失败") -> None:
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in self.problems) or "  - （无问题明细）"
        super().__init__(f"{headline}：\n{body}")

    def __str__(self) -> str:  # pragma: no cover - 与 __init__ 同一路径
        return super().__str__()


class ModelUnavailable(ArError):
    """模型未配置或调用失败（消息已脱敏，不含密钥值）。"""

    exit_code = EXIT_MODEL


class ModelPartiallyFailed(ArError):
    """模型调用失败（或模型没给出决策）导致**有段落未被改写**：产物可用但不完整。

    与 ``ModelUnavailable``（整篇都没跑起来，退出码 5）区分：这里产物已经落盘、能读，
    只是其中一些段落退回原文。事实锚只能证明「没改坏」，证明不了「真的改写了」，
    所以必须靠退出码把这件事暴露出来（DESIGN §6 的第一纪律）。
    """

    exit_code = EXIT_MODEL_PARTIAL
