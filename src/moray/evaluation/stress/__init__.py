"""压力案卷黄金语料子包：确定性合成语料 + 离线分块评测（工单 02）。"""

from .budget import (
    MAX_ENTITY_F1_DROP_PT,
    evaluate_budget_gate,
    relation_chunk_profile,
)
from .corpus import (
    DECOY_PREFIX,
    PREDICATE_TRIGGERS,
    STRESS_SEEDS,
    StressCase,
    StressEntity,
    StressRelation,
    build_text,
    generate_case,
    relation_semantically_supported,
    serialize_case,
    validate_case,
    validate_case_semantics,
)
from .extractor import StressRuleExtractor, extract_chunk
from .loader import (
    StressGoldCase,
    load_stress_case,
    load_stress_dir,
    stress_case_to_gold,
)
from .metrics import (
    CaseEvaluation,
    aggregate_cases,
    evaluate_case,
    relation_recall_for_predicates,
)
from .runner import (
    DEFAULT_GRIDS,
    aggregate_grid,
    build_report,
    run_grid,
    run_grid_async,
    run_one_grid,
    save_grid_report,
)

__all__ = [
    "MAX_ENTITY_F1_DROP_PT",
    "evaluate_budget_gate",
    "relation_chunk_profile",
    "DECOY_PREFIX",
    "PREDICATE_TRIGGERS",
    "STRESS_SEEDS",
    "StressCase",
    "StressEntity",
    "StressRelation",
    "build_text",
    "generate_case",
    "relation_semantically_supported",
    "serialize_case",
    "validate_case",
    "validate_case_semantics",
    "StressRuleExtractor",
    "extract_chunk",
    "StressGoldCase",
    "load_stress_case",
    "load_stress_dir",
    "stress_case_to_gold",
    "CaseEvaluation",
    "aggregate_cases",
    "evaluate_case",
    "relation_recall_for_predicates",
    "DEFAULT_GRIDS",
    "aggregate_grid",
    "build_report",
    "run_grid",
    "run_grid_async",
    "run_one_grid",
    "save_grid_report",
]
