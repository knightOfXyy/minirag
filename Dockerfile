# Moray 实体抽取知识库 —— 服务镜像（L14/L15 收尾）
# 只打包库与 API 服务；运行期通过环境变量连接外部 PostgreSQL+pgvector。
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_XET=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码并安装为可导入包
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e . --no-deps

# API 默认监听 8000；查询服务不需要 LLM（加 --llm 才开启实时抽取）
EXPOSE 8000
CMD ["python", "-m", "moray.api.server", "--host", "0.0.0.0", "--port", "8000"]
