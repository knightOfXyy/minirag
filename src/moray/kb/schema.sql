-- Moray 实体知识库 schema（PostgreSQL 16 + pgvector 0.8）
-- L07：实体主表 + 向量列 + 别名表 + 出处表；关系表 L08 补全。

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 实体主表：一行一个"规范实体"（L09 消歧后同一现实对象只占一行）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id          BIGSERIAL PRIMARY KEY,
    domain_code TEXT NOT NULL DEFAULT 'general',
    name        TEXT NOT NULL,                 -- 规范名
    type        TEXT NOT NULL,                 -- 领域类型代码（PER/ORG/CONTRACT...）
    attributes  JSONB NOT NULL DEFAULT '{}',
    confidence  REAL NOT NULL DEFAULT 0,
    embedding   vector(512),                   -- bge-small-zh，512 维
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain_code, type, name)
);

-- 别名：一个实体多个叫法（华为/华为公司/Huawei）
CREATE TABLE IF NOT EXISTS entity_aliases (
    id        BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    UNIQUE (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_name ON entity_aliases(alias);

-- 出处：实体每次在文档中的具体出现（provenance）
CREATE TABLE IF NOT EXISTS entity_mentions (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    surface     TEXT NOT NULL,                 -- 原文写法
    start_off   INTEGER,
    end_off     INTEGER,
    chunk_id    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mention_doc ON entity_mentions(document_id);
CREATE INDEX IF NOT EXISTS idx_mention_entity ON entity_mentions(entity_id);

-- 向量相似度索引（IVFFLAT，数据量大时走 ANN；小数据量 PG 自动全量精确计算）
-- lists 按数据规模调整；建索引后需要 ANALYZE / 一定数据量才会被规划器选用。
CREATE INDEX IF NOT EXISTS idx_entities_embedding
    ON entities USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
