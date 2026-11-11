# 半自动双轨周报（research）

当前是 **Milestone 1：只读台账 + 数据包骨架**。  
行情数据还没接，`candidates.metrics` 为空，暂时不能评分或写结论。

## 已实现

- 只读台账适配器：持仓、权重、汇总、流水/计划数量
- 观察池 YAML：当前是 13 个方向的草稿
- 生成 `data-pack.json` 骨架
- 一键脚本 `python3 -m research.weekly`
- 不写 `data/fund.db`，不生成交易

## 运行

在仓库根目录：

```bash
python3 -m research.ledger_adapter          # 打印台账快照
python3 -m research.weekly                 # 生成本周 data-pack.json
python3 -m research.weekly --week 2026-W46 # 指定周
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

## 下一步

1. 接入行情数据源（Eastmoney/AKShare/Tushare，先做固定观察池）
2. 计算估值分位、动量、资金流、与持仓相关性
3. 用 skill 读数据包写 `weekly.md`
4. 加质检与 `decisions.csv`

## 约定

- 台账只读，不改正式库
- 报告不对比配置模板，不做再平衡建议
- 不给具体基金代码/买卖点/仓位比例
- 每周最多 3 个方向进入报告
- 没有合格方向就写“本周无合格方向”
- `research/data/` 与 `reports/` 不入库（含个人数据）
