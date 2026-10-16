# STRUCTURAL_ANALYSIS_REPORT — Offline Structural Propagation & Counterfactual Analysis for Semantic Top-K

- 研究类型：**Exploratory Mechanism Study**（Gate B1/B2/B3），基于已完成的 Gate 3 正式实验缓存
- 日期：2026-10-16
- 数据身份：BEIR NFCorpus · GLM-5.3-Flash @ Paratera（temperature=null · thinking=low · max_tokens=128）· N=8 · K=3 · 每无序对 12 次重复 · 协议 `0.3.0-research-pilot` · git `6ee9919`
- 约束遵守：**零新增 LLM 调用**；未修改原始数据、冻结协议与既有代码；未执行任何 Git 写操作；全部新产物位于 `experiments/gate3_structural_analysis/`
- **最终裁决：D — Stop（无可投入的独立算法价值机制）**。三條利用路径（算子稳定性选择、动态分支算法、有界扰动认证）全部被本数据上的定量证据关闭。详见 §10–§11。

---

## 1. 执行摘要（TL;DR）

在 Gate 3 的 18 个 Query-group × 12 个 Frozen Judgment World（= 216 次基础执行/算子）上，用经官方运行记录 100% 复现验证的独立 Sandbox 对比了 4 种物理 Top-K 计划（Tournament / MinHeap / Quickselect / FullCompare），执行了 13,472 次单边翻转与 124,151 次双边翻转的反事实分析（各 × 2 种方法 = 275,246 次重放）以及全部 b∈{1,2}（empirical + worst-case）的精确认证搜索。三个结论：

1. **B1（计划稳定性）：无差异。** 四算子的跨 World 不稳定性（D_EM12）差异为 0.23–0.29，所有配对差的 bootstrap CI 均跨 0（符号检验 p ≥ 0.219），与输入顺序置换效应同量级；不稳定性由 Query 级判断噪声主导（Spearman 0.49–0.70），不稳定 Query 集合在四算子间几乎完全重合。没有任何"选对物理计划就能吸收噪声"的证据。
2. **B2（动态分支）：现象真实且普遍，但无算法缺口。** 单边翻转已执行判断后，生产 Tournament 算子 85.6% 的反事实落入原始轨迹之外（平均需 2.27 个新比较，最多 5 个）——固定 Trace 反事实分析在相同比例上返回 UNRESOLVED，结构性不完整。但完整分支感知重放在 N=8 下成本 ≤ 28 次比较（平凡），且仓库已实现的增量 Tournament 更新正是处理该问题的标准技术。双边联合翻转对自适应计划近似完美线性叠加（joint-emergent 仅 4/11,030）。
3. **B3（结构认证）：不可认证。** b=1 与 b=2、empirical 与 worst-case 两种语义下，三种自适应算子的认证覆盖率均为 **0/216**。对 Tournament 这是可证明的结构性不可能（本文给出并验证了一个驱逐引理 + 叶对鸽笼论证）；唯一可认证的是 28 次全比较计划在 b=1 下的 10/216（4.6%），b=2 归零。精确搜索本身平凡（violation 立即被发现；总耗时秒级），保守剪枝无发挥空间。

**统一解释**：观测噪声很少改变输出（10/18 个 Query 在全部 12 个 World 下完全稳定）不是因为计划稳健，而是因为**噪声与关键性近乎独立**（新分支读取的 Pair 仅 13–19% 落在 ever-flip 对上；前序审计同样发现 ever-flip 对中仅 15–18% 关键）。一旦允许假设性单边扰动，每次执行都有改变输出的关键边——这就是观测稳定性与认证不可能并存的机制。

---

## 2. 数据与实验身份

| 项 | 值 |
|---|---|
| Query-group | 18（dev 6：PLAIN-101/11/142/2429/2479/2539；heldout 12：PLAIN-112/165/175/2/23/2450/2490/2540/2630/2640/2650/2680） |
| 判断张量 | Y[q][(i,j)][r]，28 对 × 12 槽位 × 18 组 = 6,048 个判断（源自 6,368 条缓存调用中的 6,048 条 formal 记录） |
| Frozen World | 槽位 r 的 28 个判断构成一个世界（S2 合成耦合假设，与官方执行语义一致）|
| 基础执行 | 每算子 216 次（18 组 × 12 World）；官方初始槽 {0,3,6,9} 子集用于与官方 D_EM 对照 |
| 反事实规模 | 单边翻转 13,472 次、双边翻转 124,151 次（全枚举，无近似、无抽样），各以 2 种方法分析（共 275,246 次重放） |

完整性与语义审计见 `SEMANTICS_AND_ASSUMPTIONS.md`（S1–S12）与 `results/sandbox_validation.json`。

## 3. Sandbox 正确性锚点

1. **官方执行复现**：Sandbox 的 Tournament 重实现精确复现 `runs.jsonl` 中全部 144 行 Base 执行记录（72 个唯一执行）的 `topk`、`logical_calls`、`cache_hits` —— **0 不匹配**。Tournament 在官方 4 初始槽上的 D_EM 与官方 Base D_EM 逐 Split 精确相等（dev 0.3056 / heldout 0.1528 / pooled 0.2037）。
2. **传递性全序 Oracle**：4 算子 × 7 种 (n,k) 配置 × 随机全序，2,016 项检查全部返回一致且正确的 Top-K（0 失败）。开发过程中该检查捕获并修复了 MinHeap sift-up 的方向错误——Sanity Check 机制有效性的直接证据。
3. **测试套件**：27/27 通过，覆盖任务要求的全部 8 类（全序正确性、缓存计数、单边翻转触发新比较、双边非加性、非传递终止性、精确枚举 vs 认证一致性（b=0/1/2 与暴力全世界枚举对齐）、预算耗尽返回 UNKNOWN、固定 World 可复现性）。
4. **结构性事实核验**：216 次真实执行中，Tournament 与 Quickselect 从不在单次执行内重复访问同一 Pair（cache_hits=0）；MinHeap 在 65% 的 World 中重复访问（140/216）。

## 4. 算子语义（摘要）

四算子共享：固定候选行序、Pair 规范化 (i<j)、True=较小行序文档胜、执行内决策缓存（首次读取固定，再访问为缓存命中）、确定性。非传递比较器下输出均为**计划定义**的结果，不解释为正确性差异：

- **Tournament**（生产算子，`fixed_leaf_tournament_v1`）：定叶、行序、胜者移除、仅祖先重算；输出有序（提取顺序）。平均 10.61 次逻辑调用。
- **MinHeap**：大小 K 最小堆（根=最弱保留者），新候选仅与根比。9.43 次调用。
- **Quickselect**：Lomuto 划分确定性选择，输出仅集合语义。14.34 次调用。
- **FullCompare + 胜场数排名**（参考计划）：全 28 对，胜场数降序稳定排序（平局按行序）。28 次调用。

## 5. Gate B1：物理算子噪声传播

**设计**：相同 12 个 Frozen World 下运行四算子；指标为跨 World D_EM（集合不等率，C(12,2)=66 对/Query）、同 World 跨算子分歧、成本-稳定性关系、输入顺序敏感性（4 个固定置换）、Call-level Resampling 对照（S9，分开报告）。

### 5.1 主结果（pooled 18 组；dev/heldout 分列见 `results/b1_summary.json`）

| 算子 | D_EM12 (pooled) | D_EM12 dev | D_EM12 heldout | D_EM4 官方槽 | 平均调用 | 不稳定 Query |
|---|---:|---:|---:|---:|---:|---:|
| Tournament | 0.2584 | 0.3434 | 0.2159 | 0.2037 | 10.61 | 8/18 |
| MinHeap | 0.2887 | 0.3157 | 0.2753 | 0.2593 | 9.43 | 9/18 |
| Quickselect | 0.2828 | 0.3283 | 0.2601 | 0.2963 | 14.34 | 9/18 |
| FullCompare | 0.2323 | 0.2955 | 0.2008 | 0.1944 | 28.00 | 8/18 |

### 5.2 配对比较（逐 Query D_EM12 差值，pooled）

| 算子对 | 均值差 | bootstrap 95% CI | 符号检验 p |
|---|---:|---|---:|
| tournament−minheap | −0.030 | [−0.116, +0.024] | 1.000 |
| tournament−quickselect | −0.024 | [−0.134, +0.076] | 0.727 |
| tournament−fullcompare | +0.026 | [−0.014, +0.078] | 0.625 |
| minheap−fullcompare | +0.056 | [−0.008, +0.144] | 0.219 |
| quickselect−fullcompare | +0.051 | [−0.049, +0.158] | 0.727 |

**全部 CI 跨 0，全部 p ≥ 0.219。**

### 5.3 关键诊断

- **成本不能解释，也不存在可利用的成本-稳定性交换**：调用最少的 MinHeap（9.43）名义上最不稳定，调用最多的 FullCompare（28）名义上最稳定但差异不显著且成本 2.6×；调用数相近的 Tournament（10.61）与 MinHeap（9.43）之间差异（−0.030）与噪声不可区分。
- **不稳定 Query 集合跨算子几乎重合**：核心不稳定 Query 为 PLAIN-142/2479/2539/165/2/23/2540/2640（8 个，四算子一致）；唯一分歧是 PLAIN-2490（Tournament D_EM=0 完全吸收 vs MinHeap 0.712 / Quickselect 0.788 放大；FullCompare=0）——1/18 的孤立算子特异案例，方向在其它 Query 上不复现。
- **不稳定性由 Query 模糊度驱动**：Spearman(Query 噪声率, D_EM12) = 0.49–0.70（四算子）；与非传递环数 Spearman = 0.66（Tournament）。10/18 个 Query 在所有算子下完全稳定。
- **输入顺序效应 ≈ 算子效应**：4 个固定置换下平均 D_EM12 的极差：Tournament 0.060、MinHeap 0.063、Quickselect 0.040、FullCompare 0.048 —— 与算子间差异（0.02–0.06）同量级。"哪个计划"不比"哪种比较结构"更重要。
- **同 World 跨计划分歧真实存在但集中于同一批模糊 Query**：平均同 Top-K 率 0.755–0.880；任何分歧出现在 4–8/18 个 Query；10/18 个 Query 全计划恒一致。非传递性使"同一判断下的唯一正确 Top-K"不存在（计划相对性），这是语义事实而非可利用机制。
- **Call-level Resampling（补充语义，S9）**：每调用独立采样槽位时的不稳定性与 Frozen World 结果几乎相同（如 Tournament：dev 0.336 vs 0.343；heldout 0.209 vs 0.216）——S2 合成耦合假设不改变不稳定性测量的量级与结论方向。

### 5.4 B1 判定：**No-Go**

未观察到可重复、有效应量、且不能由比较次数/输入顺序/Query 模糊度解释的计划间稳定性差异。唯一反例方向（PLAIN-2490 的 Tournament 吸收）为孤立事件（1/18），且反方向案例同样存在。

## 6. Gate B2：分支敏感反事实传播

**设计**：对每 (Query, World, 算子)：执行原计划保存轨迹 → 枚举每个已执行判断的单边翻转与每对已执行判断的双边翻转（全枚举）→ Method A（Fixed-trace，越界即 UNRESOLVED）与 Method B（Branch-aware Replay，从冻结扰动世界读取新比较）分别分析。

### 6.1 单边翻转（pooled）

| 算子 | 翻转数 | Branch Escape Rate | 平均新比较 | 最大新比较 | A UNRESOLVED | A 可解析时与 B 一致 | Top-K 集合敏感率 | 有序敏感率 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Tournament | 2,291 | **85.6%** | 2.27 | 5 | 85.6% | 100% | 54.6% | 79.9% |
| MinHeap | 2,036 | 90.3% | 3.82 | 10 | 90.3% | 100% | 56.1% | 85.1% |
| Quickselect | 3,097 | 44.5% | 2.72 | 15 | 44.5% | 100% | 51.0% | — |
| FullCompare | 6,048 | 0.0% | 0 | 0 | 0.0% | 100% | 18.1% | 35.1% |

- **固定 Trace 反事实对自适应计划结构性不完整**：UNRESOLVED 率 = branch escape 率（构造性等价）；可解析时与完整重放 100% 一致（正确性交叉检查通过）。
- **新读取的 Pair 本身落在噪声对上的比例**：Tournament 19.3%、MinHeap 13.9%、Quickselect 13.0% —— 与基础 ever-flip 率（dev 24.4%/heldout 16.7%）相当，分支逃逸不系统性地命中噪声对：**分支问题与噪声问题独立叠加，不互相放大**。

### 6.2 双边翻转（全枚举）

| 算子 | 对数 | 联合改变率 | Joint-emergent（双单边均无效果而联合有效果） | Cancellation（单边有效果而联合无效果） |
|---|---:|---:|---:|---:|
| Tournament | 11,030 | 70.2% | **4** (0.004%) | 10.8% |
| MinHeap | 8,823 | 65.7% | 1 (0.001%) | 15.7% |
| Quickselect | 22,650 | 72.1% | 7 (0.003%) | 5.1% |
| FullCompare | 81,648 | 30.2% | 3,310 (4.05%) | 5.6% |

自适应计划的联合效应近似完美叠加（emergent ≈ 0）；唯一存在系统性非加性的是胜场数聚合（门槛效应：两个各自被吸收的 ±1 票可联合跨越排名阈值）。Tournament 的 4 例 emergent 全部在 PLAIN-142（例：slot 4，翻转 {(0,1),(2,6)}，base Top-K {2,4,6}）。

### 6.3 关键边结构（Tournament；含一个可证明的二分法）

对 2,291 个单边翻转（216 执行 × 每执行约 11 个已执行判断）：

- **边界二分法**：改变 Top-K 集合的翻转 92.3% 跨越 base Top-3 边界（一端成员、一端非成员）；被吸收的翻转 **0.0%** 跨边界（0/1,040）。计数完全吻合：1,155 个边界比较全部改变集合。
- **驱逐引理（经验 + 证明）**：胜者移除锦标赛中，**每个被提取成员的全部失利只可能输给更早提取的成员**（证明：成员 m 若在节点 v 输给 x，则 nodes[v]=x，m 被困于 v 之下；m 重返要求 v 被重算，即某更早提取的胜者叶子位于 v 之下，故该胜者 = nodes[v]，矛盾）。因此凡边界比较（m∈Top-K, x∉）必为 m 获胜；翻转后 m 输给非成员 x，由同一不变量 m 无法再被提取（或 x 进入集合）——**集合必然改变**。该不变量在全部 216 次真实执行上经验核验：0 例成员输给非成员。
- **按执行阶段**：比较所在阶段越晚越关键——stage 2（第 3 名产生）86.7% 改变输出、stage 1 56.9%、stage 0（建树）46.6%。关键性向树根/提取路径集中。

### 6.4 B2 判定：**机制存在、无算法缺口（No-Go for 算法研究）**

动态分支普遍（85.6%+）且固定 Trace 明显不足——但完整分支感知重放在 N=8 下至多 28 次比较（毫秒级），仓库已有的增量 Tournament 更新（`tournament(previous=…, changed_pairs=…)`）就是处理该更新的标准自调整计算技术。双边效应近线性。**不存在需要新算法解决的困难。**

## 7. Gate B3：有界扰动结构认证

**保证语义**（S6）：Top-K **集合**相等；扰动集 = 已执行判断中 ≤b 个翻转；Empirical（新读用冻结 w0）与 Worst-case（新读任意值、全分支枚举）分开报告；节点预算 200k / 20s，超限 UNKNOWN（实测 0 例超限）。

### 7.1 认证覆盖率（每算子 216 次基础执行）

| 算子 | b=1 empirical | b=1 worst | b=2 empirical | b=2 worst |
|---|---|---|---|---|
| Tournament | 0/216 | 0/216 | 0/216 | 0/216 |
| MinHeap | 0/216 | 0/216 | 0/216 | 0/216 |
| Quickselect | 0/216 | 0/216 | 0/216 | 0/216 |
| FullCompare | **10/216 (4.6%)** | 10/216 | 0/216 | 0/216 |

### 7.2 结构性不可能（Tournament，可证明）

由驱逐引理 + **叶对鸽笼**：K=3 个成员分布于 4 个互斥相邻叶对 {0,1}{2,3}{4,5}{6,7}，3 为奇数 ⇒ 至少一个叶对混合 ⇒ 该叶对比较在**每次**建树中执行且跨界 ⇒ 其翻转必然驱逐成员 ⇒ **每次 Tournament 执行在 b=1 下都 violated**。此推理实现为 O(1) 结构捷径（P2），在全部 216 实例上与精确 Oracle 判定**逐一对账一致（0 soundness violation）**，empirical 与 worst-case 均如此。MinHeap/Quickselect 无此定理但实测同样 216/216 violated。

### 7.3 唯一可认证案例及其结构

10 个可认证执行全部属于 FullCompare b=1（其无未执行对，故两种模型重合）：PLAIN-2450（8/12 World）、PLAIN-112（2/12）、PLAIN-2680（1/12）、PLAIN-2539（1/12——最不稳定 Query 的高边际特定 World）。可认证 ⟺ 胜场数第 3–第 4 名差距 = 2 且无危险对；violated 执行的差距均值仅 0.81（范围 0–2）。**b=2 下全归零**（差距 2 可被两个翻转各 ∓1 击穿）。

### 7.4 搜索成本与剪枝

violation 在首个反例分支即被发现：单实例平均搜索单位 1.4–177（最大 1,108），四算子两模型 216 实例总耗时 ≤ 2 秒；可认证实例至多 378 次重放。**搜索空间不构成挑战，保守剪枝（P1 记忆化在 violated 实例上约 0 收益；P2 捷径虽正确但只证明不可能）无可优化的对象。**

### 7.5 观测稳定性 vs 假设认证（B3 的核心对照）

| 口径 | Tournament | MinHeap | Quickselect | FullCompare |
|---|---:|---:|---:|---:|
| 12 个观测 World 全稳定的 Query（观测噪声口径） | 10/18 | 9/18 | 9/18 | 10/18 |
| b=1 假设扰动可认证执行（认证口径） | 0/216 | 0/216 | 0/216 | 10/216 |

**同一批数据上，观测噪声几乎从不改变输出，而假设性单边扰动几乎总能改变输出。** 二者并存的原因即噪声-关键性解耦：观测到的翻转大多落在非关键对上（本研究的 13–19% 新读噪声率 + 前序审计的 15–18% 关键率互证）。

### 7.6 B3 判定：**No-Go**

不存在具有实用覆盖率的非平凡认证；对生产算子认证在 b≥1 下结构性不可能（可证明）；唯一部分可认证的计划（全比较）成本 2.6× 且 b=2 即失效；搜索平凡，剪枝无意义。

## 8. 机制统一图景

```
观测层：  12 World 间输出高度稳定（10/18 Query 全计划零变化）
              ↑ 为什么？—— 不是计划稳健
结构层：  每次执行都有关键边（Tournament 平均 5.3 个边界比较/执行；
          54.6% 的已执行判断单边翻转即改变集合；b=1 认证 0/216）
              ↑ 二者为何不矛盾？—— 噪声与关键性解耦
耦合层：  ever-flip 对 ∩ 关键对 ≈ 独立交集（新分支读中 13–19% 为噪声对；
          前序审计：ever-flip 对中仅 15–18% 关键）
计划层：  四算子的比较结构差异（含输入顺序置换）只产生与噪声同量级的效果
          （B1 全部 CI 跨 0）——计划选择既不放大也不消除上述任何一层
```

## 9. 与现有方法的关系（对应任务 Q4）

| 领域 | 本研究的发现 | 关系判定 |
|---|---|---|
| Tournament 证书 / 比较排序理论 | 驱逐引理与边界二分法是胜者移除锦标赛已知的"胜者性质"类结论的小实例；它证明的是**不可能**而非新算法 | 已有理论直接覆盖 |
| Probabilistic Top-K（不确定数据库） | 跨 World D_EM 测量是该问题设定的实例；未发现新机制（噪声-关键性解耦已由前序审计确立） | 已有方法直接覆盖 |
| Self-adjusting Computation / Dynamic Dependence Graphs | B2 的分支逃逸正是这些技术针对的更新传播问题；实测更新平凡（≤28 比较），且仓库已实现增量 Tournament 更新 | 已有方法直接覆盖 |
| Change Propagation / Full Replay | 在 N=8 下完整重放是毫秒级操作；增量更新是 O(log N) 级标准技术 | 已有方法直接覆盖 |
| Semantic Query Processing（LOTUS 等） | 同 World 下计划相对的 Top-K（15–25% 分歧）是非传递比较器下"计划定义输出"的已知定性事实；本文给了单工作负载的定量刻画 | 原则上已知，定量刻画为增量 |

**未发现"需要新算法问题"的证据。** 本研究的价值是把这些"原则上适用"变成了本工作负载上的定量测量与负结论。

## 10. 必答研究问题

**Q1. Physical Plan Stability** — 不同 Top-K 物理算子的稳定性是否有非平凡差异？能否独立于比较次数和输入顺序解释？
**没有。** 四算子 D_EM12 差异 0.23–0.29，全部配对 CI 跨 0（p≥0.219）；无单调成本-稳定性关系（调用最少的 MinHeap 名义最不稳定）；输入顺序置换效应（极差 0.04–0.06）与算子效应同量级，不可分离；不稳定 Query 集合跨算子重合且由 Query 噪声主导（ρ=0.49–0.70）。唯一算子特异案例（PLAIN-2490，Tournament 吸收 vs Heap 放大）为 1/18 孤立事件。

**Q2. Dynamic Control-flow** — 翻转后是否经常出现固定轨迹无法覆盖的新比较？是否值得专门设计动态依赖算法？
**经常（Tournament 85.6%，MinHeap 90.3%），但不值得。** 固定 Trace 反事实在相同比例上 UNRESOLVED——它对自适应计划结构性不完整。然而完整分支感知重放 ≤28 次比较即完成，增量 Tournament 更新是已有标准技术，双边效应近线性叠加（emergent 4/11,030）。机制存在、系统创新性不足。

**Q3. Structural Certification** — b=1/b=2 下是否存在足够多的可认证查询？能否利用算子结构安全剪枝？
**不存在。** 三种自适应算子 0/216（b=1、b=2、两种语义全部）；Tournament 为可证明的结构性不可能（驱逐引理 + 叶对鸽笼，捷径 216/216 与精确 Oracle 一致）；唯一可认证的 FullCompare b=1 为 10/216（4.6%，全部为胜场差距=2 的特定 World），b=2 归零。剪枝（P1/P2）在 Soundness 上成立但对本问题无优化空间（搜索本身平凡）。

**Q4. Novelty Risk** — 相对五类已有工作是否揭示明确新机制？
未揭示。驱逐引理是已知锦标赛性质的小实例且方向为不可能性；分支传播由 Self-adjusting Computation/DDG 直接覆盖；测量本身是 Probabilistic Top-K 与语义查询处理的已有问题设定下的实例量化。区分：全部属"已有方法直接覆盖"，未发现"需要工程适配的既有方法无法满足"或"潜在新算法问题"的证据；对更大 N / 不同噪声模型下的外推属"当前证据不足以判断"（见 §11.2）。

**Q5. Final Research Decision** — 见 §11。

## 11. 最终裁决

### 11.1 裁决：**D — Stop（No Nontrivial Mechanism）**

现有数据未发现足够明显、可利用且具有独立算法价值的结构机制。三条研究假设的失败情况：

| 假设 | 结果 | 关键证据 |
|---|---|---|
| H-B1：物理计划具有可利用的差异性噪声吸收 | **证伪** | 全部配对 CI 跨 0；成本/顺序/Query 模糊度吸收全部表观差异；FullCompare 的 2.6× 成本买不到显著稳定性 |
| H-B2：动态控制流产生值得解决的机制缺口 | **现象确认、价值证伪** | 逃逸率 85.6% 真实且固定 Trace 结构性不完整；但重放平凡 + 增量更新已有 + 双边近线性 ⇒ 无缺口 |
| H-B3：存在非平凡有界扰动认证 | **证伪** | 自适应算子 0/216（Tournament 可证明不可能）；FullCompare 4.6%@b=1、0@b=2；搜索平凡使剪枝无对象 |

### 11.2 停止投入的范围与重开条件

**停止范围**：在当前工作负载（NFCorpus / GLM-5.3-Flash / N=8 / K=3 / 该 18 组）上，停止对以下方向的一切投入：(a) 以稳定性为目标的物理计划选择或设计；(b) 分支敏感的反事实/依赖追踪算法研究；(c) 有界扰动下的 Top-K 确定性认证。这三条的否定性证据是确定性的（精确枚举），新分析脚本无法翻案。

**重开条件**（全部需要新数据与新冻结协议，均不可由当前缓存回答）：
1. 比较算子改变（如多数票、顺序探针、更高翻转率采样），使单边扰动率与噪声率同量级耦合——若噪声能系统命中关键边，认证/计划问题的答案可能改变；
2. N ≫ 8 且单次调用昂贵的场景，使完整重放与增量更新的成本差成为真实系统问题（属工程优化，非新算法问题）；
3. 工作负载重选使有空间组（基础不稳定）占比 ≫ 25%。

三者同时不满足即维持 Stop。**不建议为形成论文故事而在当前缓存上继续构建任何复杂系统。**

## 12. 局限

1. 18 个 Query-group、单模型、单数据集、N=8/K=3——效应量外部效度未知（但主要结论为负，负结论对"是否存在大效应"的回答在本地是确定的）。
2. Frozen World 为合成耦合（S2）；Call-level Resampling 对照显示该假设不改变结论量级。
3. b≤2 由预算决定；b≥3 外推不保证（但 b=1 已 0 覆盖，更大的 b 只会更弱）。
4. 有序 Top-K 认证严于集合认证，未单列（集合已 0，有序必 0）。
5. 全部发现为 Exploratory（18 组已被前序研究使用）；无任何确认性主张。

## 13. 产物清单与复现

```
experiments/gate3_structural_analysis/
├── STRUCTURAL_ANALYSIS_REPORT.md      # 本报告
├── SEMANTICS_AND_ASSUMPTIONS.md       # S1–S12 全部语义与假设
├── scripts/
│   ├── sandbox.py                     # 独立沙箱（零 certirank 导入）
│   ├── validate_sandbox.py            # 官方复现 + 全序 Oracle 验证
│   ├── run_b1.py / run_b2.py / run_b3.py
│   └── consolidate_results.py         # 汇总数字/CSV
├── tests/test_structural_sandbox.py   # 27 项测试（全部通过）
└── results/
    ├── sandbox_validation.json        # 复现 144/144、Oracle 2016/2016
    ├── b1_operator_comparison.json / b1_summary.json / b1_diagnostics.json
    ├── b2_counterfactual.json / b2_summary.json
    ├── b3_certification.json / b3_summary.json
    ├── summary_numbers.json / per_query_metrics.csv / b3_certified_executions.csv
```

复现（纯离线，无网络）：`python -m pytest experiments/gate3_structural_analysis/tests -q && python experiments/gate3_structural_analysis/scripts/validate_sandbox.py && python experiments/gate3_structural_analysis/scripts/run_b1.py && python experiments/gate3_structural_analysis/scripts/run_b2.py && python experiments/gate3_structural_analysis/scripts/run_b3.py && python experiments/gate3_structural_analysis/scripts/consolidate_results.py`（全流程 < 1 分钟 CPU）。
