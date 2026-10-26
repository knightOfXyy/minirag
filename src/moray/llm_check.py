"""LLM 接入自检（换供应商 / 排障时先跑这个，不要直接跑批量）。

    .venv/bin/python -m moray.llm_check              # 只看配置，不发请求（零成本）
    .venv/bin/python -m moray.llm_check --call        # 一发对话请求：验鉴权/协议
    .venv/bin/python -m moray.llm_check --call --twice # 再发一发同前缀：验缓存回传
    .venv/bin/python -m moray.llm_check --extract     # 一发真实结构化抽取：验 function calling

为什么需要 --extract：三家都讲 OpenAI 兼容协议，"能对话"不等于"能用工具"。
moray 的结构化输出是靠 function calling（agentscope 把 pydantic schema 注册成
一个工具让模型调用）实现的，某家网关不支持 tools 时，对话会成功、抽取会失败——
所以要分开验，别用一次成功掩盖另一个坑。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许 python -m moray.llm_check 在项目根直接跑
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agentscope.message import SystemMsg, UserMsg

from moray.config import build_chat_model, describe, get_settings

PROBE_SYSTEM = "你是接口自检程序，回答尽量短。"
PROBE_USER = "只回复两个字：好的"

# 抽取探针：一句话里同时有人名和机构名，够验证 schema 是否真被约束住
EXTRACT_TEXT = "任正非在华为技术有限公司深圳总部会见了贵阳市政府代表团。"


def show_config() -> dict:
    d = describe()
    print("=" * 70)
    print("当前 LLM 配置（来自 .env，密钥已打码）")
    print("=" * 70)
    for k, v in d.items():
        print(f"  {k:16} = {v if v is not None else '（未设置，沿用厂商默认）'}")
    return d


def _blocks_text(resp) -> str:
    """把 ChatResponse.content 里的文本块拼出来（各家块类型略有差异）。"""
    out = []
    for b in getattr(resp, "content", []) or []:
        if getattr(b, "type", None) == "text":
            out.append(getattr(b, "text", "") or "")
    return "".join(out)


def _report(tag: str, resp, prev_input: int | None = None) -> int:
    u = getattr(resp, "usage", None)
    if u is None:
        print(f"  {tag}: 返回里没有 usage（该网关可能不回传统计）")
        return 0
    cached = int(getattr(u, "cache_input_tokens", 0) or 0)
    inp = int(getattr(u, "input_tokens", 0) or 0)
    line = (f"  {tag}: 输入 {inp}  输出 {int(getattr(u, 'output_tokens', 0) or 0)}"
            f"  缓存命中 {cached}  耗时 {float(getattr(u, 'time', 0) or 0):.2f}s"
            f"  回复「{_blocks_text(resp)[:20]}」")
    if inp:
        line += f"  命中率 {cached / inp:.0%}"
    print(line)
    if prev_input and cached == 0:
        print("     ↑ 与上一发前缀相同却 0 命中：该端点要么没启用前缀缓存，"
              "要么多副本无会话亲和（命中是概率性的）")
    return inp


def _explain_error(exc: Exception) -> str:
    """把常见网关错误翻译成人话 + 下一步。"""
    text = str(exc)
    name = type(exc).__name__
    if "401" in text or "Unauthor" in name or "authentication" in text.lower():
        return (f"鉴权失败（{name}）。检查 {get_settings()['provider']} 对应的 "
                "API Key 是否正确、是否属于这个 base_url 的站点。")
    if "404" in text or "not found" in text.lower() or "ModelNotFound" in text:
        return (f"模型不存在（{name}）。当前 model="
                f"{get_settings()['chat_model']}；DeepSeek 2026-07-24 起 "
                "deepseek-chat/deepseek-reasoner 已下线，改用 deepseek-flash "
                "或 deepseek-v4-pro。")
    if "429" in text or "Quota" in text or "RateLimit" in name:
        return f"限流或额度用尽（{name}）：{text[:200]}"
    if "connection" in text.lower() or "timeout" in text.lower():
        return f"网络不通（{name}）：检查 base_url={get_settings()['base_url']} 可达性/代理"
    return f"{name}: {text[:300]}"


async def probe_chat(twice: bool) -> int:
    """发 1~2 发极短对话：验鉴权、协议、以及缓存字段是否回传。"""
    print("\n" + "=" * 70)
    print("对话探针（验鉴权与协议）" + (" + 同前缀重发（验缓存）" if twice else ""))
    print("=" * 70)
    model = build_chat_model(stream=False)
    msgs = [SystemMsg(name="system", content=PROBE_SYSTEM),
            UserMsg(name="user", content=PROBE_USER)]
    print(f"  模型类 = {type(model).__name__}  model = {model.model}")
    try:
        r1 = await model(msgs)
        inp = _report("第 1 发", r1)
        if not twice:
            return inp
        # 第 2 发完全相同：若网关有自动前缀缓存，这里应当出现 cached>0
        r2 = await model(msgs)
        _report("第 2 发（前缀完全相同）", r2, prev_input=inp)
        return inp
    except Exception as exc:  # noqa: BLE001
        print("  ❌ " + _explain_error(exc))
        return 0


async def probe_extract() -> None:
    """跑一次真实抽取（走 tools/function calling），这才是 moray 的实际用法。"""
    print("\n" + "=" * 70)
    print("抽取探针（验 function calling + 结构化输出）")
    print("=" * 70)
    from moray.extraction import Document, Extractor

    ex = Extractor("general", max_retries=0)
    print(f"  领域 = {ex.domain.code}  前缀指纹 = {ex.prefix_version}")
    try:
        out = await ex.extract(Document(id="llm-check", text=EXTRACT_TEXT,
                                         source="llm_check"))
        u = out.usage
        print(f"  ✅ 抽出 {len(out.result.entities)} 个实体："
              + "、".join(f"{e.name}[{e.type}]" for e in out.result.entities))
        print(f"  用量：输入 {u.input_tokens} 输出 {u.output_tokens} "
              f"缓存命中 {u.cached_input_tokens} 命中率 {u.cache_hit_rate:.0%}")
        print("  提示：输出若明显大于输入，多半是思考链在烧钱，"
              "可试 LLM_REASONING_EFFORT / LLM_THINKING（见 .env.example）")
    except Exception as exc:  # noqa: BLE001
        print("  ❌ " + _explain_error(exc))
        print("  若报错提到 tools/function calling 不支持：该端点无法承载 moray 的"
              "结构化输出，需要换模型名（非思考模式）或换网关。")


async def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m moray.llm_check",
        description="LLM 接入自检（默认只看配置，不花 token）")
    ap.add_argument("--call", action="store_true", help="发一发对话请求")
    ap.add_argument("--twice", action="store_true",
                    help="连发两发同前缀请求，看缓存命中是否回传（需配合 --call）")
    ap.add_argument("--extract", action="store_true",
                    help="跑一次真实结构化抽取，验 function calling")
    args = ap.parse_args()

    try:
        show_config()
    except Exception as exc:  # noqa: BLE001 配置本身就错时给出可操作提示
        print("❌ " + _explain_error(exc))
        return 2

    if not (args.call or args.extract):
        print("\n未发请求（零成本）。接下来：")
        print("  python -m moray.llm_check --call --twice   # 验鉴权 + 缓存回传")
        print("  python -m moray.llm_check --extract        # 验 function calling")
        return 0

    if args.call or args.twice:
        await probe_chat(twice=args.twice)
    if args.extract:
        await probe_extract()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
