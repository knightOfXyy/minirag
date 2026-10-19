"""嵌入器测试。

模型语义测试较慢（需加载 ONNX），这里只测不加载模型也能验证的数学性质；
真实模型的语义质量在 lessons/l07 与集成测试里验证。
"""

import numpy as np

from moray.embedding import cosine


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([1, 0], [-1, 0]) == -1.0


def test_cosine_invariant_to_scale():
    a = np.array([0.3, 0.4])
    assert cosine(a, a * 5) == 1.0      # 同向不同模长 -> 仍为 1
    assert abs(cosine(a, [0.4, -0.3])) < 1e-9  # 垂直


def test_cosine_zero_vector_safe():
    # 零向量不应抛除零异常
    assert cosine([0, 0], [1, 1]) == 0.0
