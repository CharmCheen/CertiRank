# Gate 3 独立实验审计报告（GATE3_INDEPENDENT_AUDIT）

- 审计日期：2026-10-16
- 审计对象：`data/gate3_nfcorpus/formal/`（Gate 3 正式实验，协议 `0.3.0-research-pilot`）
- 审计代码版本：git `6ee9919f0ab228f9b56ffd44f8d18011d26f7085`（工作树除本审计目录外干净）
- 审计实现：`audit_gate3_independent/scripts/`（**零 `certirank` 导入**的独立重实现；仅读取原始 JSON/JSONL 数据与冻结 `value_model.json`）
- 审计产物：`audit_gate3_independent/outputs/*.json`
- 审计约束遵守情况：未调用任何外部 LLM API；未扩大实验；未修改冻结协议、原始数据与既有代码；所有新分析脚本与输出均位于新目录 `audit_gate3_independent/`。

---

## 0. 执行摘要（TL;DR）

**实验是真实的、正确的、可完全复现的。** 我用一套独立重实现的执行引擎（锦标赛、复核账本、策略、价值模型评分全部从协议语义重写）精确复现了全部报告数字：非确定性统计（ever-flip 24.4%/16.7%、pairwise disagreement 8.7%/6.0%）、主比较 D_EM/gain/bootstrap/positive groups/调用数，以及 432 次单独执行（dev 144 + heldout 288）的每一次最终 Top-K 与调用计数，0 处不一致。

**ReviewValue 失败的机制不是 bug，而是机制性无能力，且天花板极低。** 五层证据链：(1) heldout 9/12 组 Base D_EM 已为 0，无改善空间（地板效应）；(2) 复核探针本身几乎无翻转力（翻转要求两个探针槽位同时反对初始槽位，率仅 2.75%/3.87%）；(3) dev 拟合的价值模型 250 个样本中仅 2 个正增益（噪声水平），导致 0.2 预算下 0 次探针、0.4 预算下恰好 1 次探针后即停；(4) 锦标赛吸收了 4 次已观测翻转中的 3 次；(5) 事后完美 Oracle 在 +40% 预算下也只能把 heldout 平均 D_EM 从 0.153 降到 0.083（abs 0.069，集中在 2/12 组，其中 1 组完全不可修复），且该 Oracle 依赖在线策略原理上不可观测的跨执行信息。**Vote3（全量验证、3 倍调用）的 D_EM（0.167）反而高于 Base（0.153）**——即使无限验证也无法降低该工作负载上的分歧。

**裁决：停止当前路线（原报告选项 A），保留现象级结论（H0）。** 详见 §6。

---

## 1. Audit A：实验正确性与独立复算

### 1.1 数据契约（C1–C8）独立核验 — 全部通过，0 问题

对 4 个池（formal dev / formal heldout / calibration run_A / run_B）共 6,368 条记录：

| 检查 | 方法 | 结果 |
|---|---|---|
| 记录数与维度 | 独立计数 | dev 2,016（6 查询×28 对×12 重复）、heldout 4,032（12×28×12）、校准 160+160，与报告的 6,368 全部一致 |
| request_hash | 从 workload 内容（query 文本、文档文本 SHA-256、位置、身份字段）独立重建规范 payload 并重算 SHA-256 | 全部与存储值一致（无法用复制哈希伪造） |
| verdict 解析 | 独立正则解析器（"最后一个独立 Document 1/2 行，否则最后一次提及"） | 6,368/6,368 与 `strict_parsed_decision` 一致；0 条 invalid |
| provider_request_id | 全局查重（跨 4 个文件） | 6,368 个唯一 ID，0 重复、0 跨文件碰撞 |
| 时间戳 | UTC 带时区解析、文件内单调性 | 全部合法；dev 08:34–09:51 UTC、heldout 09:54–12:00 UTC，文件序单调率 1.0；最大间隙 47.3s 出现在 heldout 第 3,887 行前，与文档记载的续采点吻合 |
| run_id / 重复覆盖 | 逐 (query, pair, rep) 计数 | 每 slot 恰好 1 条；run_id 语义一致 |
| 原始响应形态 | 文本计数 | 6,334/6,368 为纯 "Document 1"/"Document 2"（dev 13、heldout 21、校准 0 条带推理文本；均被 last-mention 规则正确解析，含 1 条 `**Document 2**` 加粗格式） |
| 池文件哈希 | SHA-256 vs `pool_manifest.json` | 6/6 文件全部匹配 |
| 冻结链 | 重算 implementation/settings/manifest/model 哈希 | 与 `freeze_manifest.json` 全部匹配；`value_model` 的 `settings_hash` 与冻结配置一致 |
| Dev/Heldout 隔离 | 查询集合、request_hash 交集 | 共享查询 0、共享哈希 0；模型参数逐字段一致（temperature=null、thinking=low、max_tokens=128）；价值模型训练组 = 恰好 6 个 dev 组，与 heldout 0 交集 |

**结论：实验数据真实、契约完备，未发现任何缺失、重复、错位、解析失败或非法 Verdict。**

### 1.2 温度校准：null 与 0.0 无可测量差异（且方向上 0.0 更差）

独立复算（与原校准报告一致）：

| 线 | pairs | ever-flip | 平均 pairwise 分歧 |
|---|---:|---:|---:|
| A（temperature 省略） | 32 | 3 (9.4%) | 4.37% |
| B（temperature=0.0 显式发送） | 32 | 6 (18.8%) | 8.13% |

- Fisher 精确检验（双侧）：p = 0.474 —— **无可测量差异**。
- 跨线对齐比较：160 个对齐重复中 8 个不同（5.0%）；多数票 verdict 仅 1/32 对不同。
- **方向上 B ≥ A：显式 temperature=0.0 并未降低非确定性（若有任何趋势反而更高）。** 正式运行选择 temperature=null 与服务端默认解耦，是正确决策；"服务端默认温度=0" 的假设不成立，也不需要成立。
- 校准线查询（PLAIN-101/11/142/2429）全部属于 dev 侧（与 formal dev 同查询集），未污染 heldout。
- 注意：两线 32 对中 30 对相同查询文本/文档，但 request_hash 因 condition 不同而不同（`gate3-calib|A_null` vs `gate3-calib|B_zero`）——隔离正确。

### 1.3 核心统计独立复算 — 全部精确复现

**非确定性（H0 现象）：**

| 指标 | 独立复算 | 报告值 | 一致 |
|---|---:|---:|---|
| Dev ever-flip | 41/168 = 24.405% | 41/168 = 24.4% | ✓ |
| Dev pairwise disagreement | 8.71% | 8.7% | ✓ |
| Heldout ever-flip | 56/336 = 16.667% | 56/336 = 16.7% | ✓ |
| Heldout pairwise disagreement | 6.00% | 6.0% | ✓ |

（pairwise disagreement 口径：对每对取 C(12,2)=66 个重复对的布尔不等率，再对全部 pair 平均；pooled 与 macro-over-queries 在本数据上等价，因为每查询恰 28 对。）

**主比较（ReviewValue vs Random，D_EM = 4 次执行两两 set 不等率的均值，C(4,2)=6 对）：**

| 集合 | 预算 | gain（Random−RV） | baseline D_EM（Random） | bootstrap 95% | positive groups | RV 调用 | Random 调用 | 复现 |
|---|---|---:|---:|---|---:|---:|---:|---|
| Dev | 0.2 | 0.000 | 0.3056 | [0,0] | 0/6 | 10.417 | 12.417 | ✓ 精确 |
| Dev | 0.4 | 0.0833 | 0.3889 | [0,0.25] | 1/6 | 12.500 | 14.417 | ✓ 精确 |
| Heldout | 0.2 | 0.000 | 0.1528 | [0,0] | 0/12 | 10.708 | 12.708 | ✓ 精确 |
| Heldout | 0.4 | 0.000 | 0.1528 | [0,0] | 0/12 | 12.750 | 14.708 | ✓ 精确 |

Recall@3 变化 0（因无任何 Top-K 变化，见 §2）；Verdict `exploratory_or_gate_not_met` 复核成立（dev +0.083 在预注册门下也失败：rel=21.4%<25%、positive groups 1<2、区间含 0）。

**执行级核验：** 我对官方 `runs.jsonl` 中 Base/Random/ReviewValue 全部 432 次执行逐一比对最终 Top-K 与 `logical_calls`：**0 处不一致**。SimpleTopology/TraceSensitivity/Vote3 的行未参与主比较（comparator 已冻结为 Random），其汇总 D_EM 上下文见 §2.6。

### 1.4 离线 Replay 忠实性

独立重实现的语义链（与 `certirank` 代码逐行为对齐后从协议重写）：初始 verdict 取槽组 slot 0；探针读 slot 1、2，majority-of-3 更新；`cached_replay` 模式下翻转后全量重跑锦标赛，已决策对走缓存（cache_hit 不计费），新比较从预算计费；预算 = floor(base_calls × fraction)；预算内不足以完成探针（<2）即停；修复中预算耗尽则回退最后完整输出（aborted 保留在分母）。该语义复现了官方全部执行，包括 1 处细节：dev PLAIN-2539 Random 某执行探针翻转后重跑产生了 1 次新比较计费，导致只完成 1 次探针（预算 4 用 3）——官方 runs 同样如此。**Replay 忠实。**

- "不同策略意外产生相同结果"检查：Random 与 ReviewValue 在 heldout 的 48 个执行 Top-K 全部相同，但这是"复核无效果"（两策略选边不同而输出不变），不是种子/缓存碰撞——dev PLAIN-2479 上两者输出不同即为反例证明。共享响应池是配对设计的既定部分。
- 过程性发现（低风险，不改变裁决）：文件 mtime 显示 heldout 响应采集完成（20:00:19）早于 dev 冻结记录创建（20:00:48）约 29 秒。即 heldout 响应先于冻结存在于磁盘。代码层面的密封（heldout 评估必须先验证 freeze+model）在最终执行序中成立；价值模型与 comparator 选择可证明仅由 dev 数据决定（训练组/源 ID + 哈希核验）。该顺序弱点属成本/流程风险（若 dev 裁决为 stop，heldout 费用已发生），不构成结论效度风险，且最终裁决为阴性，无阳性主张被污染。
- 工具性发现（不影响本报告数字）：官方 `research_diagnostics.budgeted_oracle` 使用 incremental 修复模式，与主比较的 `cached_replay` 语义不同；但 Gate 3 正式结果未包含任何 Oracle 数字，故未影响报告裁决。本审计的 Oracle（§3）统一采用 `cached_replay` 语义。

---

## 2. Audit B：ReviewValue 失败机制（沿真实执行链路）

### 2.1 机制链总览（每层均有独立复算证据）

```
Initial Judgment → Edge Selection → Verification → Verdict Update → Tournament → Final Top-K
     [层1]              [层2]           [层3]           [层4]          [层5]
  地板效应：        价值模型无信号，   探针翻转率      4 次翻转中 3 次  与 Base 完全
  heldout 9/12      唯一正桶由 1      2.75%（需要    被锦标赛吸收；   相同（heldout）
  组 D_EM=0         个噪声样本驱动    两个探针同反）  1 次改变者使 D_EM 恶化
```

### 2.2 逐层证据

**层 1 — 改善空间稀缺（证据强度：强，确定性复算）。**
Random 的原始 D_EM = Base D_EM（heldout 12/12 组、dev 5/6 组逐组相等；唯一不等是 dev PLAIN-2479 Random 自伤 +0.5）。heldout 仅 3/12 组（PLAIN-165、PLAIN-2、PLAIN-2640）Base D_EM>0（分别为 0.667/0.667/0.500）；dev 仅 2/6 组（PLAIN-142=1.0、PLAIN-2539=0.833）。**9/12 的 heldout 组对任何策略都是零空间——D_EM 不可能低于 0。**

**层 2 — 价值模型无信号 → 过早停止（证据强度：强）。**
冻结价值模型（dev 拟合，250 样本，5 个特征桶）的样本构成：

| 特征桶 `[n,k,status,depth]` | n | mean gain | 正增益样本 | decision_changed |
|---|---:|---:|---:|---:|
| `[8,3,"conditional_impact",0]` | 24 | −0.0417 | 0 | 2 |
| `[8,3,"conditional_safe",2]` | 14 | 0.0000 | 0 | 0 |
| `[8,3,"unresolved",0]` | 48 | +0.0069 | **1（gain=+1/3, cost=4）** | 2 |
| `[8,3,"unresolved",1]` | 82 | −0.0081 | **1（gain=+1/3, cost=3）** | 5 |
| `[8,3,"unresolved",2]` | 82 | 0.0000 | 0 | 1 |

- 248/250 样本增益 ≤ 0。唯二正样本（均 +1/3）是噪声水平的孤立点。
- 评分公式要求 `expected_gain>0` 且样本 `cost ≤ remaining`：**0.2 预算（2 调用）下两个正样本均不可行（cost 3/4>2）→ 全部桶期望收益 0 → 24/24（dev）与 48/48（heldout）执行 0 次探针，ReviewValue ≡ Base，0 额外调用。**
- 0.4 预算（4 调用）下仅 `"unresolved",0/1` 两桶可行（score≈0.002–0.003）→ 每执行恰好 1 次探针（选 root 深度的 unresolved 对），之后 remaining=2 又不可行 → 停止。heldout 48 个执行的选择 100% 落在 `[8,3,"unresolved",0]` 桶。
- **预算使用：Random 用满（0.4→2 探针=4 调用；0.2→1 探针=2 调用；1 个 dev 执行因修复计费剩 1 调用）；ReviewValue 0.2 用 0/2、0.4 用 2/4。** "较早停止"属实，但根因是特征无信号而非保守策略。

**层 3 — 探针机制本身无翻转力（证据强度：强）。**
探针翻转要求 slot1 与 slot2 同时反对 slot0。全体 (pair, 槽组) 上的概率：

| 集合 | P(probe1≠init) | P(probe2≠init) | **P(探针翻转多数)** | P(翻转\|probe1≠init) |
|---|---:|---:|---:|---:|
| Dev | 7.59% | 9.23% | **3.87%** | 51.0% |
| Heldout | 5.65% | 6.47% | **2.75%** | 48.7% |

（probe 分歧高度相关：给定 probe1 反对，probe2 也反对的概率≈50%——"争议对"是持续争议的，非独立伯努利。）

**层 4 — 策略几乎打不中 actionable 对（证据强度：强，含一次不幸运抽样）。**
actionable（探针可翻转）且可见（在基线锦标赛轨迹内、可被选择）的 (pair, 槽组) 实例：dev 10/26 可见、heldout 16/37 可见（约每执行 0.2–0.3 个）。策略实际命中：

| 集合 | Random 探针 | 命中 actionable | 均匀期望 | ReviewValue 探针 | 命中 |
|---|---:|---:|---:|---:|---:|
| Dev | 47 | 2 | 1.93 | 24 | 1 |
| Heldout | 96 | **0** | 2.98 | 48 | 1 |

heldout Random 96 探针 0 命中：在"均匀选 2/可见对"零假设下 P(全漏)=3.7%——不幸运但合理的确定性抽样（种子不含 query id）。即便命中，效果也见层 5。

**层 5 — 锦标赛吸收 + 已观测的 4 次翻转中 1 次反而恶化（证据强度：中，仅 4 次翻转观测）。**
两集合全部探针共 215 次（Random 143、RV 72），verdict 翻转共 4 次：dev PLAIN-2479 Random（→Top-K 变，D_EM 0→0.5，**恶化**）、dev PLAIN-2539 Random 与 RV 各 1 次（Top-K 不变，吸收）、heldout PLAIN-2640 RV 1 次（吸收）。**没有一次翻转降低 D_EM。** dev @0.4 的 +0.083 gain 完全来自 PLAIN-2479 上 Random 的自我伤害（RV 因少动作而幸免），不是 RV 的改善——该解释在 heldout 上消失（Random 抽样未命中）。

**"不同 Top-K 但相同 D_EM"检查：** heldout 48 个执行 Random 与 RV 的 Top-K 逐执行完全相同（无此现象）；dev PLAIN-2479 存在不同 Top-K → 不同 D_EM。**"两策略不同输出但聚合相同"未在数据中构成混淆源。**

### 2.3 选边重叠与跨查询依赖

- Random 与 RV 的池化选边 Jaccard：heldout 0–0.40/组（12 组中 4 组为 0，8 组有 1–4 个重叠选择）。
- Random 的种子方案（seed=17+槽组序号+step，不含 query_id）导致跨查询部分依赖：heldout 0.2 预算下 9.1% 的查询对拥有完全相同的选边集合，0.4 下 1.5%。**轻度伪重复**，影响 bootstrap 的独立性假设（§4），不改变点估计。

### 2.4 各方法 D_EM 上下文（含未参与主比较的方法）

| 方法 | Dev D_EM | Heldout D_EM |
|---|---:|---:|
| Base | 0.3056 | 0.1528 |
| Random @0.4 | 0.3889 | 0.1528 |
| SimpleTopology @0.4 | 0.3889 | 0.1528（@0.2 为 0.1389） |
| TraceSensitivity @0.4 | 0.3889 | 0.2083（更差） |
| Vote3（全量验证，3×调用） | 0.3889 | **0.1667（差于 Base）** |
| ReviewValue @0.4 | 0.3056 | 0.1528 |

**Vote3 劣于 Base 是关键旁证：对该工作负载，即使对每个比较都做三票多数，跨执行分歧也不降反升**（不同槽组的多数票本身不一致）。选择性验证的上界被全量验证的实测结果直接压制。

### 2.5 按 Query-group 诊断表（Heldout，@0.4；`act(vis)/act` = 可见 actionable / 全部 actionable，`R/RV` = 探针数/翻转数）

| Query-group | Base D_EM | Rand D_EM | RV D_EM | gain | act(vis)/act | R 探针/翻转 | RV 探针/翻转 | 空间与诊断 |
|---|---:|---:|---:|---:|---|---|---|---|
| PLAIN-112 | 0 | 0 | 0 | 0 | 1/3 | 8/0 | 4/0 | 无空间（4 执行一致） |
| PLAIN-165 | 0.667 | 0.667 | 0.667 | 0 | 2/3 | 8/0 | 4/0 | 有空间；唯一 slot0 差异对 3 个；oracle 仅可修 0.167 |
| PLAIN-175 | 0 | 0 | 0 | 0 | 1/1 | 8/0 | 4/0 | 无空间 |
| PLAIN-2 | 0.667 | 0.667 | 0.667 | 0 | 2/5 | 8/0 | 4/0 | 有空间；slot0 仅差 (0,2) 一对；oracle 可完全修复（§3） |
| PLAIN-23 | 0 | 0 | 0 | 0 | 0/0 | 8/0 | 4/0 | 无空间，无 actionable |
| PLAIN-2450 | 0 | 0 | 0 | 0 | 1/1 | 8/0 | 4/0 | 无空间 |
| PLAIN-2490 | 0 | 0 | 0 | 0 | 3/10 | 8/0 | 4/0 | 无空间（actionable 对不影响输出） |
| PLAIN-2540 | 0 | 0 | 0 | 0 | 2/4 | 8/0 | 4/0 | 无空间 |
| PLAIN-2630 | 0 | 0 | 0 | 0 | 1/3 | 8/0 | 4/0 | 无空间 |
| PLAIN-2640 | 0.5 | 0.5 | 0.5 | 0 | 2/4 | 8/0 | 4/**1** | 有空间；RV 唯一翻转被吸收；oracle 也无法修复 |
| PLAIN-2650 | 0 | 0 | 0 | 0 | 0/0 | 8/0 | 4/0 | 无空间，无 actionable |
| PLAIN-2680 | 0 | 0 | 0 | 0 | 1/3 | 8/0 | 4/0 | 无空间 |

（Dev 对应表：PLAIN-101/11/2429 无空间；PLAIN-142 D_EM=1.0（8 个 slot0 差异对，oracle 仅修 0.167）；PLAIN-2479 Random 自伤 0→0.5；PLAIN-2539 D_EM=0.833，oracle 不可修复。完整逐组 JSON 见 `outputs/audit_b_mechanism.json`。）

---

## 3. Audit C：Oracle 上界与结构性分析（EXPLORATORY，不改变预注册门）

### 3.1 Oracle 设计

事后 Oracle：与在线策略**完全相同的验证操作与预算**（探针=2 调用、majority-of-3 更新、cached_replay 重跑计费、预算 floor(base×fraction)），但允许预知"哪些探针最有利于降低 Query-level D_EM"。实现：对每执行枚举全部合法探针子集（≤ floor(预算/2) 个、选自可见对），模拟真实预算语义得到可达输出集合；再跨 4 个执行联合最小化 D_EM（对可达输出去重后做笛卡尔积精确搜索）。仅使用已缓存响应，0 新调用。

**事后信息优势的边界（必须明确）：** 该 Oracle 同时看到全部 4 个槽组的响应与最终输出，并联合选择——这是在线单执行策略原理上不可观测的信息（协议的一步代理目标"与其他基线执行的一致性增量"正是这个不可观测量）。**额外 API 调用：0（纯缓存重放）；Oracle 报告的 extra_calls 为其选择的探针在预算内的计费。** 有限缓存样本的限制：Oracle 只能翻转缓存中"两个探针槽同反"的对（2.75%），不能创造不存在的翻转。

### 3.2 Oracle 结果

| 集合 | 预算 | Oracle D_EM（均值） | vs Base 增益 | vs Random 增益 | 有空间组内增益 |
|---|---|---:|---:|---:|---:|
| Dev | 0.2 | 0.3056 | 0.000 | 0.000 | 0.000 |
| Dev | 0.4 | 0.2778 | 0.028 | 0.111 | 0.083 |
| Heldout | 0.2 | 0.1389 | 0.014 | 0.014 | 0.056 |
| **Heldout** | **0.4** | **0.0833** | **0.069** | **0.069** | **0.278** |

- Heldout +40%：完美事后选边的天花板 = abs 0.069 / rel 45.4%（0.069/0.153）。**数值上恰好能过预注册点门（abs≥0.05、rel≥25%）——但这属于"上帝视角"，在线不可达。**
- 天花板高度集中：PLAIN-2 完全可修（0.667→0，其 4 个执行的 slot0 差异仅集中在 (0,2) 一对，oracle 以 8 调用探 (0,2)×2 执行即全收敛）；PLAIN-165 部分可修（0.667→0.5）；**PLAIN-2640 在任何合法探针选择下都不可修复**（0.5→0.5，其 actionable 对翻转后输出仍不收敛）；dev PLAIN-2539 同样不可修复。
- 即使把 Oracle 当作"机制潜力"的乐观估计：3 个有空间组中 1 个不可修、1 个仅修 1/4，仅 1 个可完全修复。**当前验证机制（盲选 + majority-of-3 探针）在该工作负载上的可交付收益不足以支撑进一步投入。**

### 3.3 结构性发现

- **关键对集中在排序边界：** 全部 417 个"翻转即改变 Top-3 集合"的 (pair, 槽组) 实例中，89%（370/417）跨越 top-3 边界（一端 win-rank≤2、另一端≥3），91% 含 top-3 内文档。边界成员资格（win counts/锦标赛结构）**部分可在线观测**——这是对"Query-aware Impact"假设唯一的有利结构信号。
- **不稳定与影响脱钩（核心负结果）：** 12 次重复中 ever-flip 的对，仅 14.8%（dev）/ 18.0%（heldout）是关键对。**即 ~85% 的翻转发生在翻转也无所谓的对上；会影响输出的对大多不翻转。** 任何以"翻转倾向"为信号的选边策略在该数据上先天低效。
- **可见性不是瓶颈：** 关键对 100% 落在基线轨迹可见集内（结构性必然：锦标赛未比较的对的翻转不改变遍历）。瓶颈在"actionable ∩ 关键 ∩ 跨执行对齐"三者交集极小（heldout 16 个可见 actionable 实例中，与关键对相交且能对齐输出的仅 PLAIN-2 的 (0,2) 等少数）。
- **固定 Trace 反事实的处理：** 在线特征的条件反事实分析（`conditional_effect`）在需要新比较时标记 `unresolved`（250 样本中 212 个落在此类桶）；而实际 cached_replay 执行**不会**遗漏新比较请求——翻转后重跑的新比较被计费（dev 有 1 例因此只完成 1 探针）。即：执行层忠实，特征层保守（信息损失），但鉴于特征整体无信号，这不是约束瓶颈。
- **数据充分性：** 对"探针机制无力"与"Oracle 天花板低"这两个结论，现有缓存数据已足够（确定性穷举）。对"更强验证算子（如顺序探针、争议检测）能否突破"，现有数据**不足以**检验（需要新的采集设计）。

### 3.4 禁止事项遵守

本审计未利用 heldout 事后标签构造新策略、未在相同 heldout 上宣称任何新策略正式有效。Oracle 结果仅作为机制潜力上界的探索性证据。

---

## 4. Audit D：统计可靠性

1. **Bootstrap 单位正确。** 组级（query-group）重采样、组内变体先平均、2000 次、百分位法——我独立重实现并精确复现了 [0,0] 与 [0,0.25]。每个 query-group 恰含 1 个查询（NFCorpus 选样），组=查询，无嵌套问题。
2. **[0,0] 的含义仅是"全部 12 个观测差值恰好为 0"。** 逐组核验：heldout 12/12 组 Random D_EM = RV D_EM = Base D_EM。这是**地板效应**（9/12 组在 0）叠加"复核无效果"的确定结果，不是"效应为零的高置信区间"——12 个组中 9 个对该比较设计完全无分辨力。
3. **Dev/Heldout 差异可由工作负载难度解释。** ever-flip 24.4% vs 16.7%（Fisher p=0.042，边际显著）；Base D_EM 0.306 vs 0.153。两 split 来自 NFCorpus 不同的 qrels 分区（dev.tsv vs test.tsv）、不同查询，选样规则相同（预注册 BM25 带 + ≥2 标注 + ≥1 完美级）。Dev 更"易翻" → Dev 上出现了噪声性的 +0.083（且由 Random 自伤驱动）而 heldout 未复现，与"工作负载难度 + 基线地板差异"一致。**未见任何泄漏或参数调优解释。**
4. **口径正确。** ever-flip（pair 属性）与 run-pair disagreement（重复对属性，共享 run、仅描述性）未混用；D_EM 为 set 不等率（协议主输出 unordered_topk_set）。质量口径 Recall@3 独立 qrels。
5. **质量分辨率足够但未被动用。** heldout Recall@3 跨 0.43–1.0（非饱和；多相关文档查询提供细于 1/3 的步长）。Δ=0 的原因是 Top-K 从未改变，不是指标钝化。
6. **伪重复（轻度，已量化）。** Random 种子不含 query_id：heldout 0.2 下 9.1% 查询对选边集合完全相同（0.4 下 1.5%）；同方法 4 次执行共享同一响应表（配对设计的定义）。影响：bootstrap 的"组独立"假设被轻度削弱，主要影响 Random 侧方差估计；点估计不受影响。run-pair 统计共享 run——代码已声明仅描述性。
7. **无事后指标筛选与多重比较问题。** 主预算 0.4 预注册、门限未动；dev 结果未过门（rel 21.4%<25%、1/6 组）被如实报告；heldout 阴性如实报告。comparator（Random）由 dev 规则在 heldout 前冻结。本审计的全部新分析（Oracle、结构、Fisher）均标注 Exploratory，未触碰预注册成功门。
8. **D_EM 分辨率：** 单组取值量化于 {0,1/6,…,1}；12 组均值的分辨率 ≈1/72≈0.014。门限 abs 0.05 在分辨率之上，设计自洽。

---

## 5. 独立复算方法学（可复现性记录）

- 引擎：`audit_gate3_independent/scripts/audit_common.py` —— 独立实现固定叶锦标赛（叶子按行序、胜者移除、仅祖先更新）、三槽复核账本（初始 slot0、探针 slot1/2、majority-of-3、cached_replay 重跑计费、预算 floor(base×fraction)）、Random 策略（种子 17+槽组+step，与冻结配置一致）、ReviewValue 评分（从冻结 `value_model.json` 数据按源码语义重实现：`expected_gain=sum(gains)/(n+2)`、可行=complete∧cost≤remaining、score=gain/max(2,cost) 且需 gain>0、Σquality≥0）。
- 非确定性/校准：`audit_a_stats.py`（含独立 Fisher 精确检验实现）。
- 契约：`audit_a_contract.py`（request_hash 独立重建、独立解析器、唯一性/时间戳/隔离）。
- 机制/Oracle/深挖：`audit_b_mechanism.py`、`audit_c_oracle.py`、`audit_cd_deepdive.py`。
- 全部输出：`audit_gate3_independent/outputs/`（audit_a_contract.json、audit_a_nondeterminism.json、audit_a_main_recompute.json、audit_b_mechanism.json、audit_c_oracle.json、audit_cd_deepdive.json）。
- 复现命令：`python audit_gate3_independent/scripts/audit_a_contract.py && python audit_gate3_independent/scripts/audit_a_stats.py && python audit_gate3_independent/scripts/audit_a_exec.py && python audit_gate3_independent/scripts/audit_b_mechanism.py && python audit_gate3_independent/scripts/audit_c_oracle.py && python audit_gate3_independent/scripts/audit_cd_deepdive.py`（纯离线，无网络）。
- 局限声明：本审计的 Random 复算复用其文档化种子方案（目的是核验而非重设计）；官方 `budgeted_oracle` 的 incremental 语义差异未在正式结果中使用（§1.4）；Fisher 检验为探索性；未重跑历史/合成数据的离线分析。

---

## 6. 必须交付的研究结论

### A. 实验是否正确？

**正确。** 数据契约 0 问题、冻结链完整、Dev/Heldout 隔离成立、全部核心统计（含 432 次执行级输出与调用数）被独立引擎精确复现、温度校准结论（null vs 0.0 无差异）成立。发现的两处过程性/工具性弱点——(i) heldout 响应先于冻结记录采集（mtime 证据，密封仅覆盖评估阶段；模型与 comparator 可证明仅由 dev 决定）；(ii) 官方 Oracle 工具的 incremental 语义与主比较 cached_replay 不一致（未用于正式结果）——**均不改变 Gate 3 Verdict**。`exploratory_or_gate_not_met` 在确认性与统计意义上均成立。

### B. ReviewValue 失败的主要机制（按证据强度排序）

| # | 机制 | 证据强度 | 关键证据 |
|---|---|---|---|
| 1 | **探针算子无翻转力**（majority-of-3 需两探针同反；率 2.75–3.87%） | 强（确定性、全量复算） | §2.2 层 3；Vote3 全量验证仍劣于 Base |
| 2 | **改善空间稀缺/地板效应**（heldout 9/12 组 Base D_EM=0） | 强 | §2.2 层 1 |
| 3 | **价值模型无信号 → 过早停止/预算利用不足**（250 样本仅 2 正增益；0.2→0 探针、0.4→1 探针） | 强 | §2.2 层 2；调用数 10.71/12.75 vs 预算 12.71/14.71 |
| 4 | **锦标赛吸收翻转**（4 次翻转 3 次无输出效应；唯一输出效应使 D_EM 恶化） | 中（观测仅 4 次） | §2.2 层 5 |
| 5 | **D_EM 目标依赖跨执行不可观测量**（一步代理目标本身不可在线观测） | 强（结构性） | §3.1；Oracle 需 4 槽组联合信息 |
| 6 | 次要：Random 在 heldout 未命中 actionable（P=3.7% 的不幸运确定性抽样）使其自身免于 dev 式自伤 | 中 | §2.2 层 4 |

### C. 是否存在明显的 Oracle Headroom？

**不存在有意义的 headroom。** +40% 预算下事后完美选边的上限为 abs 0.069 / rel 45.4%（heldout 均值），且：(i) 集中于 2/12 组（1 组完全可修、1 组修 1/4）；(ii) 3 个有空间组中 1 个（PLAIN-2640）在任何合法选择下不可修复；(iii) 依赖在线原理上不可获得的跨执行信息；(iv) 全量验证（Vote3）实测劣于 Base，从另一侧压制了任何选择性策略的空间。**当前验证机制（盲选边 + 三票多数探针 + D_EM 目标）应停止投入。**

### D. 现有数据是否支持新研究假设？

- **Query-aware Impact：不支持正式立项。** 唯一有利信号是关键对 89% 集中于 top-3 边界（部分可在线观测），但 (a) 不稳定对中仅 ~18% 是关键对（信号与目标脱钩）；(b) 影响的最终仲裁者是跨执行一致性，不可在线观测；(c) Oracle 天花板 0.069 限死任何此类策略的收益上限。可作为**待验证假设**记录，现有数据不足以检验。
- **Dynamic Provenance：无数据支持。** 实验未变动 provenance 维度，无任何相关观测。
- **Structural Certification：证据偏负面。** Vote3 劣于 Base、85% 翻转被结构吸收、局部探针在 2.75% 翻转率下无认证力。"用少量局部验证为 Top-K 稳定性发证书"的机制在当前工作负载上不成立。
- **应保留的现象级结论（确认性）：** temperature=null 下 GLM-5.3-Flash 相同请求存在可测量的运行间分歧（ever-flip 16.7%、pairwise 6.0%，heldout 336 对 12 重复）；显式 temperature=0.0 不消除该现象（校准 p=0.47）。

### E. 最终裁决与 Go/No-Go

**裁决：停止（Stop，原报告选项 A）。** 保留 H0 现象测量与"翻转/影响脱钩"的结构性发现（探索级），不再对当前 ReviewValue/选择性验证机制投入任何新的付费调用。不建议为机制假设启动新的小规模实验：缓存数据已穷举回答了机制潜力问题（Oracle 上界），新实验无法在不改变验证算子或目标定义的情况下产生增量信息。

若未来重启该方向（需新的预注册协议），Go 条件为同时满足：
1. **验证算子改变**：探针能以显著更高概率产生翻转（例如顺序探针/争议检测/不同采样参数使单对翻转率 ≫3%），否则任何选边策略的期望效应被算子上限压死；
2. **可观测的影响信号**：存在与"跨执行一致性问题对"相关的在线可观测量（边界成员资格是当前唯一候选，需在新数据上先验证相关性）；
3. **目标重定义**：主目标改为在线可观测的量（如对多数票引用的稳定性、或对独立标签的质量），D_EM 型跨执行目标需要至少一个可观测代理；任何此类改动属协议变更，需明确批准与重新预注册；
4. **工作负载重新选样**：保证有空间组占比（Base D_EM>0）足够（当前 25%）——否则地板效应吞没一切。

当前四条全部不满足 → No-Go。

---

## 附：审计产物清单

| 文件 | 内容 |
|---|---|
| `audit_gate3_independent/scripts/audit_common.py` | 独立引擎（锦标赛/账本/策略/价值模型/解析器/统计） |
| `audit_gate3_independent/scripts/audit_a_contract.py` | 契约核验（hash 重建、解析、唯一性、隔离） |
| `audit_gate3_independent/scripts/audit_a_stats.py` | 非确定性 + 温度校准复算（含 Fisher） |
| `audit_gate3_independent/scripts/audit_a_exec.py` | 主比较独立复算 vs 官方 summary |
| `audit_gate3_independent/scripts/audit_b_mechanism.py` | 失败机制逐组诊断 |
| `audit_gate3_independent/scripts/audit_c_oracle.py` | Oracle 上界 + 结构分析 |
| `audit_gate3_independent/scripts/audit_cd_deepdive.py` | 空间组解剖、边界统计、依赖量化 |
| `audit_gate3_independent/outputs/*.json` | 全部数值证据 |
| 本文件 | 审计报告 |
