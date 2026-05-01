# TAO Monitor

一个运行在 Ubuntu 上的 TAO 生态监控项目，带网页管理后台、Telegram 推送、钱包地址监控、大额转账阈值监控，以及 systemd 开机自启能力。

## 功能

- 监控你导入的钱包地址，并支持备注别名。
- 扫描 TAO 链 finalized 区块中的全部 extrinsic，并自动分类为转账、质押、委托、子网、权重、代理、多签等动作。
- 当交易涉及监控钱包时，立即记录并推送到当前账号自己的 Telegram。
- 当交易涉及监控钱包时一定推送 Telegram；其他非监控钱包交易先记录命中，暂不主动推送。
- 通过网页管理链节点、个人 Telegram 参数、个人阈值和钱包列表。
- 可选接入 TaoStats API，用于在链上事件缺失时补全减仓实际 TAO 成交额。
- 使用本机 PostgreSQL 保存配置、钱包、事件和扫描进度，更新部署时不会丢数据。
- 提供后台账号体系：总管理员可创建普通账号给朋友使用，普通账号之间的钱包和事件互相隔离。
- 支持每天固定时间自动清理旧命中记录，避免历史数据长期堆积。
- 提供网页一键导出当前账号钱包清单，以及命令行整站备份脚本，避免云服务器删档或商家异常导致资料丢失。

## 技术结构

- `FastAPI`：网页管理台和接口服务。
- `PostgreSQL + SQLAlchemy + Alembic`：配置、钱包、事件、账号、用户通知队列、扫描状态和数据库迁移。
- `substrate-interface`：连接 Subtensor WebSocket，逐块解码 extrinsic 和关联事件。
- `httpx`：调用 Telegram Bot API。
- `TaoStats API`：可选补全动态 TAO 减仓成交额。
- `systemd`：Ubuntu 开机自启与自动拉起。

## 目录

- `app/`：后端服务、页面模板、静态资源。
- `deploy/systemd/tao-monitor.service`：systemd 服务定义。
- `scripts/deploy.sh`：首次部署脚本。
- `scripts/update.sh`：更新脚本。
- `scripts/backup.sh`：整站资料备份脚本。

## 本地开发

```bash
sudo apt install -y postgresql postgresql-client
sudo -u postgres createuser taomonitor --pwprompt
sudo -u postgres createdb -O taomonitor tao_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开：

```text
http://127.0.0.1:8080
```

## Ubuntu 部署

1. 先把代码上传到 GitHub，然后在 Ubuntu 服务器拉下来。
2. 安装系统依赖：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git rsync zip postgresql postgresql-client
```

如果你是直接用 `root` 登录服务器，没有 `sudo` 也可以，命令里的 `sudo` 去掉即可。

3. 克隆项目并部署：

```bash
git clone https://github.com/mmk58860-code/taotaoi.git /opt/tao-monitor
cd /opt/tao-monitor
chmod +x scripts/*.sh
./scripts/deploy.sh
```

4. 如需补充或修改生产配置，编辑 `.env`：

```bash
sudo nano /opt/tao-monitor/.env
```

如果你部署目录不是 `/opt/tao-monitor`，请把上面的路径换成你自己的实际安装目录。

说明：

- 第一次运行 `./scripts/deploy.sh` 时，脚本已经会自动创建 `.env`
- 并且会写入网页端口、总管理员账号、总管理员密码、`SECRET_KEY`
- 你通常只需要在这里继续补充或修改 `SUBTENSOR_WS_URL` 等运行配置；每个监控菜单自己的 Telegram 参数建议登录网页后填写

至少填写这些值：

```env
SUBTENSOR_WS_URL=wss://entrypoint-finney.opentensor.ai:443
LARGE_TRANSFER_THRESHOLD_TAO=5
ADMIN_USERNAME=你的网页登录总管理员账号
ADMIN_PASSWORD=你的网页登录总管理员密码
CLEANUP_TIME=04:00
CLEANUP_RETENTION_DAYS=1
CLEANUP_RETENTION_HOURS=1
CLEANUP_INTERVAL_MINUTES=10
TAOSTATS_ENABLED=false
TAOSTATS_API_KEY=
TAOSTATS_AMOUNT_MODE=fallback
```

如果你申请了 TaoStats API Key，可以改成：

```env
TAOSTATS_ENABLED=true
TAOSTATS_API_KEY=你的 TaoStats API Key
TAOSTATS_API_KEYS=备用key1,备用key2
TAOSTATS_AMOUNT_MODE=only
TAOSTATS_REQUEST_INTERVAL_SECONDS=2
TAOSTATS_RATE_LIMIT_COOLDOWN_SECONDS=60
TAOSTATS_RETRY_COOLDOWN_SECONDS=120
```

`TAOSTATS_AMOUNT_MODE=only` 表示减仓金额只看 TaoStats，适合测试免费额度和数据准确性；如果 TaoStats 未返回，页面会明确显示“等待 TaoStats”或“TaoStats未返回”，不会再混入子网价格或限价估算。
免费额度容易触发 `429 Too Many Requests` 时，可以把 `TAOSTATS_REQUEST_INTERVAL_SECONDS` 和 `TAOSTATS_RETRY_COOLDOWN_SECONDS` 调大。
如果你有多个合法 TaoStats API Key，可以把备用 key 放到 `TAOSTATS_API_KEYS`，某个 key 触发 429 时会自动切换下一个 key，并让触发 429 的 key 冷却一段时间。

历史命中自动清理默认开启，每 10 分钟删除 1 小时前的 `chain_events` 命中记录；只清理历史命中，不会删除钱包、菜单、账号、TG 或系统设置。页面默认展示 50 条，并尽量覆盖最近 1 小时内的数据。

5. 改完配置后重启服务：

```bash
sudo systemctl restart tao-monitor.service
```

说明：

- `./scripts/deploy.sh` 首次部署时已经自动执行过 `systemctl enable tao-monitor.service`
- 所以后续只是修改 `.env` 或更新配置时，通常只需要执行 `restart`
- 如果你想手动确认开机自启状态，可以执行：

```bash
sudo systemctl enable tao-monitor.service
```

## 首次安装时可自定义

运行 `./scripts/deploy.sh` 时，会交互式询问：

- 网页端口
- 总管理员账号
- 总管理员密码
- `SECRET_KEY`

如果直接回车，就会使用默认值。

## 更新与数据保留

- 应用数据放在本机 PostgreSQL 数据库 `tao_monitor`
- 备份放在 `/opt/tao-monitor/backups/`
- `scripts/update.sh` 在 `git pull` 前会先调用备份脚本，并在更新依赖后执行 `alembic upgrade head`
- `data/`、`logs/`、`backups/` 默认都不纳入 Git 管理，所以更新不会覆盖资料
- 如果你的实际部署目录不是 `/opt/tao-monitor`，下面更新命令里的路径也要一起替换

如果旧服务器仍在使用 SQLite，请先确认备份正常，再用 `scripts/migrate_sqlite_to_postgres.py` 把旧数据迁移到 PostgreSQL，最后再切换 `.env` 里的 `DATABASE_URL`。

更新命令：

```bash
cd /opt/tao-monitor
./scripts/update.sh
```

## 资料备份

### 网页备份

- 登录后台后，进入“钱包备份”区域。
- 点击“下载当前账号的钱包备份”。
- 浏览器会直接下载一个 JSON 资料包。
- 网页备份只包含当前账号当前监控菜单的钱包地址、备注和开关状态，不包含 `.env`、系统密钥、Telegram 凭据或其他账号资料。

### 命令行备份

```bash
cd /opt/tao-monitor
./scripts/backup.sh
```

默认会备份这些资料：

- PostgreSQL 数据库 dump：`tao_monitor.dump`
- `.env`
- `README.md`

并打包成：

```text
backups/tao-monitor-backup-时间戳.zip
```

## GitHub 工作流

```bash
git init
git add .
git commit -m "feat: bootstrap tao monitor"
git branch -M main
git remote add origin https://github.com/mmk58860-code/taotaoi.git
git push -u origin main
```

## Telegram 说明

- 先在 Telegram 里创建机器人并拿到 Bot Token。
- 把机器人拉进你要接收消息的聊天里。
- 总管理员首次部署时可用 `.env` 里的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 作为自己的默认值。
- 普通账号登录后台后，应在“我的通知设置”里填写自己独立的 Bot Token 和 Chat ID。
- 配置保存后，下一轮扫描就会自动使用。

## 账号说明

- 网页后台登录账号来自 `ADMIN_USERNAME`。
- 网页后台登录密码来自 `ADMIN_PASSWORD`。
- 首次部署创建的是总管理员账号。
- 总管理员登录后台后，可以继续创建普通后台账号给朋友使用。
- 每个账号都有自己独立的钱包列表、事件记录、Telegram 推送配置和大额阈值。
- 只有总管理员可以修改系统链路设置、创建/删除账号，并查看系统里保存的可回显账号密码。

## 监听逻辑说明

- 当前版本会读取每个 finalized 区块的全部 extrinsic 与关联 events。
- 会递归展开 `Utility.batch`、`Proxy.proxy`、`Multisig.as_multi`、`Sudo.sudo`、`Scheduler.schedule` 等包装调用。
- 会把动作统一分类为转账、质押、委托、子网、权重、代理、多签、EVM、Shield 等类型。
- 钱包命中按“签名者 + 调用参数里的关联地址 + 递归扫描出的地址”综合判断。
- 金额估值默认按 `1 TAO = 1,000,000,000 Rao` 换算，用于大额阈值筛选。
- 服务按 finalized 区块向前扫描，并保存 `last_scanned_block`，避免重复推送。

## 注意

- 如果 TAO 链的事件结构或 WebSocket 入口变动，需要同步调整解析逻辑。
- 首次连接链节点前，建议先用默认 `finney` 入口验证服务联通性。
- 依赖里已包含 `itsdangerous` 和 `python-multipart`，避免登录和表单页面启动失败。
- 这个版本已经适合放到 GitHub 持续迭代，后续可以继续扩展登录权限、图表、更多事件类型和多渠道通知。
