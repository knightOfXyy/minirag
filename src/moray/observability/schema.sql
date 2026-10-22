-- L13 业务质量看板：每次文档抽取流水线运行的一行记录。
-- 与 OTEL 技术链路互补：这里是"抽取质量/成本/自动化程度"的可聚合业务指标。

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                BIGSERIAL PRIMARY KEY,
    document_id       TEXT NOT NULL,
    domain_code       TEXT NOT NULL DEFAULT 'general',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 规模
    n_entities        INTEGER NOT NULL DEFAULT 0,
    n_relations       INTEGER NOT NULL DEFAULT 0,

    -- 质量
    span_ratio        REAL,                          -- 出处一致率 0~1
    review_passed     BOOLEAN,                       -- 是否通过审校门禁
    review_iters      INTEGER NOT NULL DEFAULT 1,    -- 审校返工轮数
    open_issues       INTEGER NOT NULL DEFAULT 0,    -- 仍未解决的问题数

    -- 消歧自动化（L09）
    n_auto_merged     INTEGER NOT NULL DEFAULT 0,    -- 自动合并
    n_queued          INTEGER NOT NULL DEFAULT 0,    -- 转人工审核
    n_created         INTEGER NOT NULL DEFAULT 0,

    -- 成本
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,  -- 命中提示缓存的输入 token
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    model_name        TEXT NOT NULL DEFAULT '',

    trace_id          TEXT                           -- 关联 OTEL trace（若有）
);
CREATE INDEX IF NOT EXISTS idx_runs_doc ON pipeline_runs(document_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON pipeline_runs(created_at);

-- 对已有库平滑升级（必须先于任何引用新列的语句；可重复执行）
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS cached_input_tokens INTEGER NOT NULL DEFAULT 0;
