# Gate 3 正式实验结果

日期：2026-10-15  
数据：BEIR NFCorpus · GLM-5.3-Flash @ Paratera · temperature=null · N=8/K=3 · 12 次重复/对  
协议：`0.3.0-research-pilot`

---

## 1. 调用规模（已全部完成）

| 阶段 | 计划 | 完成 | 契约 |
|---|---:|---:|---|
| Temperature Calibration | 320 | 320 | OK |
| Formal Dev | 2,016 | 2,016 | OK |
| Formal Heldout | 4,032 | 4,032 | OK |
| **合计** | **6,368** | **6,368** | |

产物目录：`data/gate3_nfcorpus/formal/`

---

## 2. 非确定性现象（H0 支持）

| 集合 | pairs | ever-flip | ever-flip 比例 | mean pairwise disagree |
|---|---:|---:|---:|---:|
| Dev | 168 | 41 | **24.4%** | 8.7% |
| Heldout | 336 | 56 | **16.7%** | 6.0% |

结论：**相同请求的运行间分歧可测量**，与 Gate 1/2 一致。temperature=null 不能消除非确定性。

---

## 3. 主结果：ReviewValue vs Random（H1）

评价单位 = query-group；组内变体先平均；质量 = 独立 qrels 的 Recall@K。

### Dev（6 组）

| 预算 | abs gain | rel gain | quality Δ | positive groups | 点估计门 |
|---|---:|---:|---:|---:|---|
| +20% | 0.000 | 0.0% | 0.0 | 0/6 | 未过 |
| +40% | 0.083 | 21.4% | 0.0 | 1/6 | 未过（需 ≥2 组 & rel≥25%） |

### Heldout（12 组，冻结后）

| 预算 | abs gain | rel gain | quality Δ | positive groups | bootstrap 95% | 结论 |
|---|---:|---:|---:|---:|---|---|
| +20% | 0.000 | null/0 | 0.0 | 0/12 | [0, 0] | **门未过** |
| +40% | 0.000 | null/0 | 0.0 | 0/12 | [0, 0] | **门未过** |

**Verdict: `exploratory_or_gate_not_met`**

---

## 4. 解读（不改协议门）

1. **H0 成立（探索级）**：pairwise 判断翻转普遍存在，Dev 24% pair 在 12 次内 ever-flip。
2. **H1 未证**：在预注册门（abs≥0.05、rel≥25%、质量不降、≥2 组改善）下，**ReviewValue 未超过 Random**。
   - Heldout 上 gain 恒为 0：当前 ReviewValue 选边与 Random 在最终 D_EM 上不可区分。
   - 质量均未下降（Δ=0），但也无质量收益。
3. **成本**：ReviewValue 平均调用略低于 Random（约 −2 次/执行），**不是**协议主收益（主门是分歧下降）。
4. **不可做的事**：不得因门未过而事后改门、抬温度或筛查询重跑主结论。

---

## 5. 工程说明

- C5 原文复算与宽松 verdict 解析已对齐（支持「先推理后 Document k」）。
- 采集支持逐条 checkpoint 与 `--resume`；heldout 超时后从 3,887 续采至 4,032。
- 离线 `check_offline` 与 157+ 测试保持通过（若源码哈希变更需重刷产物）。

---

## 6. 下一步选项（需你决定）

| 选项 | 含义 |
|---|---|
| A. 停止当前路线 | 按两周计划：保留测量结论，不继续 ReviewValue |
| B. 诊断 H1 失败原因 | 分析 ReviewValue 选边是否过早停、特征桶是否过稀、是否更该优化质量而非 D_EM |
| C. 换目标重定义 | 以质量/可靠性门替代 D_EM 主门（**属改协议，需你明确批准**） |
| D. 扩展 N=16 / 更多 query | 增大现象分辨率（新授权） |

**默认建议：A 或 B。当前证据不足以宣称 Selective Verification 有效。**
