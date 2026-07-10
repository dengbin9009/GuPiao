# 一夜持股法股票池选股与真实分钟线回测增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 让一夜持股法从配置股票池中筛选候选股并选出排名第一的目标，同时补充一个优先本地缓存、缺失再在线补拉的最近两日真实分钟线隔夜回测脚本。

**架构：** 在后端现有 `overnight_strategy` 与 `services` 之上增加一层“股票池候选构建/选择”能力，复用现有候选过滤逻辑，避免继续依赖自选股顺序。回测部分新增独立脚本和少量缓存辅助函数，严格禁止回退到演示生成 K 线。

**技术栈：** Python 3.12、SQLAlchemy/SQLite、FastAPI 后端现有服务层、Pytest、Parquet/CSV 市场缓存。

---

### 任务 1：补充股票池选股的失败测试

**文件：**
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/tests/test_core.py`
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/tests/test_overnight_strategy.py`

- [ ] **步骤 1：先写失败测试，覆盖“不能再按自选第一只选股”**

- [ ] **步骤 2：运行定向测试，确认在当前实现下失败**

运行：

```bash
cd /Users/dengbin/Code/github/GuPiao/backend
./.venv/bin/pytest tests/test_core.py tests/test_overnight_strategy.py -q
```

- [ ] **步骤 3：记录失败原因，确保失败来自当前选股逻辑**

### 任务 2：实现股票池候选构建与一夜持股法选股

**文件：**
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/app/overnight_strategy.py`
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/app/services.py`

- [ ] **步骤 1：新增股票池候选构建与选股结果辅助函数**

- [ ] **步骤 2：让 `execute_simulation_strategy` 复用候选过滤逻辑，从股票池中选出排名第一的股票**

- [ ] **步骤 3：把扫描数量、接受数量、拒绝数量、最终选中股票写入运行摘要/日志**

- [ ] **步骤 4：重新运行定向测试，确认股票池选股逻辑通过**

运行：

```bash
cd /Users/dengbin/Code/github/GuPiao/backend
./.venv/bin/pytest tests/test_core.py tests/test_overnight_strategy.py -q
```

### 任务 3：补充分钟线覆盖与真实隔夜回测的失败测试

**文件：**
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/tests/test_market_cache.py`
- 新增：`/Users/dengbin/Code/github/GuPiao/backend/tests/test_recent_overnight_backtest.py`

- [ ] **步骤 1：先写失败测试，覆盖本地缓存优先、缺失再在线补拉、仍缺失则失败**

- [ ] **步骤 2：运行定向测试，确认在当前实现下失败**

运行：

```bash
cd /Users/dengbin/Code/github/GuPiao/backend
./.venv/bin/pytest tests/test_market_cache.py tests/test_recent_overnight_backtest.py -q
```

### 任务 4：实现真实分钟线隔夜回测脚本与缓存辅助

**文件：**
- 修改：`/Users/dengbin/Code/github/GuPiao/backend/app/market_cache.py`
- 新增：`/Users/dengbin/Code/github/GuPiao/backend/app/recent_overnight_backtest.py`
- 新增：`/Users/dengbin/Code/github/GuPiao/backend/scripts/backtest_recent_overnight.py`

- [ ] **步骤 1：为分钟线缓存增加覆盖检查和合并写入能力**

- [ ] **步骤 2：实现最近两日隔夜回测核心逻辑，禁止回退到生成 K 线**

- [ ] **步骤 3：实现脚本入口，输出机器可读和人工可读结果**

- [ ] **步骤 4：重新运行定向测试，确认回测与缓存逻辑通过**

运行：

```bash
cd /Users/dengbin/Code/github/GuPiao/backend
./.venv/bin/pytest tests/test_market_cache.py tests/test_recent_overnight_backtest.py -q
```

### 任务 5：跑回归验证并更新必要文档说明

**文件：**
- 修改：`/Users/dengbin/Code/github/GuPiao/README.md`
- 修改：`/Users/dengbin/Code/github/GuPiao/specs/002-overnight-universe-backtest/plan.md`

- [ ] **步骤 1：运行回归测试，覆盖核心策略、缓存、回测与关键校验脚本**

运行：

```bash
cd /Users/dengbin/Code/github/GuPiao/backend
./.venv/bin/pytest tests/test_core.py tests/test_overnight_strategy.py tests/test_market_cache.py tests/test_recent_overnight_backtest.py tests/test_backtest_engine.py -q
./.venv/bin/python scripts/verify_backtest_acceptance.py
./.venv/bin/python scripts/verify_realtime_chain.py
```

- [ ] **步骤 2：在 README 中补充最近两日隔夜回测脚本的使用方式**

- [ ] **步骤 3：确认本计划已执行完成并保留结果说明**
