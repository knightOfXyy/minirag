-- Moray 批量文档任务表（L11 状态与断点续跑）
-- 与知识库表同库；任务处理是幂等的，所以崩溃后把 running 重置为 pending 即可安全重跑。

CREATE TABLE IF NOT EXISTS extraction_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_id          TEXT NOT NULL UNIQUE,          -- 客户端稳定幂等键（如文档路径哈希）
    document_id     TEXT NOT NULL,
    document_ref    TEXT NOT NULL,                 -- 文档位置（路径/来源标识）
    domain_code     TEXT NOT NULL DEFAULT 'general',
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- pending / running / succeeded / failed / cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    result_summary  JSONB NOT NULL DEFAULT '{}',   -- 成功时的统计（实体/关系数等）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON extraction_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_doc ON extraction_jobs(document_id);
