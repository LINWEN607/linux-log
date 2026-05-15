# Linux Message Bot

监控 Linux 系统日志（syslog 文件），推送多平台通知。

## 功能特性

- **数据源**：挂载日志文件，通过尾随方式读取，不依赖 journald
- **多平台通知**：企业微信、钉钉、飞书、Bark、PushPlus、魔法推送、SMTP 邮件
- **Web 配置页**：内置配置 UI（默认端口 `18080`），支持 Webhook、事件类型、勿扰、标题前缀等；保存后热加载，一般无需重启
- **Web 访问控制**：可在页面中设置访问密码（`config.json` 内保存盐值与哈希）；支持关闭密码校验（`web_password_enabled`）
- **智能去重**：时间窗口去重（默认 300 秒）
- **磁盘事件合并**：同类型磁盘唤醒/休眠在时间窗口内合并推送
- **HTTP 连接池**：统一 HTTP 管理与重试
- **勿扰模式**：可设置时段内不推送，结束后汇总为一条消息
- **通知失败自愈**：连续失败达到阈值时可触发进程内重启通知链路（可通过环境变量关闭或调参）
- **Docker 部署**：支持 Docker Compose 一键部署，带健康检查
- **配置热加载**：通过 Web 保存 `config.json` 后自动生效；环境变量仍优先生效且不会被文件覆盖
- **Systemd 服务监控**：监控服务启动、停止、失败事件（通过 syslog 正则匹配）
- **系统巡检**：定时采集 CPU/内存/磁盘，周期内取平均值超过阈值则推送告警；磁盘直接检查使用率
- **Docker 容器事件**：监控容器创建、启动、停止、销毁、OOM 等（挂载 docker.sock）
- **自定义规则**：支持独立指定日志文件路径 + 正则表达式，推送自定义事件

### 支持的事件类型

以下为 `monitor_events` 中允许配置的 `eventId`：

- **登录与认证**：`LoginSucc`, `LoginFail`, `Logout`
- **SSH**：`SSH_INVALID_USER`, `SSH_AUTH_FAILED`, `SSH_LOGIN_SUCCESS`, `SSH_DISCONNECTED`
- **磁盘与存储**：`FoundDisk`, `InsertDisk`, `EjectDisk`, `StorageBroken`, `STORAGE_DEGRADED`, `DiskWakeup`, `DiskSpindown`, `DISK_IO_ERR`
- **Systemd 服务**：`SYSTEMD_SERVICE_STARTED`, `SYSTEMD_SERVICE_STOPPED`, `SYSTEMD_SERVICE_RESTARTED`, `SYSTEMD_SERVICE_FAILED`
- **Docker 容器**：`DOCKER_CONTAINER_CREATE`, `DOCKER_CONTAINER_START`, `DOCKER_CONTAINER_STOP`, `DOCKER_CONTAINER_DIE`, `DOCKER_CONTAINER_OOM`, `DOCKER_CONTAINER_KILL`, `DOCKER_CONTAINER_PAUSE`, `DOCKER_CONTAINER_UNPAUSE`, `DOCKER_CONTAINER_RESTART`, `DOCKER_CONTAINER_DESTROY`
- **自定义**：`SYSLOG_PATTERN_MATCH`

## 日志存储

- **触发推送的原始数据**写入 `./data/logs`（可配置 `log_dir`），按事件类型与日期分文件；保留天数由 `log_retention_days` 控制。
- **运行日志**：`./data/logs/monitor_YYYYMMDD.log`；应用运行日志保留天数由 `max_log_age` 控制。
- **游标**：`./data/cursor/` 目录存放各轮询器游标文件，记录已处理位置。

### 管理工具

```bash
# 需在项目根目录，或设置 PYTHONPATH
python tools/log_manager.py stats
python tools/log_manager.py recent --hours 24
python tools/log_manager.py type LoginSucc --limit 10
python tools/log_manager.py export ./logs.json --event-type SSH_AUTH_FAILED
python tools/log_manager.py cleanup 30
```

## 使用方法

### 1. 配置通知渠道

至少配置一个推送渠道；环境变量与 Web 配置页二选一即可（亦可混用）。支持企业微信、钉钉、飞书、Bark、魔法推送、SMTP 邮件、PushPlus。

未配置任何 Webhook / PushPlus 时进程仍可启动，仅提供 Web 配置页；配置完成后自动开始监控与推送。

### 2. 主要配置项（`config/config.json`）

| 项 | 说明 |
| --- | --- |
| `wechat_webhook_url` / `dingtalk_webhook_url` / `feishu_webhook_url` / `bark_url` | 各平台 Webhook 或 Bark URL |
| `pushplus_params` | PushPlus JSON（可多个，`\|` 分隔） |
| `data_source` | 数据源模式：`syslog-tail`（推荐）、`journald`、`both` |
| `syslog_paths` | 监控的日志文件路径列表 |
| `custom_patterns` | 自定义正则规则列表，每条可独立指定 `file_path` + `pattern` |
| `title_prefix` | 推送标题前缀，留空则使用默认「Linux」 |
| `monitor_events` | 要监控的事件 ID 列表 |
| `log_level` | 日志级别 |
| `log_dir` / `cursor_dir` | 日志与游标目录 |
| `logger_poll_interval` | 轮询间隔（秒） |
| `http_pool_size` / `http_retry_count` / `http_timeout` | HTTP 客户端参数 |
| `dedup_window` | 去重时间窗口（秒） |
| `log_retention_days` | 原始推送日志保留天数 |
| `max_log_age` | 应用运行日志 `monitor_*.log` 保留天数 |
| `dnd_enabled` / `dnd_start_time` / `dnd_end_time` | 勿扰开关与时段（HH:MM，可跨日） |
| `system_patrol_enabled` | 系统巡检告警开关 |
| `patrol_cpu_threshold` / `patrol_cpu_interval` / `patrol_cpu_period` | CPU 告警阈值/采样间隔/报告周期 |
| `patrol_mem_threshold` / `patrol_mem_interval` / `patrol_mem_period` | 内存告警阈值/采样间隔/报告周期 |
| `patrol_disk_threshold` | 磁盘使用率告警阈值 |
| `web_password_enabled` | 是否要求密码才能访问配置页（默认 true） |
| `poll_batch_summary_enabled` | 轮询汇总模式（同一轮内多事件合并推送） |

首次在 Web 中设置密码后，会在同文件写入 `web_password_salt`、`web_password_hash`（请勿手工泄露）。

### 3. 常用环境变量

除上述 Webhook 外，还可通过环境变量覆盖（**已设置的环境变量不会被 `config.json` 覆盖**）：

- `DATA_SOURCE`：数据源模式（`syslog-tail` / `journald` / `both`，默认 `journald`）
- `MONITOR_EVENTS`（逗号分隔）
- `LOGGER_POLL_INTERVAL`、`LOG_LEVEL`、`HTTP_POOL_SIZE`、`HTTP_RETRY_COUNT`、`HTTP_TIMEOUT`、`DEDUP_WINDOW`
- `LOG_RETENTION_DAYS`、`MAX_LOG_AGE`
- `UI_PORT`：Web 端口（默认 `18080`）
- `NOTIFY_RESTART_ENABLED`、`NOTIFY_RESTART_CONSECUTIVE`、`NOTIFY_RESTART_WINDOW`、`NOTIFY_RESTART_COOLDOWN`：通知链路失败重启策略
- `APP_HOME`：自定义应用根目录（影响 `config.json` 解析路径，一般 Docker 内为 `/app`）

### 4. 启动

#### 使用 Docker Compose（推荐）

```yaml
services:
  linux-message-bot:
    build: .
    container_name: linux-message-bot
    restart: unless-stopped
    ports:
      - 18080:18080
    volumes:
      - ./data/logs:/app/data/logs:rw
      - ./data/cursor:/app/data/cursor:rw
      - ./config:/app/config:rw
      # syslog 文件（必需，data_source 设为 syslog-tail 时）
      - /var/log/syslog:/var/log/syslog:ro
      - /var/log/auth.log:/var/log/auth.log:ro
      - /var/log/kern.log:/var/log/kern.log:ro
      # Docker 容器事件（可选）
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - TZ=Asia/Shanghai
      - DATA_SOURCE=syslog-tail
```

浏览器访问 `http://<本机IP>:18080` 打开 Web 配置页。

#### 本地运行（无 Docker）

**依赖**：Python 3.9+，以及 `psutil`（系统巡检）。

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 运行（至少配置一个推送渠道）
PYTHONPATH=. WECHAT_WEBHOOK_URL=xxx python3 src/main.py
```

## 项目结构

```
├── src/
│   ├── monitor/              # 数据源轮询、事件处理、模型
│   │   ├── syslog_tail_poller.py   # Syslog 文件尾随轮询器（主数据源）
│   │   ├── journald_poller.py      # Journald 日志轮询器（备选数据源）
│   │   ├── docker_events_poller.py # Docker 容器事件监听
│   │   ├── system_patrol.py        # 系统巡检（阈值告警）
│   │   ├── event_processor.py      # 事件处理与通知生成
│   │   └── models.py               # 数据模型
│   ├── notifier/             # 多平台通知、连接池
│   ├── utils/                # 日志、存储、推送统计
│   ├── web/                  # Web 配置 UI（Flask）
│   ├── config.py
│   └── main.py
├── config/config.json        # 配置文件（可挂载，Web 可写）
├── data/logs                 # 运行日志与推送存储
├── data/cursor               # 轮询游标等
├── tools/log_manager.py
├── Dockerfile
├── docker-compose.yml
├── healthcheck.sh            # 容器健康检查
└── pyproject.toml
```

## 故障排除

- **收不到通知**：检查 Webhook / PushPlus、网络与 `docker compose logs`。
- **无事件**：确认日志文件已挂载且路径正确；检查 `docker compose logs` 中 poller 是否正常启动。
- **时间不对**：调整 `TZ` 环境变量。
- **重复通知**：调大 `dedup_window`（秒）。
- **Web 配置页打不开**：确认端口映射与防火墙；容器内可设 `UI_PORT` 并与 `ports` 一致。
- **忘记 Web 密码**：编辑 `config.json`，删除 `web_password_salt` 与 `web_password_hash` 后重启，再在页面重新设置密码（或暂时将 `web_password_enabled` 设为 `false`）。
- **Docker 容器事件不工作**：确认已挂载 `/var/run/docker.sock`。

