"""
================================================================================
加餐 · 提示缓存（prompt cache）：我们的前缀到底被复用了几次
================================================================================

大模型算一次输入要付两笔钱：把 token 变成 KV 的 prefill，和逐字生成 decode。
【前缀缓存】= 服务端把"和上次逐字节相同的前缀"的 KV 留着，下次直接复用：
命中的那部分输入按远低于正常价计费（DeepSeek/火山通常 1/4~1/10），prefill
时间也省掉。命中判定是"从头开始逐 token 比对"，所以规矩只有一条：

    把不变的东西放前面、且逐字节不变；把变的东西尽量往后放。

一次真实抽取请求的构成（moray 现在的样子）：

    ┌─ system: 角色 + 领域类型枚举 + 属性规格 + 关系谓词 + TASK_PROTOCOL ─┐ 不变
    ├─ tools:  结构化输出 schema（服务端模板会渲染进提示词）             ┤ 不变
    ├─ user:   "请抽取下面正文中的实体与关系：\n\n"                      ┘ 不变
    └─ user:   <<< 正文 / 返工反馈 >>>                                        唯一变的部分

前两段对"同一领域的所有请求"完全一样（领域配置改了才会变），所以一段新闻切成
N 块、每块再审校返工，只要前缀不动，N+1 次请求都在复用同一段 KV。

三种运行方式：
  .venv/bin/python lessons/prompt_cache_probe.py            # 纯离线，不花 token
  .venv/bin/python lessons/prompt_cache_probe.py --live     # 真发请求，打印命中率
  .venv/bin/python lessons/prompt_cache_probe.py --live --warm  # 先预热再打（对比用）

--------------------------------------------------------------------------------
实测记录（2026-10-22，端点 /api/coding/v3，模型 deepseek-v4-flash-ga）
--------------------------------------------------------------------------------
① 网关【会】回传统计：usage 里有 prompt_tokens_details.cached_tokens
   （不是 DeepSeek 式的 prompt_cache_hit_tokens），agentscope 已归一成
   ChatUsage.cache_input_tokens —— 所以"看不见"曾纯粹是我们自己没读。

② 缓存真实存在，粒度 128 token，但命中不稳定。构造超长 system（3653 字符
   ≈ 2400 token）连发三请求：
      R1 首发                        prompt 2636  cached    0
      R2 同前缀，末尾多一个数字       prompt 2639  cached    0
      R3 同 system，正文彻底换掉      prompt 2405  cached 2304（≈96%）
   R3 命中而 R2 不命中，只能用"多副本 + 无会话亲和"解释：这一发落到哪个副本
   决定命中与否。预热能提高概率（副本总有一个人缓存过），但不是确定收益。

③ moray 真实抽取路径（news_long_002 切 4 块，串行 3 次）：
      块1 输入 1849 输出 2866 实体 7     块2 输入 1832 输出 2141 实体 3
      块3 输入 1850 输出 4284 实体 7     三次 cached 全 = 0
   两个数字值得记住：
   · 输入 1849 远大于「system 468 + 引导语 15 + 正文 114 ≈ 597」：剩下的
     约 1250 落在【结构化输出的 JSON Schema】上（本地估算该 schema ≈ 1453
     token，与余量同量级，两边 tokenizer 不同）。它按领域生成、逐请求不变，
     所以【真正值钱的可缓存前缀是这块 schema，不是 system 的文字】。
   · 输出 2141~4284 > 输入 1832~1850，且其中 458~796 是 reasoning_tokens
     （连"概括一句话"这种请求都会想这么多）。输出单价高于输入，
     所以【当前最大的成本项是输出侧的思考链，不是没吃到的输入缓存】。

④ 因此本课配套的三件事：读缓存字段（可见）、前缀对齐（能吃就吃）、
   以及 config 里的 ARK_REASONING_EFFORT / ARK_THINKING 开关（默认不设 =
   行为不变）。下一步额度重置（2026-10-28）后要做的两件事：
   a) 跑 --live 多次统计命中概率，决定是否保留 warm_prefix（不命中就关掉）；
   b) 用 L15 评测比较 reasoning_effort=minimal 的成本/质量曲线——抽取不需要
      思考链，但要用黄金集 P/R/F1 证明掉了多少再决定，别拍脑袋。

⑤ 本次跑到 429 AccountQuotaExceeded 停止（套餐月度额度），上面的数字都是
   停止前真实拿到的；②③ 各只发了几个请求，样本很小，别当统计结论用。

生产代码：
  src/moray/schema/factory.py     build_system_prompt / TASK_PROTOCOL
  src/moray/extraction/extractor.py  前缀组装 / warm_prefix()
  src/moray/extraction/document.py   Usage.cached_input_tokens / cache_hit_rate
  src/moray/observability/           中间件计量 + pipeline_runs 缓存命中率
  src/moray/config.py                ARK_REASONING_EFFORT / ARK_THINKING
================================================================================
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.extraction import (
    Chunker,
    ChunkedExtractionPipeline,
    Document,
    Extractor,
    estimate_tokens,
)
from moray.schema import TASK_PROTOCOL, build_system_prompt, get_domain

SAMPLE = ROOT / "data" / "samples" / "news_long_002.txt"
LIVE = "--live" in sys.argv
WARM = "--warm" in sys.argv


# -----------------------------------------------------------------------------
# 第 1 步：看清"可复用前缀"到底有多长（离线，纯算术）
# -----------------------------------------------------------------------------
def step_measure() -> None:
    print("=" * 78)
    print("第 1 步：一次请求里，哪些字节是跨请求不变的？")
    print("=" * 78)

    domain = get_domain("general")
    system = build_system_prompt(domain)
    lead = Extractor._LEAD_IN
    import json
    from moray.schema import build_extraction_model
    schema = json.dumps(
        build_extraction_model(domain).model_json_schema(), ensure_ascii=False)

    t_system = estimate_tokens(system)
    t_lead = estimate_tokens(lead)
    t_schema = estimate_tokens(schema)
    print(f"system（角色+类型枚举+属性规格+谓词+协议）：{len(system):4d} 字符"
          f" ≈ {t_system:4d} token")
    print(f"结构化输出 JSON Schema（tools 渲染进提示词）  ：{len(schema):4d} 字符"
          f" ≈ {t_schema:4d} token")
    print(f"user 固定引导语                       ：{len(lead):4d} 字符"
          f" ≈ {t_lead:4d} token")
    stable = t_system + t_schema + t_lead
    print(f"=> 可复用稳定前缀 ≈ {stable} token"
          f"（其中 schema 占 {t_schema / stable:.0%}——"
          "结构化输出的 schema 才是前缀的大头，它按领域生成、逐请求不变）")

    text = SAMPLE.read_text(encoding="utf-8").strip()
    chunks = Chunker(max_units=120, overlap_units=0).chunk_text(text)
    body_tokens = [estimate_tokens(c.text) for c in chunks]
    print(f"\n样本 news_long_002.txt 切成 {len(chunks)} 块，"
          f"每块正文 ≈ {body_tokens} token")
    avg_body = int(sum(body_tokens) / len(body_tokens))
    ratio = stable / (stable + avg_body)
    print(f"若缓存全部生效，理论上限：每次请求约 {ratio:.0%} 的输入可走缓存折扣"
          f"（前缀 {stable} / 单次输入 {stable + avg_body}）")
    print("⚠️ 这是【上限】不是实测：网关是否真的让你命中、TTL 多长，"
          "见文件头的实测记录与第 4 步。")


# -----------------------------------------------------------------------------
# 第 2 步：前缀稳定性自证（离线）
# -----------------------------------------------------------------------------
def step_stability() -> None:
    print("\n" + "=" * 78)
    print("第 2 步：逐字节比对——同领域两次构造、不同块之间，前缀是否一致")
    print("=" * 78)

    # ① 同领域两个 Extractor 实例（模拟 worker 并发里两份对象）
    a, b = Extractor("general"), Extractor("general")
    assert a._system_prompt == b._system_prompt
    print("✅ 同领域两个实例的 system 逐字节相同 → 前缀可跨请求复用")

    # ② 两块正文的 user 消息：公共前缀 = 固定引导语
    text = SAMPLE.read_text(encoding="utf-8").strip()
    real_chunks = Chunker(max_units=120, overlap_units=0).chunk_text(text)
    p1 = Extractor._first_prompt(real_chunks[0].text)
    p2 = Extractor._first_prompt(real_chunks[1].text)
    common = 0
    for c1, c2 in zip(p1, p2):
        if c1 != c2:
            break
        common += 1
    print(f"✅ 相邻两块真实正文，user 消息公共前缀 = {common} 字符"
          f"（= 引导语 {len(Extractor._LEAD_IN)} 字符；正文一开头就分岔，"
          "而正文之后没有任何固定文字，变化点被推到了最后）")

    # ③ 反例：把时间戳/文档标题塞进 system —— 每次请求都换前缀，缓存永久失效
    polluted = a._system_prompt + f"\n今天时间：{time.strftime('%X')}"
    print(f"❌ 反例：system 尾部加一个时间戳，两条请求的前缀从第 "
          f"{len(a._system_prompt)} 字符起就不同 → 之后每次都命不中")
    assert polluted != a._system_prompt

    # ④ 领域配置变了才会换前缀（这正是我们想要的粒度）
    fin = Extractor("finance")
    print(f"✅ 换领域才会换前缀：{a.prefix_version} vs {fin.prefix_version}"
          "（prefix_version 就是排查'为什么没命中'时的指纹）")

    # ⑤ 返工不重复发正文：差额正好是正文本身
    err = "字段 entities.0.type: 输入不是合法的枚举值"
    old_retry = (
        "你上一次的结构化输出没有通过校验，错误如下：\n"
        f"{err}\n\n"
        "请针对这些错误修正后【重新输出完整结果】（不要输出解释文字）。\n"
        f"原始文本：\n{text}"
    )
    new_retry = Extractor._retry_prompt(err)
    print(f"\n返工一轮的额外输入：旧写法 {estimate_tokens(old_retry)} token"
          f"（正文重发一遍） vs 现在 {estimate_tokens(new_retry)} token"
          f"（只追加反馈，差额 {estimate_tokens(text)} ≈ 正文本身）")
    print("   正文和上一次的输出都还在对话历史里，重发既多付钱、"
          "又让本轮消息与已缓存的历史不同形。")
    print(f"   任务协议是否已进 system：{TASK_PROTOCOL in a._system_prompt}")


# -----------------------------------------------------------------------------
# 第 3 步：预热在做什么（离线，用假模型看调用序列）
# -----------------------------------------------------------------------------
async def step_warmup_plan() -> None:
    print("\n" + "=" * 78)
    print("第 3 步：warm_prefix() 的调用序列（假模型，不联网）")
    print("=" * 78)

    class _Spy(Extractor):
        """只记录调用，不真发请求。"""

        def __init__(self):
            super().__init__("general", max_retries=0)
            self.sent: list[str] = []

        def _build_agent(self):
            # 真实 extract() 会往 agent 上挂 _audit_source_text，
            # SimpleNamespace 允许任意属性，object() 不行
            return SimpleNamespace()

        async def _call_structured(self, agent, content):
            self.sent.append(content)
            return SimpleNamespace(
                structured_output={"entities": [], "relations": []},
                usage=SimpleNamespace(input_tokens=500, output_tokens=3,
                                      cache_input_tokens=0),
            )

    text = SAMPLE.read_text(encoding="utf-8").strip()
    chunks = Chunker(max_units=120, overlap_units=0).chunk_text(text)

    spy = _Spy()
    ex = ChunkedExtractionPipeline(spy, concurrency=3, warm_prefix=True)
    out = await ex.run(Document(id="d", text=text), chunks)
    print(f"{len(chunks)} 块 + 预热开关开：模型调用 {len(spy.sent)} 次"
          f"（1 次预热 + {len(chunks)} 次抽块）")
    print(f"  预热请求内容：{spy.sent[0]!r}")
    print(f"  记入成本的用量：输入 {out.usage.input_tokens}"
          f" / 缓存命中 {out.usage.cached_input_tokens}"
          f" / 命中率 {out.usage.cache_hit_rate:.0%}（假模型如实报 0）")

    spy2 = _Spy()
    await ChunkedExtractionPipeline(spy2, warm_prefix=False).run(
        Document(id="d", text=text), chunks)
    print(f"关掉预热：模型调用 {len(spy2.sent)} 次 —— N 个块一起冲向未建的缓存")

    print("\n为什么预热只发一发：Extractor 按 (领域, 配置版本) 去重，"
          "同一进程里同领域只探一次；配置热更换了版本才会重探。")


# -----------------------------------------------------------------------------
# 第 4 步：真请求实测命中率（--live 才跑）
# -----------------------------------------------------------------------------
async def step_measure_live() -> None:
    print("\n" + "=" * 78)
    print("第 4 步：真实请求，逐次打印 cached_tokens")
    print("=" * 78)

    text = SAMPLE.read_text(encoding="utf-8").strip()
    chunks = Chunker(max_units=120, overlap_units=0).chunk_text(text)
    ex = Extractor("general", max_retries=0)
    print(f"模型 {ex.model_name}｜领域 {ex.domain.code}｜前缀指纹 "
          f"{ex.prefix_version}｜{len(chunks)} 块\n")

    async def call(label: str, chunk_text_: str) -> None:
        out = await ex.extract(Document(id=label, text=chunk_text_,
                                        source="probe", domain_code="general"))
        u = out.usage
        print(f"{label:10} 输入 {u.input_tokens:6d}  输出 {u.output_tokens:4d}"
              f"  缓存命中 {u.cached_input_tokens:6d}"
              f"  命中率 {u.cache_hit_rate:6.1%}"
              f"  抽出实体 {len(out.result.entities)}")

    try:
        if WARM:
            probe = await ex.warm_prefix(force=True)
            print(f"{'预热探测':10} 输入 {probe.input_tokens:6d}"
                  f"  输出 {probe.output_tokens:4d}"
                  f"  缓存命中 {probe.cached_input_tokens:6d}")
        else:
            print("（未加 --warm：直接开抽，看并发首发的原始表现）")

        # 串行发（缓存建立需要时间；并发首发容易一起 miss）
        for i, c in enumerate(chunks[:3], start=1):
            await call(f"块{i}", c.text)
    except Exception as exc:  # noqa: BLE001 探针脚本：把网关错误翻译成人话
        msg = str(exc)
        print(f"\n❌ 请求失败：{type(exc).__name__}")
        if "429" in msg or "QuotaExceeded" in msg:
            print("   套餐额度用尽（按周期重置）。等重置、换 ARK_CHAT_MODEL，"
                  "或改用平台按量端点（/api/v3）再跑；离线结论不受影响。")
        else:
            print(f"   {msg[:300]}")
        return

    print("\n判读：")
    print("  · cached 一直是 0 → 网关没回传 cached_tokens，或该端点未启用缓存；"
          "此时预热是纯浪费，该关掉。")
    print("  · 第一发 0、后续 >0 → 自动前缀缓存生效，预热/前缀对齐有价值。")
    print("  · cached ≈ 前缀长度（第 1 步算的那个数） → 吃满了。")
    print("  · 命中时有时无 → 网关多副本但没有会话亲和性：命中取决于这一发"
          "落到哪个副本，预热只能提高概率、不能保证。想要确定收益得用显式缓存"
          "（Ark 上下文缓存 / cache_id 一类），见下方实测记录。")
    print("  · cached < 前缀长度且稳定 → 前缀被某处不稳定内容截断，"
          "对比 prefix_version 找差异。")


async def main() -> None:
    step_measure()
    step_stability()
    await step_warmup_plan()
    if LIVE:
        await step_measure_live()
    else:
        print("\n" + "=" * 78)
        print("加 --live 真发请求测命中率（会消耗少量 token）")
        print("  .venv/bin/python lessons/prompt_cache_probe.py --live")
        print("  .venv/bin/python lessons/prompt_cache_probe.py --live --warm")
        print("=" * 78)
        return

    print("\n# 动手任务：")
    print("# 1. --live 不带 --warm 跑一遍，再 --live --warm 跑一遍，比第一发命中率。")
    print("# 2. 把 Chunker(max_units=...) 调大，让正文变长 → 观察命中率下降"
          "（前缀占比变小，这是正常的）。")
    print("# 3. 跑一次 lessons/l13_observability.py，在 /metrics/dashboard 里"
          "看 cache_hit_rate 是否被持续记录。")


if __name__ == "__main__":
    asyncio.run(main())
