"""
本地中文向量嵌入器（L07）
=========================

为什么用本地模型而不是 ARK：火山方舟 Coding Plan 不支持 embedding 接口，
而实体消歧/语义检索需要向量。本地 bge-small-zh-v1.5：
  · 离线、免费、无 QPS 限制；512 维；ONNX 推理（fastembed，不依赖笨重的 torch）；
  · 中文实体名/短文本语义区分度好（实测"华为技术"vs"华为" 0.83，
    vs"腾讯" 0.58，vs"深圳" 0.46，足够支撑实体消歧）。

工程要点：
1. 模型全局单例（加载一次约几秒、占内存），get_embedder() 复用；
2. 输出做 L2 归一化，这样点积==余弦相似度，pgvector 里用内积索引即可；
3. embed() 支持批量，减少推理次数；
4. 通过环境变量 HF_ENDPOINT 走镜像、HF_HUB_DISABLE_XET 关掉鉴权失败的
   xet 协议（见 README/课程说明）。
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache

import numpy as np

# 在导入 huggingface 相关库之前，确保国内下载环境变量就位
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512


def cosine(a: np.ndarray | list[float], b: np.ndarray | list[float]) -> float:
    """余弦相似度；输入是否归一化都安全。"""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denom) if denom else 0.0


class Embedder:
    """bge 文本嵌入器（ONNX），输出 L2 归一化向量。"""

    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM) -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name
        self.dim = dim
        self._model = TextEmbedding(model_name=model_name)
        self._lock = threading.Lock()

    def embed(self, texts: str | list[str]) -> np.ndarray:
        """编码单条或多条文本，返回 shape=(n, dim) 的 float32 归一化向量。"""
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)

        with self._lock:
            vecs = np.asarray(list(self._model.embed(items)), dtype=np.float32)

        # L2 归一化（保持少量数值稳定处理）
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms
        return vecs[0] if single else vecs

    def embed_one(self, text: str) -> list[float]:
        """编码单条文本并返回 Python list（便于写库/JSON）。"""
        return self.embed(text).tolist()


@lru_cache(maxsize=2)
def get_embedder(model_name: str = DEFAULT_MODEL) -> Embedder:
    """进程内单例，避免重复加载模型。"""
    return Embedder(model_name=model_name)
