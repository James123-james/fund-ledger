# 半自动双轨周报（research）

当前是 **Milestone 2：AKShare 覆盖验证完成**。
只读台账和数据包骨架可用；AKShare 能覆盖一部分方向的 5 年估值，其余只能做价格分位。

## 已实现

- 只读台账适配器：持仓、权重、汇总、流水/计划数量
- 观察池 YAML：当前是 13 个方向的草稿
- 生成 `data-pack.json` 骨架
- 一键脚本 `python3 -m research.weekly`
- AKShare 覆盖检查：`python3 -m research.check_source`
- 不写 `data/fund.db`，不生成交易

## 运行

在仓库根目录：

```bash
python3 -m research.ledger_adapter          # 打印台账快照
python3 -m research.weekly                 # 生成本周 data-pack.json
python3 -m research.weekly --week 2026-W46 # 指定周
python3 -m research.check_source          # 检查 AKShare 覆盖
```

输出：

```text
research/data/2026-W46/data-pack.json
```

格式与 `docs/weekly-report-template/_template/data-pack.json` 对齐。

## 目录

```text
research/
├── __init__.py
├── ledger_adapter.py        # 只读台账
├── build_pack.py            # 数据包骨架
├── weekly.py                # 一键入口
└── config/
    ├── watchlist.draft.yml  # 13 个方向草稿
    └── settings.example.yml # 台账地址、路径、报告上限
```

## 观察池草稿

`watchlist.draft.yml` 里 13 个方向：

- 宽基：沪深300、中证A500、科创50
- 风格：红利低波
- 行业/主题：半导体、机器人、创新药、化工、有色、电力、消费、港股科技
- 商品：黄金

`proxy` 是给数据源用的指数/ETF 映射；空的表示待映射。
`existing_overlap` 标了与现有基金的重叠，周报会用它做交叉检查。

**这是草稿**，确认后把 `status: draft` 改成 `active`，并删掉不需要的方向。

## AKShare 验证结果

已安装 AKShare 1.20.37，`python3 -m research.check_source` 的结论：

| 类型 | 方向 | 数据 |
| --- | --- | --- |
| 5 年 PE/PB | 沪深300 | Legulegu 月频，61 期 |
| 5 年 PE/PB（代理） | 红利低波 | 上证红利代理，61 期 |
| 行业 PE/PB 快照，5 年历史可拉 | 半导体(电子)、机器人(机械)、创新药(医药)、化工、有色、电力(公用)、消费(食品饮料) | 申万一级，月频约 60 次请求 |
| 只有价格分位 | 科创50、黄金、港股科技 | 新浪 ETF 历史，约 1210 个交易日 |
| 缺代理 | 中证A500 | 缺 ETF 代码 |

注意：

- 东方财富 push2 会限流，首次约 5 分钟，反复触发可能 40 分钟以上；ETF 价格改用新浪，避开它
- 申万行业 5 年前只有 27 个，现在是 31 个，行业分类会变；历史要用**指数代码**对齐，新行业没有完整 5 年
- 主题类（半导体/机器人/创新药/电力/消费）用的是行业代理，不是原主题指数
- 黄金/港股科技/科创50 只有价格分位，没有 PE/PB；报告要明确标注

## 下一步

1. 写 `fetch_sw_history.py`：按月拉申万行业 PE/PB，缓存到 `research/data/`
2. 写 `metrics.py`：5 年估值分位、价格分位、动量、与持仓相关性
3. 把指标写进 `data-pack.json`
4. 用 skill 读数据包写 `weekly.md`，加质检与 `decisions.csv`

## 约定

- 台账只读，不改正式库
- 报告不对比配置模板，不做再平衡建议
- 不给具体基金代码/买卖点/仓位比例
- 每周最多 3 个方向进入报告
- 没有合格方向就写“本周无合格方向”
- `research/data/` 与 `reports/` 不入库（含个人数据）
