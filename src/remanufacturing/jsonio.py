"""确定性的 JSON 规范化与内容摘要，用于固化证据版本。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Iterable


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    """生成跨平台一致、可重复的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def normalize(value: Any) -> Any:
    """把 Decimal 等类型转换为可普通 json.dumps 的形式。"""

    return json.loads(canonical_json(value))
