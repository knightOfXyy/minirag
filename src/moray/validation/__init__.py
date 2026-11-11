"""离线数据校验模块（schema.org 词汇 + SHACL 形状 + pyshacl）。

设计目标（见 .scratch/talent-be/eval-schemaorg-pyshacl.md）：
- 完全离线：schema.org 全量词汇随包 bake（vocab/），运行期不联网；
- 不独立持久化 RDF：仅在请求内把抽取结果临时投影为 RDF 图，校验完即弃；
- 预览期强校验：默认 inference="none" + 项目自有形状，毫秒级返回结构化违约。
"""

from .schemas import ValidationRequest, ValidationResponse, Violation
from .shacl_validator import validate_extraction

__all__ = [
    "ValidationRequest",
    "ValidationResponse",
    "Violation",
    "validate_extraction",
]
