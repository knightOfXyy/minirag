-- 领域配置中心：用一张表承载"可热更的抽取领域包"（实体类型 + 专有属性规格）。
-- 运营/管理端直接 UPDATE 这张表的 spec(JSONB)，抽取进程无需重启即可生效
-- （DbDomainResolver 按 updated_at 版本缓存检测变更）。

CREATE TABLE IF NOT EXISTS domain_configs (
    code        TEXT PRIMARY KEY,                  -- 领域代码：general/finance/...
    name        TEXT,
    spec        JSONB NOT NULL,                    -- DomainSpec 的纯数据形态（见 schema.domains.domain_to_dict）
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
