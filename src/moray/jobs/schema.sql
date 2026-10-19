-- Moray 批量文档任务表（L11 状态与断点续跑；L15+ 多 worker 租约）
-- 与知识库表同库；任务处理是幂等的，所以租约过期后把 running 重置为 pending
-- 即可安全重跑（见 jobstore.reclaim_expired / claim_next 的过期接管）。

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
    finished_at     TIMESTAMPTZ,
    -- ===== 多 worker 租约（lease）=====
    locked_by       TEXT,                          -- 当前持有该任务的 worker_id
    locked_at       TIMESTAMPTZ,                   -- 最近一次认领/心跳续租时间
    expires_at      TIMESTAMPTZ                    -- 租约到期时刻；到期可被其他 worker 接管
);

-- 对 L11 时期已建表的库做平滑升级（必须在建引用新列的索引之前执行；可重复）
ALTER TABLE extraction_jobs ADD COLUMN IF NOT EXISTS locked_by  TEXT;
ALTER TABLE extraction_jobs ADD COLUMN IF NOT EXISTS locked_at  TIMESTAMPTZ;
ALTER TABLE extraction_jobs ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_jobs_status ON extraction_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_doc ON extraction_jobs(document_id);
-- worker 拉取下一个任务时按到期时间扫描
CREATE INDEX IF NOT EXISTS idx_jobs_lease
    ON extraction_jobs(status, expires_at);
