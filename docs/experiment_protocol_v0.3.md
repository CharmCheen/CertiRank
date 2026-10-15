# 研究实验协议 v0.3

目标保持用户版研究总纲 H0–H3、原 +40% 主预算和成功/停止阈值。
版本 `0.3.0-research-pilot`。实际运行设置统一读取 `configs/experiment.json`，
记录解析后的配置及其哈希；配置参与运行，不仅用于来源描述。

## 从研究目标到实现

| 原问题 | 当前处理 | 证据边界 |
|---|---|---|
| 强制翻转影响无法代表复核价值 | 保留 TraceSensitivity 为结构基线；新增 ReviewValue 开发统计估计器 | 真实数据尚不足拟合；未宣称策略有效 |
| N=8 预算小、无修复空间 | 保留 20%/40%，增加独立标记的 2/4/6/8 调用开发网格与 N=16 合成诊断 | 不用诊断最好档位替换主比较 |
| smoke 没触发真实新分支 | 逐复核记录投票、判断改变、集合变化、需新分支、修复调用和中止 | 历史未触发仍如实保留；合成数据另行检验 |
| 缺少预算忠实的理想参考 | 显式枚举合法复核序列，使用实际响应，多数票、修复和预算完整执行 | 只有搜索完整时才是该有限响应表的经验最优；截断为乐观已达参考 |
| 节点计数遗漏成本 | 独立 CPU benchmark 包括选边、复制、失效扫描、轨迹维护、提取、复核和账目组装 | 本机合成微基准，不等同真实服务成本或延迟 |
| 配置/分组/评价未闭环 | 统一入口、分池读取、冻结代码/模型/配置、组级 bootstrap、标签质量及 Gate 报告 | 无新真实数据；未来采集和 live 运行仍需授权 |

## 策略与价值估计

Base、Random、SimpleTopology、TraceSensitivity、Vote3 均保留。
TraceSensitivity 只按条件翻转影响分类，是结构基线。
ReviewValue 使用开发集上的实际三次投票和完整修复计算统计分桶：
候选规模、K、条件影响类型、拓扑距离。

目标为“干预后与其他独立基础执行的一致性增量”的单步代理；同时记录判断改变概率、
预算内完成概率、实际修复成本和独立标签质量变化。
选边使用预算可行的平均代理收益除以预计成本；样本不足、未知特征或无正收益时停止。
估计与实际动作一起写入日志，后续可在冻结留出结果上检查校准。
这里只拟合执行层统计表，不训练或修改 LLM。

该代理没有完整算法收益保证。测试含双方各自向对方基础答案移动、完整分歧仍不下降的反例；
最终必须以完整算法独立执行 D_EM/D_J、质量和实际成本判断。
历史每对五响应无法提供两个不重叠三槽位组，因此不在历史数据上拟合或伪造 ReviewValue。

## 响应、标签与分组

正式默认 12 响应/对，四个不重叠三槽位组。方法之间使用同组作配对，
不同完整执行使用不同组。时间漂移和服务依赖仍需用真实采集日志检查。
历史 636/327/51 属于 beir/scifact 的开发暴露查询，不能重新进入该数据集的留出分区。
变体共享 query-group，dev 与 heldout 的 workload/calls/identity 文件物理分开。

清单格式见 `configs/pool_manifest.template.json`。每个 pool 使用当前 C1–C8 原始日志契约，
分别钉住三个文件的 SHA-256；manifest 显式记录 label_source，workload 包含 qrels。
真实新池默认固定 N=8/K=3，变化需要预先改配置并形成新冻结记录。
已有历史响应不重排 A/B、不改变请求身份。

历史组合敏感性现在均衡初始槽位：5×C(4,2)=30 种角色分配，每个初始槽位各出现六次。
它们依然相互关联，禁止用作独立执行或显著性样本。

质量按独立标签的 Recall@K 报告，原质量下降 0.02 Gate 保留。
报告显式标记单相关文档查询数量与质量分辨率；当前历史 Recall 全为 1 的饱和性未被消除。
新的独立边界标签或多相关任务仍需真实数据；多数票或模型回答不能补成任务真值。
无 logprobs 时 Confidence unavailable，主张限于超越可用简单基线。

## 开发、冻结和留出

```bash
# 需要先准备完整、合法的数据清单；模板本身不可运行
python scripts/run_experiment.py --manifest POOLS.json --split dev --out DEV_RESULTS

# 只有明确作出第一周 proceed 裁决后，才请求创建冻结记录
python scripts/run_experiment.py --manifest POOLS.json --split dev --out DEV_RESULTS \
  --create-freeze --decision proceed

python scripts/run_experiment.py --manifest POOLS.json --split heldout --out HELDOUT_RESULTS \
  --model DEV_RESULTS/value_model.json --freeze DEV_RESULTS/freeze_manifest.json
```

所有命令可加 `--config CONFIG.json`。当前无真实新池，未创建真实方法冻结。
开发选择最强简单 comparator（Random/SimpleTopology）后锁定；候选默认 ReviewValue。
冻结记录绑定代码、配置、模型和分区清单。任何不匹配在读取留出响应之前拒绝。
开发结果只用于开发，不能作为泛化证据。

每查询先汇总执行对，再将同组查询变体平均，最后跨 query-group 等权平均。
Bootstrap 按 query-group 配对重采样；单组不输出区间。
failed 行保留账目并阻止不完整指标，aborted 完整回退输出保留在分母。
主预算判定保持：绝对改善≥0.05、相对改善≥25%、质量下降≤0.02、多个组有改善。
点估计与区间支持分别报告，零基线的相对改善为 null。

## 修复成本与当前取舍

策略主比较统一使用合法缓存重放，避免把未验证的复杂修复器收益混入选边比较。
增量执行继续用于独立正确性与 CPU 对照。
初次合成微基准没有显示达到 25% 的完整 CPU 改善；暂缓扩展通用修复器。
两者新响应调用相同符合当前缓存与选边语义，不把调用复用收益归因于增量机制。

CPU 是 `process_time_ns`，包含测量开销；报告交错运行、warmup 与原始重复记录。
不对计时做逐字节重放，也不把重复计时当作独立查询。`--check` 检查计时记录的来源和格式。
真实 HTTP、token、端到端延迟、费用和重试仍未验证。

## 离线复现

```bash
python scripts/analyze_offline.py
python scripts/pilot_budget.py
python scripts/research_diagnostics.py
python scripts/benchmark_cpu.py
uv run --no-project --with pytest python scripts/check_offline.py
```

前三种分析的产物确定性再生成；CPU 数字每次测量可变化。
合成报告均标记 synthetic_only，真实历史报告不混入合成收益。
理想搜索截断、样本不足、质量未证实都保留为不可判断，不通过调预算或更换指标追逐阳性结果。
