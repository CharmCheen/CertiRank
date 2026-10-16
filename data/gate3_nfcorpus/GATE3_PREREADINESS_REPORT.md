# Gate 3 正式实验预审报告

日期：2026-10-15  
范围：Phase 0–4 无付费授权工作  
状态：**已停止在第一笔新的付费模型调用之前**

---

## 1. 执行摘要

| Phase | 状态 | 结论 |
|---|---|---|
| 0 审计 | 已完成 / 已验证 | 协议 `0.3.0-research-pilot` 自洽；temperature null/0 语义正确；heldout 需 freeze+model |
| 1 真实数据 | 已完成 | BEIR NFCorpus 6 dev + 12 heldout 已冻结；nDCG@3 ∈ [0.20, 0.92]，非 Gate2 饱和 |
| 2 温度校准 | 计划已冻结 / **未付费执行** | 320 次请求清单与 Dry Run 就绪 |
| 3 成本 | 部分完成 | Token 量已估；**Paratera 单价未核实，不输出 USD** |
| 4 科学有效性 | 已完成 | 见 §5；主目标应定为 Ground-truth Quality，非 Exact Reproducibility |
| Go/No-Go | **有条件 Go（Conditional Go）** | 见 §7 |

---

## 2. Phase 0 — 实现审计

### 2.1 协议与版本

- `configs/protocol.yaml`：`certirank-gate2c` / **`0.3.0-research-pilot`**
- `configs/experiment.json`：`protocol_version=0.3.0-research-pilot`
- 主预算 `0.4`（+40%）、次预算 `0.2`（+20%）；`review_budget(100,"0.4")=40`

### 2.2 已验证

| 检查项 | 结果 |
|---|---|
| `temperature: null` | **省略字段**（`request_body_extras` 无 temperature） |
| `temperature: 0.0` | **显式发送** `temperature: 0.0` |
| 两者 request_hash | 不同（model_parameters 含 temperature） |
| Provider | Paratera `https://llmapi.paratera.com` + `GLM-5.3-Flash` |
| thinking | `thinking_mode=low` → wire `reasoning_effort=low` |
| Heldout 密封 | 无 `--freeze` + model 时 `ValueError: sealed` |
| 历史暴露查询 | 636/327/51 不能进 heldout |
| 策略信息隔离 | `select_pair` 只见 `PolicyView(visible, reviewed, result, remaining)` |
| 复核计费 | probe 预留 2 调用；新分支 observe 计费；跨执行缓存禁止 |
| +40%/+20% 成功门 | abs≥0.05 & rel≥25% & quality_delta≥−0.02 & positive_groups≥2；另需 bootstrap 区间 |

### 2.3 离线回归

- `python scripts/check_offline.py`：**22/22 审计 + 154 tests 全过**
- 未修改协议成功标准；未改 LOTUS 生产代码

### 2.4 风险备注

- `Confidence` 不可用（无 logprobs）→ 主张限于优于 Random / SimpleTopology 等可用基线
- 策略候选：Base / Random / SimpleTopology / TraceSensitivity / Vote3 / **ReviewValue**

---

## 3. Phase 1 — 真实 Query Group 冻结

### 3.1 数据

| 项 | 值 |
|---|---|
| 数据集 | BEIR **nfcorpus**（官方 zip） |
| URL | `https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip` |
| 规模 | 3,633 docs / 3,237 queries |
| Qrels | 分级：1=possibly relevant，2=perfectly relevant |
| 检索 | 确定性 BM25（k1=1.2，b=0.75，tokenizer `[a-z0-9]+`，title+text，tie=`(-score, doc_id)`） |
| 候选 | 每查询 Top-8 |

产物目录：`data/gate3_nfcorpus/`

- `dataset_manifest.json`（含文件 SHA-256）
- `dev_queries.json`（6）
- `heldout_queries.json`（12）
- `selection_audit.json`
- `dataset_report.md`
- `bm25_top8.json`

### 3.2 预注册选样规则（LLM 调用前）

1. Dev ← `qrels/dev.tsv`；Heldout ← `qrels/test.tsv`（物理隔离）
2. 资格：
   - 全量 qrels ≥2 篇标注
   - Top-8 内 ≥2 篇标注
   - Top-8 内 ≥1 篇 grade=2
   - BM25 nDCG@3 ∈ **(0.20, 0.95)**（拒绝过易/过差）
   - 排除历史 ID 636/327/51
   - 与已选查询 token-Jaccard ≤0.80
3. 顺序：合格者按 `query_id` 字典序
4. **禁止**看 LLM 翻转后再选、按方法收益选样、改 heldout、人为插相关文档

### 3.3 选中结果

**Dev（6）**

| query_id | n_lab | n_perfect | nDCG@3 |
|---|---:|---:|---:|
| PLAIN-101 | 4 | 3 | 0.531 |
| PLAIN-11 | 2 | 2 | 0.387 |
| PLAIN-142 | 6 | 1 | 0.521 |
| PLAIN-2429 | 2 | 2 | 0.387 |
| PLAIN-2479 | 3 | 3 | 0.235 |
| PLAIN-2539 | 4 | 1 | 0.840 |

**Heldout（12）**：PLAIN-112, 165, 175, 2, 23, 2450, 2490, 2540, 2630, 2640, 2650, 2680  
nDCG@3：0.20–0.92；n_lab：2–7

相对 Gate 2（单相关文档、nDCG 全 1.0）：**质量分辨率已具备**。

### 3.4 评测约定

- 候选中未标注文档按 **BEIR 约定记 0 分**，**不得**解释为人工确认不相关
- Recall@K 使用 `qrels score>0`；grade 信息保留在 JSON 中供 nDCG 分析

### 3.5 本轮禁止项已遵守

- Heldout **未**调用 LLM
- 未用合成 `queries.example.json` 作为正式数据

---

## 4. Phase 2 — Temperature 校准计划

产物：`data/gate3_nfcorpus/temperature_calibration/calibration_plan.json`

| 线 | 请求行为 | 请求身份 |
|---|---|---|
| A `A_null` | **不发送** temperature | request_hash_A |
| B `B_zero` | 发送 `temperature: 0.0` | request_hash_B |

设计（协议建议值）：

```
4 queries (dev 前 4 字典序) × 8 pairs × 5 reps × 2 lines = 320 次
```

- Pair 选择：每查询 28 对中 `pair_id` 字典序前 8
- 跨线同 pair 的 request_hash **已验证不同**
- Dry Run：两线均产出可解析记录；**零付费调用**

校准后应计算：

- Ever-flipped Pair Ratio  
- Pairwise Run Disagreement  
- Invalid Response Rate  
- 跨配置差异  

解释边界（已写入 plan）：

- 5 次重复**不能**认证确定性，也**不能**给出单次翻转概率  
- 5 次内零翻转 ≠ 模型严格确定  

可执行命令（**需授权后**）：

```bash
# 线 A：temperature=null（需临时 API 配置 temperature: null）
python scripts/collect_pair_responses.py \
  --queries data/gate3_nfcorpus/calibration_queries_A.json \
  --split dev --out data/gate3_nfcorpus/temperature_calibration/run_A \
  --repetitions 5 --query-prefix calibA- --k 3

# 线 B：temperature=0.0（当前 configs/api_config.json 即 0.0）
python scripts/collect_pair_responses.py \
  --queries data/gate3_nfcorpus/calibration_queries_B.json \
  --split dev --out data/gate3_nfcorpus/temperature_calibration/run_B \
  --repetitions 5 --query-prefix calibB- --k 3
```

（正式执行前用 `calibration_plan.json` 生成对应 4×8 查询子集 JSON，并确认 api_config 的 temperature 字段。）

---

## 5. Phase 3 — 成本估算

产物：`data/gate3_nfcorpus/cost_estimate.json`  
脚本：`scripts/estimate_gate3_costs.py`（可传入真实单价）

### 5.1 Token 估算（chars/4 启发式 + 实测对照）

| 阶段 | 计划调用 | prompt 估/条 | prompt 总计 | completion 估/条 |
|---|---:|---:|---:|---:|
| Temperature Calibration | 320 | ~925 | ~2.96e5 | 12（观测） |
| Formal Dev | 2,016 | ~834 | ~1.68e6 | 12 |
| Formal Heldout | 4,032 | ~906 | ~3.65e6 | 12 |
| **Formal 合计** | **6,048** | — | **~5.33e6** | — |

实测参照（短文档 smoke，**不可直接外推**）：prompt≈92，completion≈12，延迟≈0.95s。

保守 completion：32 tok/次 → formal 1.93e5 tok；硬上限 128 tok/次 → 7.74e5 tok。

### 5.2 单价（阻塞项）

**Paratera / GLM-5.3-Flash 官方计价未在本轮核实。**  
按要求**不编造 USD**。需你提供或确认：

- 非缓存输入 $/1M tok  
- 缓存输入 $/1M tok  
- 输出 $/1M tok  
- 失败/重试是否计费  

填入后：

```bash
python scripts/estimate_gate3_costs.py --price-in <X> --price-out <Y>
```

### 5.3 运行与恢复

- 并发建议 1（保时间戳可审计）；serial ≈ 320s + 2016s + 4032s ≈ **1.8 h** 纯等待（不含重试）
- 物理尝试上限建议：`ceil(6048 × 1.15) = 6,956`
- 金额上限 / 截止时间：**待你设定**

### 5.4 授权门（当前全部 False）

| 门 | 调用 | 已授权 |
|---|---:|---|
| Calibration | 320 | **否** |
| Formal Dev | 2,016 | **否** |
| Formal Heldout | 4,032 | **否** |

**不得沿用 Gate 2 授权。**

---

## 6. Phase 4 — 科学有效性审计

| 问题 | 判断 | 依据 |
|---|---|---|
| 8 候选是否足以做 Top-3？ | **是** | C(8,2)=28 对；nDCG@3 分辨率 0.20–0.92 |
| Qrels 能否评 nDCG@3？ | **是（有限）** | 分级 1/2；未标注按 0 分是评测约定 |
| 未标注文档偏差？ | **存在** | 已声明；不作“确认不相关” |
| 12 次重复够不够探索翻转？ | **仅探索** | 协议已写明；不可统计认证 |
| Pair 是否被当成独立 Query？ | **否** | 统计单位=query-group；组内变体先平均 |
| null vs 0.0 请求是否区分？ | **是** | 字段行为 + request_hash 已验证 |
| 四组对照是否控预算/信息？ | **是** | 同响应组配对；policy 只见 visible；禁跨执行缓存 |
| Selective Verification 是否偷看未来？ | **否** | `probe` 只用预留槽位 1/2/3 |
| Query-aware 是否有公平基线？ | **是** | Random / SimpleTopology / Vote3 / Base；Confidence=unavailable |
| 主评价目标？ | **应定为 Ground-truth Quality（Recall@K / nDCG@3）** | 见下 |

### 6.1 关键点：Reproducibility ≠ 收益

若合法缓存重放已使历史答案 100% 可复现，则提高 Exact Reproducibility **不能**作为 Selective Verification 的主收益。

主比较必须是：

1. **Ground-truth 质量**（独立 qrels 的 Recall@K / nDCG@3）  
2. 在质量不降（Gate ≤0.02）下，**执行间分歧 D_EM** 与 **调用成本**  

+40% 成功门（未改释义）：

- 绝对分歧下降 ≥ 0.05  
- 相对分歧下降 ≥ 25%  
- 质量下降 ≤ 0.02  
- ≥2 个 query-group 改善  
- 另：bootstrap 95% 增益下界 > 0  

---

## 7. Go / No-Go 建议

### **Conditional Go** — 批准后先做 320 次温度校准，再决定是否开 6,048

| 条件 | 动作 |
|---|---|
| 1. 确认 Paratera GLM-5.3-Flash 单价并填写 `cost_estimate.json` | 你 |
| 2. 设定金额上限 + 物理调用上限 + 截止时间 | 你 |
| 3. 明确批准 **仅 320 次** calibration | 你 |
| 4. 校准后若 A/B 均无足够分歧 | **No-Go**（不得靠抬温度/筛查询硬推） |
| 5. 校准显示可观测分歧 + 费用可接受 | 批准 Dev 2,016 → freeze → Heldout 4,032 |

### No-Go 触发器

- 两条温度线下 ever-flip / run-disagreement 均接近 0  
- 费用超你设定上限  
- 校准出现大量 invalid parse（>5%）且无法在不改冻结 prompt 的前提下解析  

---

## 8. 交付物清单

| 交付物 | 路径 | 状态 |
|---|---|---|
| Dataset Manifest | `data/gate3_nfcorpus/dataset_manifest.json` | 已完成 |
| Dev Query Groups | `data/gate3_nfcorpus/dev_queries.json` | 已完成 |
| Heldout Query Groups | `data/gate3_nfcorpus/heldout_queries.json` | 已完成（未碰 LLM） |
| Selection Audit | `data/gate3_nfcorpus/selection_audit.json` | 已完成 |
| Dataset Report | `data/gate3_nfcorpus/dataset_report.md` | 已完成 |
| 校准冻结计划 + Dry Run | `data/gate3_nfcorpus/temperature_calibration/` | 已完成（未付费） |
| 成本估算 | `data/gate3_nfcorpus/cost_estimate.json` | 部分（缺单价） |
| 协议一致性审查 | 本文件 §2 / §6 | 已完成 |
| 可执行校准命令 | 本文件 §4 | 已给出 |
| Dev→Freeze→Heldout 计划 | 本文件 §7 | 已给出 |
| Go/No-Go | 本文件 §7 | **Conditional Go** |

---

## 9. 分类总表

### 已完成
- Phase 0 审计与回归  
- NFCorpus 冻结 6+12  
- 校准计划 320 条与 Dry Run  
- Token/时间估算与授权门框架  

### 已验证
- temperature null/0 语义  
- heldout 密封  
- 策略信息隔离  
- +40%/+20% 门定义未变  

### 尚未执行
- 任何新的付费调用（320 / 2,016 / 4,032）  
- Paratera 单价核实  
- 校准指标计算  

### 存在风险
- 未标注文档的评测约定偏差  
- 12 重复仅探索级  
- chars/4 token 估计误差  
- 重试计费规则未知  

### 需要你决定
1. Paratera 单价（或发票口径）  
2. 金额/调用/时间上限  
3. 是否批准 **320 次** 温度校准  

---

**本报告对应工作在第一笔新付费调用前全部停止。**
