# Gate 2C 审计报告（gate2c_audit.md）

**结论：PASS** — 22 项检查，0 项未通过。

生成命令：`python scripts/audit_gate2.py`（零 LLM 调用，仅标准库）。审计对象：`data/external/lotus_gate2/` 的不可变副本（manifest SHA-256 已逐文件校验）。

## 1. 数据来源与不可变性

- 来源：LOTUS 非确定性研究，commit `136ae4f4a344`，run `live_pilot_20261014T132233Z`，420 planned calls。
- 条件：P0 (retargeted endpoint, see report section A)（与原 P0 冻结条件的 endpoint 偏差已在报告中记录，结论只适用于该条件，不与 Gate 1 官方端点数据合并）。
- 密钥策略：No API keys, .env files, or non-public query content are stored here. provider_runtime.yaml contains environment-variable NAMES only.
- 上游 lineage（仅记录哈希，未拷贝内容）：gate1b_retrieval_candidates.jsonl、gate1b_pairset_manifest.json（见 manifest）。

- **[IMM-1] PASS** — 外部数据不可变性：manifest 内全部文件 SHA-256 与字节数一致。31 个文件校验完成
- **[IMM-2] PASS** — 数据来源（provenance）已记录：LOTUS repo commit、run_id、endpoint 偏差说明。commit 136ae4f4a344, run live_pilot_20261014T132233Z

## 2. 数据契约与 Pair Response Pool

契约字段与校验规则的规范文本见 `configs/protocol.yaml`；实现见 `src/certirank/pair_pool.py`。

- **[CT-1] PASS** — 数据契约 C1-C5,C8：420 条记录全部携带契约字段、请求哈希与冻结 workload 一致、输入顺序一致、判定为 strict-valid 布尔值。420/420 条 strict-valid；契约问题 0 个
- **[CT-2] PASS** — 数据契约 C6 完整性：3 query × 28 pair × 5 次响应 = 420，无缺失、无多余。records=420, expected=420
- **[CT-3] PASS** — 数据契约 C7 请求身份：每对 5 次响应的 request_hash 完全一致（同一请求，可池化复用）。84 个 pair slot，420 条响应全部通过同请求检查
- **[POOL-1] PASS** — Pair Response Pool 形态：84 个 (query, pair) slot × 5 响应；84 个唯一 request_hash。{"327": {"slots": 28, "responses": 140}, "51": {"slots": 28, "responses": 140}, "636": {"slots": 28, "responses": 140}}
- **[POOL-2] PASS** — 原始回答保留：每条响应含 raw_response_content（可重新归一化）。420/420 条原始回答非空

**Pool 可复用性结论：** 420 条响应构成 84 个 `(query, pair_position, request_hash)` slot、每 slot 恰 5 条响应，且每 slot 内 5 次响应共享同一 request_hash（即 5 次独立重复回答的是同一请求身份）。该表可直接作为离线 Tournament 的固定响应表使用：任取每 slot 的 3/5 子集即可组成一个独立三次投票的 run（C(5,3)=10 种组合/slot）；新分支请求（执行路径未覆盖的 pair）不在表内，必须实际调用并计入预算。

## 3. 独立复算结果 vs 报告数字

复算链路：原始 `strict_parsed_decision` → 判定向量（按 pair_position 排序）→ CertiRank 自实现的 naive sort 重放（稳定排序，平票按候选行序）→ 全部统计。**未复用生产方代码**。

- **[A-1] PASS** — 报告 §A：420 planned calls / 15 runs 全部在原始数据中出现。records=420, runs=15
- **[A-2] PASS** — 报告 §A：prompt tokens 327,530 / completion tokens 1,260 与原始数据逐行求和一致。prompt=327530, completion=1260
- **[A-3] PASS** — 报告 §A.2：唯一超长请求可定位（G2-51-r4, pair_position 25, ~20,137 s）。G2-51-r4 pos=25 latency=20137s
- **[D-0] PASS** — 报告 §D：Replay gate 15/15 —— 从原始判定独立重放 naive sort，Top-3 与 LOTUS 记录完全一致。topk 15/15, full ranking 15/15
- **[B-1] PASS** — 报告 §B：逐 query 翻转对数 8 / 9 / 12（pair 单位，各 28 对）。636: 8, 327: 9, 51: 12
- **[B-2] PASS** — 报告 §B：macro ever-flip 34.5%（29/84）；macro run-pair 平均分歧 16.7%（30 个依赖 run pair 的描述统计）。ever-flip 0.345238, disagreement 0.166667
- **[C-1] PASS** — 报告 §C：逐 query 的 distinct Top-3（有序/集合）、set/order agreement、Jaccard@3、τ-b、nDCG@3 全部复现。636: distinct 3/3, set 0.20, order 0.20, jaccard 0.60, tau 0.743, ndcg 1.0; 327: distinct 3/2, set 0.60, order 0.30, jaccard 0.80, tau 0.714, ndcg 1.0; 51: distinct 2/1, set 1.00, order 0.40, jaccard 1.00, tau 0.786, ndcg 1.0
- **[C-2] PASS** — 报告 §C：macro set agreement 60% / ordered 30% / Jaccard 0.80 / τ-b 0.748 / nDCG@3 1.0。set 0.60, order 0.30, jaccard 0.80, tau 0.747619, ndcg 1.0
- **[D-1] PASS** — 报告 §D.1：单点翻转四分类计数（base=r0）—— 636: 5/8/2/13，327: 8/5/2/13，51: 10/11/5/2。636: 5/8/2/13; 327: 8/5/2/13; 51: 10/11/5/2
- **[D-3] PASS** — 报告 §D.1/§D.3 表格（pair 单位，84 对）：29 翻转 / 37 高影响 / 14 交集 / 23 稳定但高影响 / 15 翻转但低影响。ever=29, material=37, both=14, stable-material=23, unstable-inert=15
- **[D-3b] PASS** — 报告 §D.3 表：逐 query 交集 6/6/2、翻转但低影响 2/3/10、稳定但高影响 9/9/5。overlap {'636': 6, '327': 6, '51': 2}, unstable-inert {'636': 2, '327': 3, '51': 10}, stable-material {'636': 9, '327': 9, '51': 5}
- **[D-3c] PASS** — 报告正文 '46 single-flip high-impact pairs' 核查：正文与其自身 §D.1/§D.3 表格及存储分析 JSON 矛盾（14+23=37；逐 query order+set = 15+15+7 = 37）；复算确认正确值为 37。复算=37，表格隐含=37，正文=46；替代口径均不等于 46（5-base 并集=60、交集=8、run-pair 影响和=56、run-pair 差异位置和=140）→ 判定正文笔误，沿用 37；正文派生的 '23 (50%)' 应读作 23/37≈62%
- **[D-2] PASS** — 报告 §D.2：多点传播（仅真实观测向量）—— joint-emergent 0/30，cancelling 6/30。joint_emergent=0, cancelling=6 (30 run pairs total)
- **[X-1] PASS** — 与生产方存储分析 (gate2_live_analysis.json) 的 7 项 macro 指标逐位一致。全部一致
- **[X-2] PASS** — 与生产方存储传播分析 (gate2_live_replay_analysis.json) 的单点四分类计数逐位一致（同 base=r0）。全部一致

### 3.1 复算汇总表

| 指标 | 636 | 327 | 51 | macro |
|---|---|---|---|---|
| 翻转对数（pair/28） | 8 | 9 | 12 | 0.3452 |
| run-pair 平均分歧 | 0.1357 | 0.1571 | 0.2071 | 0.1667 |
| distinct 有序 Top-3 / 集合 | 3/3 | 3/2 | 2/1 | — |
| set/order agreement | 0.20/0.20 | 0.60/0.30 | 1.00/0.40 | 0.60/0.30 |
| Jaccard@3 / τ-b | 0.60/0.7429 | 0.80/0.7143 | 1.00/0.7857 | 0.80/0.7476 |
| nDCG@3（逐 run 均值） | 1.0 | 1.0 | 1.0 | 1.0 |
| 单点翻转 none/below/order/set | 5/8/2/13 | 8/5/2/13 | 10/11/5/2 | — |

Pair 级交叉表（84 对）：ever-flipped **29**；单点高影响 **37**；翻转∧高影响 **14**；稳定∧高影响 **23**；翻转∧低影响 **15**。Replay gate：15/15 runs Top-3 复现，全排序 15/15 一致。

## 4. 统计单位核对（此前 29 / 46 / 14 / 23 的含义）

| 数字 | 统计单位 | 分母 | 来源 |
|---|---|---|---|
| 29 翻转对 | **pair**（84 = 3 query × 28 pair 中的对） | 84 | §B/§D.3，5 次重复中出现 ≥2 种不同 valid 判定 |
| 37 高影响对 | **pair**（单点反事实，base=r0；order+set） | 84 | §D.1 表格；正文写的 "46" 是笔误（见 §4.1） |
| 14 翻转∧高影响 | **pair** | 84 | §D.3 |
| 23 稳定∧高影响 | **pair** | 84 | §D.3 |
| 15 翻转∧低影响 | **pair** | 84 | §D.3 |
| 34.5% ever-flip 比率 | pair 比率（29/84 = 逐 query 比率的 macro） | 84 | §B |
| 16.7% run-pair 平均分歧 | **run pair**（每 query C(5,2)=10 个，共 30 个，**依赖样本**，仅描述统计） | 30 run pairs | §B |
| 60% / 30% set/order agreement | **run pair** 比率 | 30 run pairs | §C |
| 15/15 replay gate | **run**（每次完整重放） | 15 runs | §D |
| nDCG@3 = 1.0 | **run** 比率（15 个 run 全部） | 15 runs | §C |

**单位结论：** 29/37/14/23/15 全部是 **pair 级**数字（分母 84），不能当作 run 数、run-pair 数或query 数使用；16.7%、60%、30% 是 **run-pair 级**描述统计（30 个依赖样本）；15/15 与 nDCG@3 是 **run 级**。§D.3 的 14 (17%) / 15 (18%) / 23 (27%) 的百分比分母同样是 84 对。

### 4.1 关于正文 "46" 的核查结论（P1 要求的数字核查项）

Gate 2 报告正文（§D.3 bullet 与 §E）写有 "of the 46 single-flip high-impact pairs" 及由此派生的 "23 (50%)"。**该 46 与报告自身的 §D.1/§D.3 表格矛盾**：逐 query 高影响（order+set）= 15+15+7 = 37，且 14 + 23 = 37。生产方存储的 `gate2_live_replay_analysis.json` 同样给出 37（overlap 14 / stable-material 23 / unstable-inert 15）。为排除口径差异，审计穷举了替代定义：以 5 次 run 分别为 base 的并集=60、交集=8、run-pair 单点影响求和=56、run-pair 差异位置求和=140、受影响位置并集=12 —— 均不等于 46。

**审计裁定：** 正文 "46" 为笔误（或残留自早期草稿口径）。可复现、可引用的 pair 级数字为：**29 翻转 / 37 高影响 / 14 交集 / 23 稳定但高影响 / 15 翻转但低影响**；"一半高影响对从未翻转" 的正确表述是 **23/37 ≈ 62%**（结论方向不变：影响与不稳定性仍严重错位）。

## 5. 执行事实复核（报告 §A）

- 420/420 planned calls 全部在 `gate2_live_pair_calls.jsonl` 中，15/15 runs 完整；
- prompt tokens 逐行求和 = 327,530，completion = 1,260（报告：327,530 / 1,260）；
- §A.2 的超长请求定位到 `G2-51-r4` pair_position 25，latency 20,137s（约 5.6 小时；响应有效，未重试，数据点保留）。

## 6. 遗留事项（进入 P1/P2 前必须知道）

1. **端点偏差**：本数据来自 Paratera 网关的 `DeepSeek-V4.1-Flash`，非原 P0 冻结端点；翻转率不可与 Gate 1 官方端点数字直接比较。
2. **统计力**：每对仅 5 次重复，`ever-flipped` 是粗糙的翻转指示器（p≈0.2 的对在 5 次内至少翻转一次的概率仅 ~67%）；正式结论需按两周计划补充独立查询与重复。
3. **信息边界**：完整响应表只可用于离线评价与反事实上界；在线策略只能读执行轨迹；新分支请求必须实际调用。违反此边界的结果无法声称在真实系统可实现。
4. **nDCG@3 的局限**：每 query 恰 1 篇相关文档且 15/15 run 停在 rank 1，答案不稳定性全部发生在 rank 2–3 —— 该指标在此设计下对不稳定性不敏感。

---
*Audit JSON: `results/gate2c_audit.json`; 生成于 2026-10-15T04:26:30+00:00 之后，数据均未改动。*
