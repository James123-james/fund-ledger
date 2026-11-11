---
name: fund-ledger-weekly-report
description: 读取 research/data/<week>/data-pack.json，生成双轨周报 weekly.md，并执行质检；只使用数据包里的数字，不编造行情。
whenToUse: 用户要跑每周双轨周报、周报质检，或说“用数据包写周报”“生成本周 weekly.md”时使用。
---

# 双轨周报生成

## 输入

1. `research/data/<week>/data-pack.json`
2. `docs/weekly-report-template/_template/weekly.md`（结构参考）
3. 上一周 `weekly.md`（如果有，用于“本周变化”）

## 数据包字段

- `macro[].status`：`ok` / `manual` / `missing`
  - `manual` 看 `as_of`：超过 7 天算 stale，不用于 Regime
- `catalysts[].status`：`confirmed` / `candidate`
  - 只有 `confirmed` 能当主催化；`candidate` 只能写“待确认”，不能算已落地
- `data_status`：`full` / `partial` / `price_only` / `missing`
- `max_corr_120d` / `overlap`：>0.7 为高重叠，必须降级

## 硬规则

- **数字只能来自数据包**；数据包没有的，写“未接入/缺失”，不许补。
- `data_status` 决定写法：
  - `full`：有 PE/PB 分位和价格分位，可做估值判断。
  - `partial`：历史不足，标注“样本不足”。
  - `price_only`：**只能写“价格分位”，不能写“估值分位”**。
  - `missing`：不进入方向候选，只在数据质量里列出。
- `proxy: true` 或 `source: sws` 的行业代理，必须写“代理”，不能当成原主题指数。
- 宏观 `macro` 有 `missing`/stale、或候选没有 `confirmed` 催化时：
  - 不要硬写方向结论。
  - 输出“本周无合格方向（宏观/催化数据未齐）”，并给数据观察表。
  - 本周决定写“不操作”。
- 只有 `candidate` 催化时，可以列入“待确认观察”，不能写成“催化已落地”。
- `overlap` 为 true 或 `max_corr_120d >= 0.7` 的方向，必须降级，不能作为新增方向。
- 每周最多 **3 个方向**进入正文；没有合格方向就空着。
- 不给具体基金代码、买卖点、仓位比例；只给筛选条件。
- 不对比任何配置模板，不做再平衡/偏离分析。
- 每个深度方向必须有：
  - 一句逻辑（≤30 字）
  - 数字锚、事件锚（`confirmed` 催化）、否决锚
  - ≥3 条反面证据（数据/事件/时间）
  - “什么情况说明我错了”
  - 与现有持仓的关系（数据包有则写，没有写“未计算”）

## 输出

写 `reports/<week>/weekly.md`，结构：

1. 一句话结论
2. 宏观与 Regime（没有就写未接入）
3. 本周变化（对比上周；没有上周就写“首期”）
4. 价值轨（最多 2 个；没有就写“本周价值端无合格方向”）
5. 趋势轨（最多 2 个；没有就写“本周趋势端无合格方向”）
6. 持仓交叉检查（数据包只有权重/重叠提示时，不编相关性）
7. 本周决定（0–2 条；不操作也写）
8. 下周三件事
9. 数据质量（missing/partial/代理/价格分位/manual stale）
10. 质检

## 质检

- 替换测试：换掉方向名，结论是否仍成立
- 反面证据 ≥3，且有数据/事件/时间
- 数字锚/事件锚/否决锚齐全
- 无套话词：建议关注、结构性机会、长期向好、分散配置、市场承压等
- 可证伪
- 宏观 → 资产 → 动作链完整；没有宏观就不写方向
- 本周变化点已对比

不通过就重写，最多 2 次；仍不通过就在周报里写“质检未通过”，不要伪装成通过。
