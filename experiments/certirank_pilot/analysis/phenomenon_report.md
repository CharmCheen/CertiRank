# 离线现象报告：历史响应上的固定 Tournament

证据等级：探索性。3 个历史查询已暴露给开发；新物理调用 0。

同一原始比较矩阵分别运行两个固定算子。算子输出差异单独列示，不能算作同计划随机性。
主指标使用无序 Top-3 集合；每查询 5 次单响应执行，执行对相互依赖。

| 查询 | 算子 | D_EM | Jaccard 距离 | 平均调用 | Recall@3 |
|---|---|---:|---:|---:|---:|
| 636 | Tournament | 0.600 | 0.300 | 10.2 | 1.000 |
| 636 | naive | 0.800 | 0.400 | 28.0 | 1.000 |
| 327 | Tournament | 0.900 | 0.540 | 11.0 | 1.000 |
| 327 | naive | 0.400 | 0.200 | 28.0 | 1.000 |
| 51 | Tournament | 0.000 | 0.000 | 10.6 | 1.000 |
| 51 | naive | 0.000 | 0.000 | 28.0 | 1.000 |

## 轨迹可观测性与单请求翻转

分母为每次执行实际观察到的唯一请求，汇总为 run-request 级；同一请求复用遵循统一缓存规则。
conditional_safe / conditional_impact 均以其他已观察判断固定为条件；不能解释为未来重采样保证。
unresolved 在首个未观察比较处停止。Oracle 用该次响应槽位的完整表补全反事实，计算新增请求数；这些结果只供离线分析。

| 查询 | 观察请求 | conditional_safe | conditional_impact | unresolved | Oracle 集合影响 | unknown 且有影响 | 新请求总数 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 636 | 51 | 4 | 5 | 42 | 34 | 29 | 103 |
| 327 | 55 | 1 | 5 | 49 | 40 | 35 | 107 |
| 51 | 53 | 5 | 5 | 43 | 27 | 22 | 104 |

完整逐请求记录见 `counterfactuals.jsonl`，其中包含新分支分布，不能用平均数替代尾部。

## 历史 naive 指标对齐

r0 集合口径：{'high_impact': 28, 'ever_flipped_and_high_impact': 12, 'high_impact_never_flipped': 16, 'ever_flipped_low_impact': 17}。有序口径 37 与集合口径 28 分别保留。

## 当前裁决

P1 执行基础已实现；P2 历史配置的现象与可观测性诊断已生成。
H0 按本表逐查询解释；H1/H2/H3 尚无真实策略收益验证。
历史 5 次响应最多容纳一个完整三次投票组，重叠组合无法扩充独立执行数。
可继续合成数据上的策略接口与成本归因验证；正式开发/留出评价需补数据。
Confidence 标为 unavailable；TraceSensitivity 属未冻结候选。不得据此宣称超越置信度。

另生成 48 行单响应组三次投票工程 smoke（`strategy_smoke.jsonl`）：Base、Random、SimpleTopology、TraceSensitivity、Vote3。
全部复核方法在固定选择规则下与合法缓存重放核对输出、状态和逐请求账目；因只有一个独立组，策略 D_EM 保持 null。
确定性产物保留节点计数；完整 CPU 时间由独立 benchmark 脚本测量，真实服务延迟尚未测量。

`subset_sensitivity.json` 平衡每个初始响应槽位：5 响应产生 30 个初始/复核角色分配，每个初始槽位出现 6 次；全部相互关联。
`review_funnel.json` 给出实际复核触发漏斗；`budget_feasibility.json` 给出绝对调用预算的强制翻转修复可行域。
`budgeted_oracle.json` 用实际追加响应与预算枚举合法复核序列；当前只有一个响应组，完整算法分歧保持未知。
ReviewValue 已提供开发集拟合接口，历史数据无法拟合独立组收益，故本报告不运行该策略。

预算估算只支持历史情景参考，付费运行仍需授权与完整 run manifest。
