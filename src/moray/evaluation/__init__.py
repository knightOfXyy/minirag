"""离线评测：黄金集 + P/R/F1（L15）。"""

from .gold import GoldCase, case_document, load_gold_case, load_gold_dir
from .metrics import PRF, EvalReport, evaluate, macro_average
from .runner import RunResult, compare, run_evaluation, save_report

__all__ = [
    "GoldCase", "case_document", "load_gold_case", "load_gold_dir",
    "PRF", "EvalReport", "evaluate", "macro_average",
    "RunResult", "compare", "run_evaluation", "save_report",
]
