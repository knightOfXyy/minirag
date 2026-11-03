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

    trace_id          TEXT,                          -- 关联 OTEL trace（若有）

    -- 工单 01：分块级度量（文档级聚合）。chunk_calls=0 表示整篇旧闭环、未分块，
    -- 看板聚合时据此过滤，避免老数据把分块均值拉成 0。
    chunk_calls         INTEGER NOT NULL DEFAULT 0,  -- 分块 LLM 调用块数
    total_attempts      INTEGER NOT NULL DEFAULT 0,  -- 含校验返工的尝试总次数
    truncated_chunks    INTEGER NOT NULL DEFAULT 0,  -- 最终被长度上限截断的块数
    truncation_signals  INTEGER NOT NULL DEFAULT 0,  -- 任意尝试撞长度上限的块数
    reworked_chunks     INTEGER NOT NULL DEFAULT 0,  -- 发生校验返工的块数
    rework_rate         REAL    NOT NULL DEFAULT 0,  -- reworked_chunks/chunk_calls
    wait_ms             INTEGER NOT NULL DEFAULT 0,  -- 排队/限流等待总耗时
    prefill_ms          INTEGER NOT NULL DEFAULT 0,  -- 首 token（prefill）总耗时
    decode_ms           INTEGER NOT NULL DEFAULT 0,  -- 解码总耗时
    chunk_metrics       JSONB                         -- 每块一条明细，供下钻
);
CREATE INDEX IF NOT EXISTS idx_runs_doc ON pipeline_runs(document_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON pipeline_runs(created_at);

-- 对已有库平滑升级（必须先于任何引用新列的语句；可重复执行）
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS cached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS chunk_calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS total_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS truncated_chunks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS truncation_signals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS reworked_chunks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS rework_rate REAL NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS wait_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS prefill_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS decode_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS chunk_metrics JSONB;

-- 截断是"调大块预算"唯一的静默杀手：给看板按截断过滤/排序留索引。
-- 放在 ALTER 之后：对已存在的旧库，列是刚加上去的。
CREATE INDEX IF NOT EXISTS idx_runs_truncated
    ON pipeline_runs(truncated_chunks) WHERE truncated_chunks > 0;
