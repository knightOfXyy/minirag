"""把实体知识库暴露为 MCP 工具（L12）。"""

from .kb_server import build_server, main

__all__ = ["build_server", "main"]
