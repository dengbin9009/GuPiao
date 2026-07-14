# GuPiao 代理工作约定

## 开始工作前

1. 先阅读 `specs/` 中与任务相关的规格、计划和快速开始文档。
2. 再阅读 `STATE.md`、`LOOP.md`、`loop-constraints.md`，确认当前目标、退出条件和硬门禁。
3. 所有后续项目文档默认使用中文。
4. Python 固定使用 3.12；Node.js 固定使用 20。

## 安装、测试与构建

后端安装（在仓库根目录执行）：

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[market,agents,dev]'
```

后端测试：

```bash
cd backend
.venv/bin/python -m pytest -q
```

需要静态检查时：

```bash
cd backend
.venv/bin/python -m ruff check .
```

前端安装与构建（必须使用 Node 20）：

```bash
cd frontend
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm install
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm run build
```

## 工程纪律

- 按 TDD 工作：先补充或调整会失败的测试，确认失败原因，再做最小修复，最后重构并运行相关测试。
- 修复必须在专用 worktree 中完成，禁止直接在 `main` 工作。
- 一次只处理一个可验证问题，不得顺手重构无关代码，也不得通过删除、跳过或弱化测试来获得绿灯。
- 声称完成前必须执行 `LOOP.md` 中的验收命令或等价检查，并记录证据；局部测试不能替代全套测试。
- 不得自动 deploy、push、merge 或创建真实交易动作；这些操作都需要人工明确批准。

## 交易与密钥安全硬门禁

- 不得读取、提交、复制或输出 `.env`、`.env.*` 及其中任何密钥、令牌、密码或账户信息；只可阅读不含密钥的示例和规格文档。
- 不得启用 `LIVE_TRADING_ENABLED`；所有运行和测试必须保持 `LIVE_TRADING_ENABLED=false`。
- 不得把 `BROKER_ADAPTER` 改为 `simulation` 之外的值；必须保持 `BROKER_ADAPTER=simulation`。
- 不得调用真实券商接口、解锁真实账户或发送任何真实订单。
- 任何涉及实盘配置、账户、适配器、下单、推送、合并或部署的步骤必须停止并请求人工确认。
