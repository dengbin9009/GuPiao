# GuPiao 数据迁移包

本目录用于把当前服务的历史业务数据迁移到另一台服务器。

## 包含内容

- `database/gupiao-sanitized.sqlite3.zst.part-*`：SQLite 数据库压缩分片。
- `database/table-counts.txt`：源数据库与迁移快照的表行数对照。
- `database/integrity-check.txt`：迁移快照的 SQLite 完整性检查结果。
- `data/market/`：本地分钟和小时行情缓存。
- `data/backtests/`：回测权益曲线缓存。
- `restore.sh`：合并分片、解压并校验数据库。

## 安全说明

为避免把凭据提交到代码仓库，以下表只保留结构，不保留数据行：

- `administrators`
- `broker_gateways`
- `notification_channels`
- `live_trading_accounts`

因此目标服务器必须通过受保护的运行环境重新配置管理员账号、通知渠道和券商网关。当前系统仍应保持：

```text
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
```

不要把 `.env` 或任何真实令牌放入本目录。

## 恢复

在本目录执行：

```bash
./restore.sh /srv/gupiao/backend/gupiao.db
```

如果目标使用 Docker Compose，应将恢复后的 SQLite 数据库挂载到后端工作目录，并把
`DATABASE_URL` 指向它。若目标改用 MySQL，不能直接把 SQLite 文件当作 MySQL 数据目录；
需要先完成 SQLite 到 MySQL 的逻辑导入和版本迁移。

恢复完成后，先检查：

1. `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check` 均通过。
2. `database/table-counts.txt` 中除脱敏表外的业务表行数一致。
3. `data/market/` 和 `data/backtests/` 已复制到目标项目的 `data/` 目录。
4. 首次启动前已设置新的 `GUPIAO_SECRET_KEY`、管理员账号和密码。
5. `LIVE_TRADING_ENABLED=false` 且 `BROKER_ADAPTER=simulation`。
