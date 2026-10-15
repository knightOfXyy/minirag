"""抽取结果的"落盘出口"（sink）。

现在用 JSON 文件实现，重点是先把接口定下来：
    sink.save(output) -> 路径/标识
L08 会新增 PostgresSink（实体/关系/出处写进 PostgreSQL+pgvector），
上层调用方代码不用改——这就是面向接口编程的好处。
"""

from __future__ import annotations

import json
from pathlib import Path

from .document import ExtractionOutput


class JsonFileSink:
    """把每次抽取结果按文档 ID 存成一个 JSON 文件。"""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, document_id: str) -> Path:
        return self.out_dir / f"{document_id}.json"

    def save(self, output: ExtractionOutput) -> Path:
        path = self.path_for(output.document_id)
        # ensure_ascii=False 保留中文；indent 方便人工查看（教学/调试友好）
        path.write_text(
            json.dumps(output.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, document_id: str) -> ExtractionOutput:
        return ExtractionOutput.model_validate_json(
            self.path_for(document_id).read_text(encoding="utf-8")
        )
