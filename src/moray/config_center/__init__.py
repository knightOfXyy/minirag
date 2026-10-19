"""配置中心：用 PostgreSQL 表承载可热更的领域抽取配置。"""

from .domain_store import DomainConfigStore
from .resolver import DbDomainResolver

__all__ = ["DomainConfigStore", "DbDomainResolver"]
