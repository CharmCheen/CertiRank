# Gate 2C 当前状态（P0 完成，2026-10-15）

> 阶段编号与日程以用户版 v0.1 计划（`docs/CertiRank_两周执行计划_2026-10-15至2026-10-28.md`）
> 为权威；本文件对应其 **P0（10/15：状态核对与协议定义）**，通过条件已全部满足。

## 一句话

Gate 2 的原始 artifacts 已完整恢复为不可变副本并建立数据契约；一条命令
（`python scripts/audit_gate2.py`）即可从原始响应独立复算全部核心统计
（22/22 通过，零 LLM 调用）；Pair Response Pool 确认可复用；发现并裁定
报告正文一处数字笔误（"46" 应为 37）；两周计划全部数据采集的金额上限已
由实测数据推算（`experiments/certirank_pilot/budget_estimate.md`）。

## P0 通过条件核对（对照两周计划 §5 P0）

- [x] **`current_state.md` + `protocol.yaml` 交付**（`configs/protocol.yaml`，
      P0 冻结版：数据契约 C1–C8、重放语义、信息边界、审计期望）；
- [x] **已完成数据有可定位来源**：LOTUS repo commit `136ae4f`、run
      `live_pilot_20261014T132233Z`，31 个文件 SHA-256 逐文件记录于
      `data/manifests/gate2_external_data.json`（`--check` 通过）；
- [x] **未提交修改没有被覆盖**：LOTUS 仓库只读引用，CertiRank 独立目录，
      未触碰原实验区；
- [x] **所有成本上限可计算**：见下文预算推算（两周计划 §7）。

## 研究总纲悬置问题的核对结果（总纲 §3.1 "密钥更新后的执行情况"）

总纲指出 2026-10-14 的 `gate2_live_report.md` 是鉴权失败的运行快照、密钥更新后
的执行情况待核对。核对结论：**该悬置已解除**，三段事实链如下——

| 时间（UTC） | 目录 | 结果 |
|---|---|---|
| 10-14 11:44 | `live_smoke_20261014T114455Z/` | 鉴权失败快照：`BLOCKED_PROVIDER_CREDENTIAL_INVALID`，1 个 run（G2-636-r0）、0 个比较、0 个模型回答（api key ****c424 invalid）。**总纲引用的就是这份** |
| 10-14 13:18 | `live_smoke_20261014T131841Z/` | 更新密钥后的 smoke：1 query（636）× 28 pair，28/28 strict-valid |
| 10-14 13:22 | `live_pilot_20261014T132233Z/`（主数据） | 完整 pilot：3 query × 28 pair × 5 响应 = 420/420 strict-valid，15/15 runs 成功，replay gate 15/15 |

即：顶层 `gate2_live_report.md` 描述的是**密钥更新后成功完成的 pilot**，不是失败
快照；失败快照作为独立目录原样存档，两者未混淆。据此，总纲 §3.1 表中
"密钥更新后的执行情况：待核对" 一行可以关闭，Gate 2 现象证据（H0 的初步
依据）以主 pilot 数据为准。

## 已恢复的实验资产

来源：LOTUS 非确定性研究（`lotus-code`，commit `136ae4f`），run
`live_pilot_20261014T132233Z`（2026-10-14，420 planned calls / 15 runs / 0 失败）。
全部文件为不可变副本，SHA-256 见 `data/manifests/gate2_external_data.json`：

| 类别 | 文件 | 用途 |
|---|---|---|
| 原始响应 | `gate2_live_pair_calls.jsonl`（420 行） | 审计与 Pool 的唯一判定来源 |
| 原始 run | `gate2_live_query_results.jsonl`（15 行） | replay gate 的比对目标 |
| 冻结 workload | `gate2_workload.json` | pair 表、钉死的 request_hash、候选表、qrels |
| 生产方报告 | `gate2_live_report.md` | 审计期望值的引用来源 |
| 生产方分析 | `gate2_live_analysis.json`、`gate2_live_replay_analysis.json` | 交叉核对目标 |
| 运行账目 | `gate2_live_manifest.json`、`*_verification.json` | 预算与验证状态记录 |
| 佐证 | `live_smoke_20261014T131841Z/`、`live_smoke_20261014T114455Z/` | 密钥前后 smoke 对照（见上表） |
| 端点配置 | `provider_runtime.yaml` | 仅环境变量名，无密钥 |
| 生产方脚本 | `lotus_scripts_reference/`（7 脚本 + gate2 包 5 模块） | 只读来源参照，CertiRank 不执行 |

## 数据契约（已冻结）

规范：`configs/protocol.yaml`；实现：`src/certirank/pair_pool.py`。
每条比较应答八类字段：query_id、候选 ID（含输入顺序）、pair 槽位、请求身份
（request_hash，与 workload 逐槽位 1:1 校验）、模型与 prompt 版本、输入顺序、
调用实例、原始回答 + 规范化判定。**仅凭 pair_id 相同不构成复用依据。**

## 审计结论（详见 results/gate2c_audit.md）

- **完整性**：3 query × 28 pair × 5 响应 = 420 条记录，无缺失、无多余，全部
  strict-valid；
- **可复现性**：独立重放 naive sort（CertiRank 自实现，非生产方代码），
  15/15 run 的 Top-3 与全排序和 LOTUS 记录完全一致；
- **统计复算**：报告 §A–§D 的全部数字复现（逐 query 翻转 8/9/12、macro
  34.5% / 16.7%、set/order agreement 60%/30%、Jaccard 0.80、τ-b 0.748、
  nDCG@3 1.0、单点四分类、交叉表 29/14/23/15、多点传播 0 joint / 6 cancelling）；
- **数字核查（PROSE-46）**：正文的 "46 single-flip high-impact pairs" 与其自身
  表格及存储分析矛盾，可复现值为 **37**（14 + 23）；"高影响对从未翻转" 的占比
  应表述为 23/37 ≈ 62%，结论方向不变；
- **Pool 可复用性**：84 slot × 5 响应，槽位内 5 次响应共享同一 request_hash
  （同一请求身份的独立重复）→ 可作为离线 Tournament 固定响应表；表外比较
  属新分支请求，必须实际调用。

## 预算推算（两周计划 §3.2 数据块，P0 通过条件）

由 420 条真实调用的 token 分布与 15 个 run 的计价记录反解
（`scripts/pilot_budget.py`，零 LLM 调用、幂等）：

- 实测单次逻辑调用：prompt 均值 780 tokens（provider 缓存命中 80%）、
  completion 恒 3、传输尝试 1 次、重试 0；中位延迟 0.78 s
  （唯一悬置请求 ~5.6 h 已单列，见审计 A-2/A-3）；
- 单价反解在 15 run × 2 计价情景上交叉验证，最大误差 2.3e-06 USD；
- **金额上限**（逻辑调用、不含重试）：开发集 2,016 次 $0.05–0.13；
  留出集 4,032 次 $0.09–0.25；N=16 扩展 5,760 次 $0.13–0.36；
  核心合计 6,048 次 **$0.14–0.38**；全量 11,808 次 **$0.28–0.74**
  （off_peak–peak × 低—高 token 档位）；
- 串行采集时间参考：核心合计约 1.3 小时（中位延迟）至 8.4 小时（p95）。

计价情景来自生产方 manifest 的估算记录（Paratera 网关 DeepSeek-V4.1-Flash），
非实际账单；正式立项前按当期价格复核。

## 统计单位（引用旧数字时使用）

- **pair 级**（分母 84）：29 翻转 / 37 高影响 / 14 交集 / 23 稳定但高影响 / 15 翻转但低影响；
- **run-pair 级**（30 个依赖样本，仅描述统计）：16.7% 平均分歧、60%/30% agreement；
- **run 级**（分母 15）：15/15 replay、nDCG@3 = 1.0。

## 边界与偏差（继承自 Gate 2 报告）

1. 端点偏差：Paratera 网关的 DeepSeek-V4.1-Flash，非原 P0 冻结端点；
   不可与 Gate 1 官方端点数据合并。
2. pilot 规模：3 query、每对 5 次重复；ever-flipped 是粗糙指示器
   （p≈0.2 时 5 次内至少翻转一次仅 ~67%）。
3. 信息边界：完整响应表只用于离线评价与上界；在线策略只读执行轨迹；
   新分支请求计入预算（`information_boundary`，自 P0 冻结）。

## 与用户 v0.1 两周计划的阶段映射

本仓库早期内部草稿（`docs/two_week_plan.md`，已标注让位）使用的 P0–P3 编号
与用户版计划不同，对应关系如下：

| 用户版计划（权威） | 本仓库已完成 / 待办 |
|---|---|
| P0（10/15）状态核对与协议 | ✅ 本文 + `protocol.yaml` + 预算推算 |
| P1（10/16）小例子与正确性边界 | ⬜ **下一步**：四元素反例 fixtures、Tournament Top-1/Top-3 固定规则、N=4 全 64 表穷举、增量 vs 从头重放、预算耗尽行为（全部零调用） |
| P2（10/17–18）现象验证 | 数据侧大部分已由审计覆盖（`gate2c_audit.md`）；待补：`phenomenon_report.md` 形态重组、保守 unknown 集合、5 响应子集方差描述、审计断言例行回归化 |
| P3–P7（10/19–28） | ⬜ 按用户版计划执行 |

`oracle.py`（真实调用通道）待任何需要新调用的阶段实现；`trace.py`/`policy.py`
/`repair.py` 分别在 P1/P3/待 P3 决策后实现。
