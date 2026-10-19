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
-- 幂等：同一实体在同一文档、同一位置的同面写法只保留一条。
-- 用 COALESCE 归一 NULL（PostgreSQL 唯一索引把 NULL 视为互不相等），
-- 否则 span/chunk 缺失时重复入库无法去重。
CREATE UNIQUE INDEX IF NOT EXISTS uq_mention
    ON entity_mentions (
        entity_id, document_id, surface,
        COALESCE(start_off, -1), COALESCE(end_off, -1), COALESCE(chunk_id, '')
    );

-- 向量相似度索引（IVFFLAT，数据量大时走 ANN；小数据量 PG 自动全量精确计算）
-- lists 按数据规模调整；建索引后需要 ANALYZE / 一定数据量才会被规划器选用。
CREATE INDEX IF NOT EXISTS idx_entities_embedding
    ON entities USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- 关系表：头实体 -[谓词]-> 尾实体（L08）。实体用外键 id 关联。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relations (
    id              BIGSERIAL PRIMARY KEY,
    domain_code     TEXT NOT NULL DEFAULT 'general',
    source_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate       TEXT NOT NULL,
    target_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    confidence      REAL NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain_code, source_entity_id, predicate, target_entity_id)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_predicate ON relations(predicate);

-- 关系的文本证据（provenance）：一条关系可由多篇文档的多个句子支撑
CREATE TABLE IF NOT EXISTS relation_evidence (
    id          BIGSERIAL PRIMARY KEY,
    relation_id BIGINT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    surface     TEXT NOT NULL,
    start_off   INTEGER,
    end_off     INTEGER,
    chunk_id    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (relation_id, document_id, start_off, end_off)
);
CREATE INDEX IF NOT EXISTS idx_relev_relation ON relation_evidence(relation_id);
CREATE INDEX IF NOT EXISTS idx_relev_doc ON relation_evidence(document_id);

-- ---------------------------------------------------------------------------
-- 人工审核队列（L09 HITL）：消歧器拿不准的候选放这里，等人裁决后再合并
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_reviews (
    id              BIGSERIAL PRIMARY KEY,
    domain_code     TEXT NOT NULL DEFAULT 'general',
    document_id     TEXT,
    survivor_id     BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    duplicate_id    BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    survivor_name   TEXT NOT NULL,          -- 冗余存名字，审核台展示用（实体可能先被删）
    duplicate_name  TEXT NOT NULL,
    similarity      REAL NOT NULL,          -- 向量相似度（决策依据之一）
    reason          TEXT NOT NULL DEFAULT '',-- 消歧器/Agent 给出的判断理由
    status          TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_review_status ON merge_reviews(status);
CREATE INDEX IF NOT EXISTS idx_review_dup ON merge_reviews(duplicate_id);
-- 同一对实体只允许有一条待审记录（裁决后可再次入队）
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_pending
    ON merge_reviews(LEAST(survivor_id, duplicate_id),
                     GREATEST(survivor_id, duplicate_id))
    WHERE status = 'pending';
