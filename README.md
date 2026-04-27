# TAO Monitor

一个运行在 Ubuntu 上的 TAO 生态监控项目，带网页管理后台、Telegram 推送、钱包地址监控、大额转账阈值监控，以及 systemd 开机自启能力。

## 功能

- 监控你导入的钱包地址，并支持备注别名。
- 扫描 TAO 链 finalized 区块中的全部 extrinsic，并自动分类为转账、质押、委托、子网、权重、代理、多签等动作。
- 当交易涉及监控钱包时，立即记录并推送到当前账号自己的 Telegram。
- 当交易金额大于或等于当前账号设置的阈值时，立即记录并推送到当前账号自己的 Telegram。
- 通过网页管理链节点、个人 Telegram 参数、个人阈值和钱包列表。
- 使用 SQLite 保存配置、钱包、事件和扫描进度，更新部署时不会丢数据。
- 提供后台账号体系：总管理员可创建普通账号给朋友使用，普通账号之间的钱包和事件互相隔离。
- 提供网页一键导出当前账号钱包清单，以及命令行整站备份脚本，避免云服务器删档或商家异常导致资料丢失。

## 技术结构

- `FastAPI`：网页管理台和接口服务。
- `SQLite + SQLAlchemy`：配置、钱包、事件、账号、用户通知设置、扫描状态持久化。
- `substrate-interface`：连接 Subtensor WebSocket，逐块解码 extrinsic 和关联事件。
- `httpx`：调用 Telegram Bot API。
- `systemd`：Ubuntu 开机自启与自动拉起。

## 目录

- `app/`：后端服务、页面模板、静态资源。
- `deploy/systemd/tao-monitor.service`：systemd 服务定义。
- `scripts/deploy.sh`：首次部署脚本。
- `scripts/update.sh`：更新脚本。
- `scripts/backup.sh`：整站资料备份脚本。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
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
sudo apt install -y python3 python3-venv python3-pip git rsync zip
```

如果你是直接用 `root` 登录服务器，没有 `sudo` 也可以，命令里的 `sudo` 去掉即可。

3. 克隆项目并部署：

```bash
git clone https://github.com/mmk58860-code/taotaoi.git /opt/tao-monitor
cd /opt/tao-monitor
chmod +x scripts/*.sh
./scripts/deploy.sh
```

4. 编辑生产配置：

```bash
sudo nano /opt/tao-monitor/.env
```

至少填写这些值：

```env
SUBTENSOR_WS_URL=wss://entrypoint-finney.opentensor.ai:443
LARGE_TRANSFER_THRESHOLD_TAO=5
TELEGRAM_BOT_TOKEN=你的机器人 token
TELEGRAM_CHAT_ID=你的 chat id
ADMIN_USERNAME=你的网页登录总管理员账号
ADMIN_PASSWORD=你的网页登录总管理员密码
```

5. 重启服务：

```bash
sudo systemctl restart tao-monitor.service
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

- 应用数据放在 `/opt/tao-monitor/data/tao_monitor.db`
- 备份放在 `/opt/tao-monitor/backups/`
- `scripts/update.sh` 在 `git pull` 前会先调用备份脚本
- `data/`、`logs/`、`backups/` 默认都不纳入 Git 管理，所以更新不会覆盖资料

更新命令：

```bash
cd /opt/tao-monitor
./scripts/update.sh
```

## 资料备份

### 网页备份

- 登录后台后，进入“钱包备份”区域。
- 点击“下载当前账号的钱包备份”。
- 浏览器会直接下载一个 CSV 文件。
- 网页备份只包含当前账号的钱包地址、备注、开关状态和添加时间，不包含 `.env`、系统密钥、Telegram 凭据或其他账号资料。

### 命令行备份

```bash
cd /opt/tao-monitor
./scripts/backup.sh
```

默认会备份这些资料：

- `data/tao_monitor.db`
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
