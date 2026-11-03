"""
压力案卷黄金语料生成器（工单 02）
================================

把 moray-2 实验期的 ``lessons/gen_stress_doc.py`` 升级为**可离线回归的评测
资产生成器**：给定显式随机种子，确定性地产出一篇公安案卷风格的长文，并同步
产出「带类型的实体规范名清单 + 关系三元组标准答案」。

与实验脚本的关键差异（决定答案可信）
------------------------------------
1. 实体答案带类型（PER/ORG/LOC/EVENT/MISC），不再是一份扁平 gazetteer；
2. **实体过滤**：只保留规范名在最终干净正文中字面出现的词条
   （生成过程会刻意保留一些不写进正文的候选，必须被这道闸门剔除）；
3. **关系过滤**：每条关系的头、尾名称都必须在正文中可定位、且都指向
   过滤后实体集合里的规范名，否则丢弃；
4. 人名/地名用集合去重 + 固定顺序产出，消除 hash-seed 导致的跨进程不确定性，
   保证同一种子两次生成的正文与答案逐字节一致。

**gold 是刻意子集（绝对 P 不可跨抽取器横比）**：为控制规模，正文里还有
若干「句面陈述但未写进答案」的关系，例如每篇 18 句「法定代表人{PER}」、
卷四每个案子列出 5 名涉案人却只记第一个人的「涉案关联」。确定性规则抽取器
不建模这些谓词，所以当前无 FP；但工单 04 注入真实 LLM 路径做 A/B 时，模型
读出的这些句面会被记 FP，绝对 precision 会被低估。A/B 只应比较同一被评对象
在不同块预算下的**增量**，不要跨抽取器比较绝对 P。该限制也写入
``runner.build_report`` 的 ``gold_scope_note``。

刻意埋入的三类边界压力（与 chunk_boundary_probe 的设计一致）
----------------------------------------------------------
· 远距关系：头在卷一、尾首次出现在数千字之后的卷五（办案地位于 / 追赃方向
  指向），卷五句面重复头名，供句内规则抽取；
· 超长无句号句：逼 Chunker 走 _hard_split，并在硬切点附近密集堆实体名；
· 高密度列举：同一句并列多个公司/人名，压召回随块预算的变化；
· **块预算敏感关系长句**：每条 >1500 units 的合语句里同时含关系头/尾，
  句面是「头，……叙述……，相关材料显示其<触发词><尾>，现予确认。」——
  头在句首、触发词与尾在句末紧邻，正文真正陈述该三元组；
  1500 档必被硬切拆开、3500 档可整句保留；每篇 4 条不同头名/谓词
  （办案地位于 / 追赃方向指向 / 关联物证）——给「调大块预算→关系召回
  提升」一个两位数条关系的真实信号（而不是 1% 余量或指标恒定）。

本模块只造数据、不做抽取，纯函数式、零副作用（写盘交给 serialize/CLI）。
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------------------
# 固定回归种子：每个种子一篇压力案卷，进黄金数据目录随仓提交。
# 选三个不同的固定整数（不是 1/2/3，避免与手写示例混淆）。
# ---------------------------------------------------------------------------
STRESS_SEEDS: tuple[int, ...] = (20261102, 20261103, 20261104)

# ---------------------------------------------------------------------------
# 实体池：名字都是完整规范名，保证字面匹配可在正文中定位
# ---------------------------------------------------------------------------
SURNAMES = list("王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧")
GIVEN = ["建国", "国庆", "秀兰", "志强", "海燕", "俊杰", "丽娟", "德海", "晓峰",
         "桂芳", "立军", "永刚", "春梅", "卫东", "小雪", "浩然", "思远", "梓萱"]

ORG_PREFIX = ["临江宏远", "城南信达", "北辰恒泰", "东港联合", "西江盛通",
              "云岭金鼎", "昌平安瑞", "桥南顺达", "裕民广泰", "锦江华诚"]
ORG_KIND = ["建筑工程有限公司", "物流有限责任公司", "商贸有限公司",
            "实业集团有限公司", "运输有限公司", "机械制造有限公司"]

LOC_POOL = ["临江区", "桥南区", "云岭市", "东港新区", "西江县城",
            "北辰街道", "昌平市", "江景路", "海丰大道", "锦城巷",
            "望江路", "青云镇", "白石桥", "铜官山区", "玉泉街道"]

# decoy 实体统一带这个前缀：结构上不可能与正文真实名称撞名，
# 避免"刻意不可定位"的候选被真实同名实体"救活"、混进黄金答案。
DECOY_PREFIX = "未入卷"

# gold 规范谓词 -> 正文里的触发词。生成器写句、抽取器找左触发词、
# 语义闸门校验句面都只认这一张表（单一来源，防止三处文案漂移）。
PREDICATE_TRIGGERS: dict[str, str] = {
    "供职于": "曾在",
    "户籍在": "户籍地",
    "注册地位于": "注册地位于",
    "涉案关联": "案件编号",
    "关联物证": "关联到关键物证",
    "办案地位于": "办案地位于",
    "追赃方向指向": "追赃方向指向",
}


CASE_KINDS = ["系列盗窃案", "合同诈骗案", "寻衅滋事案", "故意损毁财物案",
              "非法经营案", "聚众斗殴案"]
CLUE_ITEMS = ["断线钳", "液压剪", "改装电动车", "伪造公章", "现金赃款",
              "作案手机", "手套鞋印", "监控视频片段", "银行流水单", "出入门禁记录"]

FILLER_SUBJ = ["办案民警", "现场勘查人员", "报案人", "见证人", "社区工作人员",
               "物业值班员", "被害人委托代理人", "值班辅警", "技术中队民警"]
FILLER_VERB = ["对周边监控进行了逐帧排查", "走访了相邻三个小区的门卫",
               "核实了当事人近三个月的活动轨迹", "调取了沿街商铺的收银记录",
               "复核了物证保管链条的登记信息", "整理了证人询问笔录的时间线",
               "比对了现场提取的指纹与鞋印", "复查了涉案账户的资金往来",
               "询问了当晚值班的全部保安人员", "核验了车辆卡口的通行记录",
               "回访了案发地点附近的商户", "补充了辨认笔录的法律手续",
               "固定了电子数据的哈希校验值", "核对了传唤与拘留的时间节点",
               "梳理了同案人员之间的通讯联系", "评估了涉案财物的鉴定价值",
               "复查了户籍与暂住登记信息", "确认了看守所收押的体检材料",
               "翻拍并归档了现场方位照片", "向检察机关通报了侦查进展"]


@dataclass(frozen=True)
class StressEntity:
    """标准答案实体：规范名 + 受控类型。"""

    name: str
    type: str  # PER / ORG / LOC / EVENT / MISC


@dataclass(frozen=True)
class StressRelation:
    head: str
    predicate: str
    tail: str


@dataclass
class StressCase:
    """一篇压力案卷：干净正文 + 程序过滤后的标准答案。"""

    seed: int
    name: str
    text: str
    entities: list[StressEntity] = field(default_factory=list)
    relations: list[StressRelation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 确定性造名：集合去重后按【生成顺序】产出，不依赖 set 迭代序
# ---------------------------------------------------------------------------
def make_people(rng: random.Random, n: int) -> list[str]:
    out: list[str] = []
    used: set[str] = set()
    while len(out) < n:
        cand = rng.choice(SURNAMES) + rng.choice(GIVEN)
        if cand not in used:
            used.add(cand)
            out.append(cand)
    return out


def make_orgs(rng: random.Random, n: int) -> list[str]:
    out: list[str] = []
    used: set[str] = set()
    while len(out) < n:
        name = rng.choice(ORG_PREFIX) + rng.choice(ORG_KIND)
        if name not in used:
            used.add(name)
            out.append(name)
    return out


def pid(rng: random.Random) -> str:
    return f"11010119{rng.randint(80, 99)}{rng.randint(1000, 1231)}{rng.randint(1000, 9999)}X"


def case_no(rng: random.Random, year: int) -> str:
    """案号也吃种子：三个种子各写各的编号，避免 EVENT 答案完全相同。"""
    return f"A{year}-{rng.randint(100, 999):03d}"


def sentence(rng: random.Random) -> str:
    return rng.choice(FILLER_SUBJ) + rng.choice(FILLER_VERB) + "。"


def para(rng: random.Random, k: int) -> str:
    return "".join(sentence(rng) for _ in range(k))


def long_runon(rng: random.Random, orgs: list[str], people: list[str],
               target_chars: int) -> str:
    """约 target_chars 字、中间无句末标点的供述长句（仅逗号），逼 hard_split。"""
    seq: list[str] = ["当晚我接触和往来的人员单位有"]
    total = len(seq[0])
    while total < target_chars:
        if rng.random() < 0.85:
            token = rng.choice(orgs) if rng.random() < 0.7 else rng.choice(people)
        else:
            token = rng.choice([
                "搬了两箱东西", "绕开正门监控", "把工具放后备箱",
                "联系收赃下家", "沿河堤步行", "在便利店买水",
                "换乘两辆出租车", "用现金结账",
            ])
        seq.append(token)
        total += len(token) + 1
    return "，".join(seq) + "。"


def long_relation_sentence(rng: random.Random, head: str, tail: str,
                           target_units: int, *,
                           trigger: str) -> str:
    """一条 >生产块预算、含关系头/尾且中间无句末标点的合语句。

    句面结构固定为「头，……（不含实体的叙述）……，相关材料显示其<触发词><尾>，
    现予确认。」：头在句首、触发词与尾在句末**紧邻**，因此正文确实陈述了
    gold 三元组（而不是旧实现里「头+触发词被逗号截断、尾在 1900 units 之后」
    那种只有位置巧合的写法）。

    1500 档 Chunker 会 hard_split 把它拆成两半（头尾分居两块，关系必丢），
    3500 档整句留在同一块（句内回退到最近的句首主语，关系可抽到）。
    """
    prefix = f"{head}，"
    ending = f"相关材料显示其{trigger}{tail}，现予确认。"
    parts: list[str] = []
    total = len(prefix) + len(ending)
    while total < target_units:
        token = rng.choice(FILLER_VERB)
        parts.append(token)
        total += len(token) + 1
    middle = "，".join(parts)
    return prefix + (middle + "，" if middle else "") + ending


def build_text(rng: random.Random, *, target_chars: int,
               max_units: int
               ) -> tuple[str, list[StressEntity], list[StressRelation]]:
    """造正文。

    返回 (正文, 候选实体[含类型,含刻意 decoy], 候选关系[含刻意不可定位项])。
    候选里**故意混入**正文不出现的实体与头/尾不可定位的关系，由
    :func:`generate_case` 的过滤闸门统一剔除——这样过滤逻辑是真的在干活，
    而不是靠巧合全命中。
    """
    n_people = 40
    n_orgs = 18
    people = make_people(rng, n_people)
    orgs = make_orgs(rng, n_orgs)
    locs = list(dict.fromkeys(LOC_POOL))
    cases: list[str] = []
    while len(cases) < 8:
        no = case_no(rng, 2024)
        if no not in cases:
            cases.append(no)

    tail_locs = [l for l in locs[10:14]]
    early_locs = [l for l in locs if l not in tail_locs]

    md: list[str] = ["# 临江区公安案卷材料汇编（压力测试生成卷）\n"]
    relations: list[StressRelation] = []

    head_orgs = orgs[:4]
    distal_pairs = list(zip(head_orgs, tail_locs))
    distal_head_orgs = set(head_orgs)
    distal_tail_locs = set(tail_locs)
    reuse_orgs = [o for o in orgs if o not in distal_head_orgs]

    mid_people = people[-4:]
    mid_items = CLUE_ITEMS[6:10]
    mid_pairs = list(zip(mid_people, mid_items))
    mid_people_set = set(mid_people)
    reuse_people = [p for p in people if p not in mid_people_set]

    # 刻意保留一批"永不写进正文"的候选实体，验证实体过滤闸门有效。
    # 名字统一加 DECOY_PREFIX：旧实现用 make_people/make_orgs 随机造名，
    # 可能与真实 ORGS 撞名，使"刻意不可定位"的 decoy 关系通过可定位闸门、
    # 变成正文不支撑的语义假答案。
    decoy_entities = (
        [StressEntity(f"{DECOY_PREFIX}人员{n}", "PER")
         for n in "甲乙丙"]
        + [StressEntity(f"{DECOY_PREFIX}单位{n}", "ORG")
           for n in "丁戊"]
    )

    # ---- 卷一：人员与单位 ----
    md.append("## 一、涉案人员与单位\n")
    roster = [p for p in people if p not in mid_people_set]
    for i, p in enumerate(roster):
        org = orgs[i % len(orgs)]
        loc = rng.choice(early_locs)
        md.append(
            f"{p}，{rng.choice(['男', '女'])}，身份证号 {pid(rng)}，"
            f"户籍地{loc}，曾在{org}任职。")
        relations.append(StressRelation(p, "供职于", org))
        relations.append(StressRelation(p, "户籍在", loc))
    md.append("")
    for o in orgs:
        loc = rng.choice(early_locs)
        md.append(f"{o}，统一社会信用代码 91{rng.randint(10 ** 15, 10 ** 16 - 1)}X，"
                  f"注册地位于{loc}，法定代表人{rng.choice(reuse_people)}。")
        relations.append(StressRelation(o, "注册地位于", loc))
    md.append("")

    # ---- 远端关系：头在卷一首次且唯一出现，尾保留到卷五 ----
    for h, _ in distal_pairs:
        md.append(f"侦查阶段，{h} 相关账目被先行调阅，后续追赃另行部署。")
    md.append("")

    # ---- 卷二：常规侦查叙述（低密度、撑篇幅）----
    md.append("## 二、侦查经过\n")
    body_budget = int(target_chars * 0.62)
    body = 0
    mid_head_done = False
    mid_tail_done = False
    while body < body_budget:
        if not mid_head_done and body >= body_budget * 0.28:
            md.append("前期排查锁定重点人员 "
                      + "、".join(p for p, _ in mid_pairs) + "。")
            mid_head_done = True
        if not mid_tail_done and body >= body_budget * 0.38:
            # 每个 (人, 物证) 各自成句：头尾必须落在同一句面才能被
            # 句内规则抽取器命中。旧写法把 4 个人和 4 件物证分列两段，
            # 任何块预算下都抽不到（36/331 gold 关系结构性不可达）。
            for p, t in mid_pairs:
                # 人名后紧跟逗号：PER 启发式按"名字后一个字符是边界"识别，
                # 直接接动词会认不出人名，使关系在任何块预算下都抽不到。
                md.append(f"后续鉴定中，{p}，关联到关键物证 {t}，鉴定意见已附卷。")
                relations.append(StressRelation(p, "关联物证", t))
            mid_tail_done = True
        chunk = para(rng, rng.randint(3, 6))
        if rng.random() < 0.3:
            back_ref = rng.choice(reuse_people)
            chunk = f"围绕{back_ref}的社会关系，" + chunk
        md.append(chunk)
        body += len(chunk)
    md.append("")

    # ---- 卷三：超长供述句（hard_split 压力区）----
    md.append("## 三、讯问笔录节选\n")
    md.append("讯问时间：2024 年 6 月 12 日 21 时。供述主要内容如下。")
    md.append("办案人员问：当晚的具体经过？")
    md.append("答：" + long_runon(rng, reuse_orgs, reuse_people,
                                  target_chars=max(3200, max_units * 2 + 400)))
    md.append("")
    md.append("补充供述：" + long_runon(
        rng, reuse_orgs, reuse_people, target_chars=max_units + 300))
    md.append("")

    # ---- 卷四：高密度并列句 ----
    md.append("## 四、涉案单位与线索清单\n")
    for c in cases:
        involved_p = rng.sample(reuse_people, 5)
        involved_o = rng.sample(reuse_orgs, 4)
        item = rng.choice([c for c in CLUE_ITEMS if c not in mid_items])
        md.append(
            f"案件编号 {c} 涉及 {('、'.join(involved_p))} 等人员，"
            f"关联单位包括 {('、'.join(involved_o))}，现场提取到{item}。")
        relations.append(StressRelation(involved_p[0], "涉案关联", c))
    md.append("")

    # ---- 卷五：远距关系的"尾"首次出现在文末 ----
    # 句面重复头名：头虽然在卷一出现过，但句内规则抽取器看不到块外文字，
    # 若这里只写"前述第 N 家调账单位"，任何块预算下都抽不到这两类关系。
    md.append("## 五、异地协作与追赃\n")
    tail_budget = int(target_chars * 0.12)
    tailed = set()
    spent = 0
    di = 0
    while spent < tail_budget:
        idx = di % len(distal_pairs)
        h, t = distal_pairs[idx]
        md.append(
            f"经异地协作，{h}的办案地位于{t}，"
            f"追赃方向指向{t}；协作函已抄送当地派出所。")
        if t not in tailed:
            tailed.add(t)
            relations.append(StressRelation(h, "办案地位于", t))
            relations.append(StressRelation(h, "追赃方向指向", t))
        spent += 120
        di += 1
        md.append(para(rng, rng.randint(2, 4)))
    md.append("")

    # ---- 卷六：块预算敏感的关系长句（>1500 units、<3500 units）----
    # 4 条不同头名/谓词的长句：1500→3500 的关系召回差异是两位数条关系，
    # 而不是 1% 余量；其中两类谓词（办案地位于/追赃方向指向/关联物证）
    # 分别覆盖 ORG-LOC 与 PER-MISC 两种类型组合。
    md.append("## 六、协作续查\n")
    # 长句前放一条 >48 字、且不含任何实体名的过渡句：保证长句头名的
    # 左侧 48 字窗口里没有别的实体，避免"就近回填"抓错头。
    transition = ("办案人员对异地协作与追赃事项逐项复核后，未发现新的涉案单位线索，"
                  "相关材料已按程序归档备查，手续齐全，不存在遗漏。")
    long_relations = [
        (orgs[4], tail_locs[0], "办案地位于", "办案地位于"),
        (orgs[5], tail_locs[1], "追赃方向指向", "追赃方向指向"),
        (people[0], CLUE_ITEMS[5], "关联物证", "关联到关键物证"),
        (people[1], CLUE_ITEMS[0], "关联物证", "关联到关键物证"),
    ]
    for head, tail, predicate, trigger in long_relations:
        md.append(transition)
        md.append(long_relation_sentence(
            rng, head, tail, target_units=max_units + 400,
            trigger=trigger))
        md.append("")
        relations.append(StressRelation(head, predicate, tail))

    text = "\n".join(md)

    # 植入候选实体（带类型），并把永不落正文的 decoy 一并放进候选集，
    # 交给 generate_case 的过滤闸门剔除。
    planted: list[StressEntity] = []
    planted += [StressEntity(n, "PER") for n in people]
    planted += [StressEntity(n, "ORG") for n in orgs]
    planted += [StressEntity(n, "LOC") for n in locs]
    planted += [StressEntity(n, "EVENT") for n in cases]
    planted += [StressEntity(n, "MISC") for n in CLUE_ITEMS]
    planted += decoy_entities

    # 刻意追加头/尾不可定位的关系候选（decoy 实体从不写进正文）
    if decoy_entities:
        dp = next(e.name for e in decoy_entities if e.type == "PER")
        do = next(e.name for e in decoy_entities if e.type == "ORG")
        relations.append(StressRelation(dp, "供职于", do))
        relations.append(StressRelation(people[0], "供职于", do))
    return text, planted, relations


def relation_semantically_supported(
    text: str, entities: list[StressEntity], relation: StressRelation
) -> bool:
    """正文是否有一句**语义上直接陈述**该三元组（而不只是头尾都能定位）。

    判据（r3 评审修正）：
    · 头名出现在触发词之前（涉案关联例外：头在案号之后）；
    · 触发词与尾名**相邻**（只容忍空格/制表符，不容忍夹进别的词或标点）；
    · 涉案关联还要求尾（案号）与头之间没有插入另一个 PER。

    旧版只要求「触发词与尾之间没有同类型实体」——块预算长句里同类型实体
    只有尾名一个，断言恒真。现在尾必须紧跟触发词，换成另一个同样可定位的
    实体就会被拒绝。
    """
    trigger = PREDICATE_TRIGGERS.get(relation.predicate)
    if trigger is None:
        return False
    type_of = {e.name: e.type for e in entities}
    by_type: dict[str, set[str]] = {}
    for e in entities:
        by_type.setdefault(e.type, set()).add(e.name)
    head_type = type_of.get(relation.head)

    for line in text.splitlines():
        ti = line.find(trigger)
        if ti < 0 or relation.head not in line or relation.tail not in line:
            continue
        ti_end = ti + len(trigger)
        if relation.predicate == "涉案关联":
            tail_pos = line.find(relation.tail, ti_end)
            if tail_pos < 0:
                continue
            hi = line.find(relation.head, tail_pos + len(relation.tail))
            if hi < 0:
                continue
            between = [n for n in by_type.get(head_type or "", ())
                       if n != relation.head
                       and n in line[tail_pos + len(relation.tail):hi]]
            if not between:
                return True
            continue
        if line.rfind(relation.head, 0, ti) < 0:
            continue
        rest = line[ti_end:]
        if rest.startswith(relation.tail) or rest.lstrip(" \t").startswith(
                relation.tail):
            return True
    return False


def validate_case_semantics(case: StressCase) -> list[str]:
    """语义闸门：返回正文没有直接陈述的关系三元组（空列表才通过）。

    与 :func:`validate_case` 的可定位闸门互补；落盘（gen_stress_gold.py）与
    装载（loader.load_stress_case）都经由 ``validate_case`` 调到它。
    """
    return [
        f"{r.head}-{r.predicate}->{r.tail}"
        for r in case.relations
        if not relation_semantically_supported(case.text, case.entities, r)
    ]


def validate_case(case: StressCase) -> tuple[list[str], list[str]]:
    """答案可信闸门（与 generate_case 内的落盘前过滤同一口径）。

    返回 (缺失实体名, 不可信关系三元组列表)；两者皆空才算通过。
    实体：规范名必须在干净正文中字面出现。
    关系：先查头/尾可定位且都指向实体答案的规范名（可定位闸门），再查正文
    是否有一句在语义上直接陈述它（:func:`validate_case_semantics`）。
    两道闸门合并返回，调用方无需分别记两套返回值。
    """
    ent_names = {e.name for e in case.entities}
    missing_entities = [
        e.name for e in case.entities if e.name not in case.text]
    missing_relations: list[str] = []
    for r in case.relations:
        if (r.head not in case.text or r.tail not in case.text
                or r.head not in ent_names or r.tail not in ent_names):
            missing_relations.append(f"{r.head}-{r.predicate}->{r.tail}")
    missing_relations.extend(validate_case_semantics(case))
    return missing_entities, missing_relations


def generate_case(seed: int, *, target_chars: int = 12000,
                  max_units: int = 1500, name: str | None = None) -> StressCase:
    """按种子确定性生成一篇压力案卷，并过滤出文本可支撑的标准答案。"""
    rng = random.Random(seed)
    text, candidates, relations = build_text(
        rng, target_chars=target_chars, max_units=max_units)

    # 实体过滤：剔除未在干净正文中字面出现的候选（含刻意 decoy），按类型保序去重
    entities: list[StressEntity] = []
    seen_names: set[str] = set()
    for cand in candidates:
        if cand.name in seen_names or cand.name not in text:
            continue
        seen_names.add(cand.name)
        entities.append(cand)

    # 关系过滤：头/尾可定位且都属于过滤后实体集；三元组去重
    ent_names = {e.name for e in entities}
    kept_relations: list[StressRelation] = []
    rel_seen: set[tuple[str, str, str]] = set()
    for r in relations:
        key = (r.head, r.predicate, r.tail)
        if key in rel_seen:
            continue
        if (r.head in text and r.tail in text
                and r.head in ent_names and r.tail in ent_names):
            rel_seen.add(key)
            kept_relations.append(r)

    return StressCase(
        seed=seed,
        name=name or f"stress_seed{seed}",
        text=text,
        entities=entities,
        relations=kept_relations,
    )


# ---------------------------------------------------------------------------
# 落盘 / 载入：正文 .md + 答案 .answer.json（与 gold 的 .relations.json 同风格，
# 但实体也带类型，故使用独立 sidecar）
# ---------------------------------------------------------------------------
def serialize_case(case: StressCase) -> dict:
    return {
        "seed": case.seed,
        "name": case.name,
        "entities": [asdict(e) for e in case.entities],
        "relations": [asdict(r) for r in case.relations],
    }
