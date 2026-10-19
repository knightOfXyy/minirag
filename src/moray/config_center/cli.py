"""领域配置中心管理 CLI
=====================

管理 domain_configs 表（热更的抽取领域包）。改完立即对 /extract 与 worker 生效，
无需重启任何进程（DbDomainResolver 按 updated_at 版本缓存自动刷新）。

    python -m moray.config_center.cli list
    python -m moray.config_center.cli show finance
    python -m moray.config_center.cli seed            # 把内置 general/finance 种入
    python -m moray.config_center.cli import recruit.json   # 从 JSON 导入/更新
    python -m moray.config_center.cli disable finance
    python -m moray.config_center.cli enable finance

import 的 JSON 即 DomainSpec 的纯数据形态，例：
{
  "code": "recruit", "name": "招聘",
  "relation_predicates": ["要求掌握"],
  "types": [
    {"code": "JOB", "label": "职位", "guide": "招聘岗位",
     "attributes": [
       {"key": "title", "label": "职位名", "required": true, "type": "string"},
       {"key": "salary_min", "type": "integer"},
       {"key": "seniority", "type": "string", "enum": ["初级","中级","高级"]}
     ]}
  ]
}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ..kb import close_pool, init_pool
from ..schema import domain_from_dict
from . import DomainConfigStore, DbDomainResolver


def _print(title: str, text: str) -> None:
    print(f"\n=== {title} ===\n{text}")


async def _run(args: argparse.Namespace) -> None:
    pool = await init_pool()
    store = DomainConfigStore(pool)
    await store.init_schema()

    if args.cmd == "seed":
        n = await store.seed_builtin()
        print(f"种入内置领域 {n} 个（已存在的跳过）")

    elif args.cmd == "list":
        rows = await pool.fetch(
            "SELECT code, name, enabled, updated_at FROM domain_configs ORDER BY code")
        if not rows:
            print("（空表，先运行 seed）")
        for r in rows:
            mark = "●" if r["enabled"] else "✗(禁用)"
            print(f"[{mark}] {r['code']:<12} {r['name'] or '':<10} "
                  f"updated={r['updated_at']:%Y-%m-%d %H:%M:%S}")

    elif args.cmd == "show":
        row = await store.get_raw(args.code)
        if row is None:
            print(f"未找到 {args.code}")
            sys.exit(1)
        print(json.dumps(row["spec"], ensure_ascii=False, indent=2))

    elif args.cmd == "import":
        data = json.loads(open(args.file, encoding="utf-8").read())
        domain = domain_from_dict(data)
        await store.upsert(domain)
        print(f"已写入领域 {domain.code}（{domain.name}），"
              f"类型 {len(domain.types)} 个；对所有抽取进程即时生效")

    elif args.cmd in ("enable", "disable"):
        ok = await store.set_enabled(args.code, args.cmd == "enable")
        print(("已更新" if ok else "未找到该领域"), args.code)

    await close_pool(pool)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Moray 领域配置中心")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("code")
    imp = sub.add_parser("import"); imp.add_argument("file")
    en = sub.add_parser("enable"); en.add_argument("code")
    dis = sub.add_parser("disable"); dis.add_argument("code")
    asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    main()
