# 一夜持股概率组合策略实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 新增与原一夜持股法并行的概率组合模拟策略，使用独立 200 万元账户，每日动态选择最多 10 只股票，按校准概率非等权分配 2% 至 36% 单股仓位，并在下一交易日 10:30 退出。

**架构：** 新建 `probability_portfolio` 包，拆分模型、因子、分配、运行时初始化和交易执行；通过现有策略执行注册表和调度器接入。模型或真实因子未就绪时失败关闭，只允许无下单演练。账户估值和日亏损修复提取为共享模拟账户服务。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy、SQLite/MySQL、Vue 3、Pytest、Ruff、Node.js 20。

---

## 文件结构

- `backend/app/probability_portfolio/config.py`：默认参数、参数校验和 readiness。
- `backend/app/probability_portfolio/features.py`：真实因子构建和数据质量检查。
- `backend/app/probability_portfolio/model.py`：训练样本、逻辑回归、单调校准和预测。
- `backend/app/probability_portfolio/allocation.py`：候选选择、封顶再分配和整数手计划。
- `backend/app/probability_portfolio/runtime.py`：策略定义、独立账户和默认关闭计划初始化。
- `backend/app/probability_portfolio/execution.py`：入场、dry-run、持仓批次和 10:30 退出。
- `backend/app/simulation_accounts.py`：统一账户估值和当日收益口径。
- `backend/app/models.py`：新增概率策略审计实体和真实行情字段。
- `backend/app/database.py`：SQLite 增量迁移；MySQL 迁移脚本单独保存。
- `backend/app/strategy_execution.py`：注册 `portfolio_entry/portfolio_exit`。
- `backend/app/main.py`：readiness、运行详情和配置 API。
- `frontend/src/App.vue`：策略状态和逐股决策展示。

## 任务 1：建立数据模型和运行时初始化

- [ ] 先在 `backend/tests/test_probability_portfolio_models.py` 添加失败测试，验证四类审计实体、200 万独立账户、模拟模式和两条默认关闭计划。
- [ ] 运行定向测试，确认因模型和初始化函数缺失而失败。
- [ ] 在 `backend/app/models.py`、`backend/app/database.py` 和 `backend/app/probability_portfolio/runtime.py` 实现最小模型与幂等初始化。
- [ ] 添加 `backend/migrations/004_probability_portfolio_mysql.sql`，只执行新增表和新增列。
- [ ] 重新运行定向测试并提交。

## 任务 2：实现确定性的非等权分配器

- [ ] 在 `backend/tests/test_probability_portfolio_allocation.py` 添加失败测试，覆盖 0、1、少于10、超过10个候选、2%下限、36%上限、60%总上限和概率非等权。
- [ ] 运行测试并确认失败来自分配器不存在。
- [ ] 在 `backend/app/probability_portfolio/allocation.py` 实现稳定排序、动态总仓位和封顶再分配。
- [ ] 添加含滑点、费用、现金和100股整数手的计划数量测试与实现。
- [ ] 运行定向测试并提交。

## 任务 3：实现真实因子和失败关闭

- [ ] 在 `backend/tests/test_probability_portfolio_features.py` 添加失败测试，覆盖真实换手率、VWAP、MA5/MA20、动量、波动率、基准、上市日期、涨跌停和未来数据拒绝。
- [ ] 扩展 `Stock` 行情字段和实时行情标准化，缺失字段不得使用固定默认值。
- [ ] 在 `features.py` 使用已完成日线和截至14:40的分钟快照生成特征。
- [ ] 数据不足时返回结构化拒绝原因，不调用概率模型。
- [ ] 运行定向测试并提交。

## 任务 4：实现训练、校准和模型 readiness

- [ ] 在 `backend/tests/test_probability_portfolio_model.py` 添加无未来数据、14:40/10:30净收益标签、模型可复现、单调校准和 readiness 阈值失败测试。
- [ ] 在 `model.py` 实现版本化样本构建、逻辑回归和单调校准。
- [ ] 保存训练统计、Brier 分数、系数、映射与哈希；预测只读取 ready 产物。
- [ ] 增加 `backend/scripts/train_probability_portfolio.py`，默认只训练并报告，不启用调度。
- [ ] 运行定向测试并提交。

## 任务 5：修复统一账户估值与日亏损

- [ ] 在 `backend/tests/test_simulation_accounting.py` 添加失败测试，复现新持仓快照漏算市值、平仓残留市值和累计亏损被当作日亏损。
- [ ] 在 `simulation_accounts.py` 实现 `revalue_account`、`snapshot_account` 和 `daily_pnl_pct`。
- [ ] 将原一夜持股法和 TradingAgents 的账户估值调用迁移到统一实现，保持 API 兼容。
- [ ] 运行账户测试、原策略和 TradingAgents 回归并提交。

## 任务 6：实现组合入场与 dry-run

- [ ] 在 `backend/tests/test_probability_portfolio_execution.py` 添加失败测试，覆盖模型未就绪零订单、1至10只逐股成交、部分跳过、账户隔离和重复窗口幂等。
- [ ] 在 `execution.py` 实现候选快照、逐股决策审计、全量预检和模拟买入。
- [ ] 每笔买入创建 `StrategyPositionLot`，可卖日为下一交易日，退出时间为10:30。
- [ ] dry-run 保存完整决策但订单数必须为0。
- [ ] 运行定向测试并提交。

## 任务 7：实现10:30组合退出

- [ ] 添加失败测试，覆盖T+1、策略归属、10:30正常退出、行情过期重试至10:45、不可成交保留和后续交易日恢复。
- [ ] 实现只消费当前配置开放批次的退出逻辑和逐股卖出审计。
- [ ] 将 `portfolio_entry/portfolio_exit` 注册到策略执行表，并扩展调度容忍窗口。
- [ ] 运行执行与调度测试并提交。

## 任务 8：实现管理员 API 与控制台

- [ ] 在 `backend/tests/test_probability_portfolio_api.py` 添加 readiness、配置、dry-run、运行列表和详情的失败测试。
- [ ] 实现专用 API，拒绝LIVE模式和共享账户，模型未就绪时禁止启用入场计划。
- [ ] 在前端策略中心显示模型状态、200万账户、计划、候选概率、目标仓位和拒绝原因。
- [ ] 添加前端操作测试或可独立测试的动作函数，并运行 Node 20 构建。
- [ ] 提交 API 和界面改动。

## 任务 9：完整验证和无下单演练

- [ ] 运行概率策略全部定向测试。
- [ ] 运行后端全套 pytest 与 Ruff。
- [ ] 运行 Node 20 前端生产构建。
- [ ] 在临时 SQLite 数据库运行幂等初始化和 dry-run，验证200万账户、两条计划关闭、订单数0、真实订单数0。
- [ ] 更新 `quickstart.md`、`tasks.md`、`STATE.md` 和非敏感 Loop 记录。
- [ ] 保留功能分支，不自动push、merge、部署或重启主目录服务。

## 验收命令

```bash
cd backend
/Users/dengbin/Code/github/GuPiao/backend/.venv/bin/python -m pytest -q
/Users/dengbin/Code/github/GuPiao/backend/.venv/bin/python -m ruff check .

cd ../frontend
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm run build
```

