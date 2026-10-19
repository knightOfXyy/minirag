"""本地向量嵌入（bge 中文模型，ONNX 推理）。"""

from .embedder import Embedder, cosine, get_embedder

__all__ = ["Embedder", "cosine", "get_embedder"]
