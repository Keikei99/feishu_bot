# Ubuntu 单机部署：Miniconda、nohup 与滚动文件日志

本文适用于 Ubuntu 22.04 / 24.04，使用普通 SSH 用户部署到 `$HOME/feishu-summary`。机器人通过飞书长连接接收事件，需要能够访问飞书及配置的模型服务，无需为机器人开放公网 HTTP 回调端口。

Miniconda 用来安装和管理本项目的 Python 环境，服务器无需另外安装系统级 Python、venv 或 pip。本流程通过 Conda 安装 Python 3.12，环境目录固定为项目内的 `.venv`。这个目录名只是为了兼容启动脚本，实际环境由 Conda 创建和管理。

运行方式为一个 Python 进程，日志由 Python 按文件大小轮转；`scripts/botctl.sh` 负责后台启动、查询和正常停止。`nohup` 不提供崩溃自动重启或服务器重启后的自动启动，这两项仍需人工处理。

## 1. 准备服务器

登录服务器，在服务器终端执行：

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates rsync util-linux procps nano
mkdir -p "$HOME/feishu-summary"
```

安装系统软件时使用 `sudo`，后续安装 Miniconda、依赖及运行机器人均使用当前普通用户。

接着安装 Miniconda。下面的命令会根据服务器架构选择官方 Linux 安装器；已有可用安装时只显示版本，不覆盖现有目录：

```bash
(
  set -e
  if [ -x "$HOME/miniconda3/bin/conda" ]; then
    "$HOME/miniconda3/bin/conda" --version
  elif [ -e "$HOME/miniconda3" ]; then
    printf 'miniconda3 目录已存在，但未找到可用 conda；请先检查现有安装。\n' >&2
    exit 1
  else
    case "$(uname -m)" in
      x86_64) MINICONDA_ARCH=x86_64 ;;
      aarch64|arm64) MINICONDA_ARCH=aarch64 ;;
      *) printf '该架构不在本文安装流程范围内。\n' >&2; exit 1 ;;
    esac
    curl -fL \
      "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${MINICONDA_ARCH}.sh" \
      -o "$HOME/miniconda-installer.sh"
    bash "$HOME/miniconda-installer.sh"
  fi
)
```

安装器采用交互方式：按提示阅读并确认条款，保留默认安装目录（例如 `/home/ubuntu/miniconda3`，对应 `$HOME/miniconda3`）。是否执行 `conda init` 不影响本文流程，后续直接调用 Conda 的绝对路径。安装后确认：

```bash
"$HOME/miniconda3/bin/conda" --version
```

## 2. 上传代码

在本地电脑进入本项目目录，把下面的 SSH 地址改为自己的服务器：

```bash
cd /Users/keikei/Codes/feishu_bot/historical-conversation-summary
DEPLOY_HOST='ubuntu@你的服务器IP'
ssh "$DEPLOY_HOST" 'mkdir -p "$HOME/feishu-summary"'
rsync -av \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='run/' \
  ./ "$DEPLOY_HOST:feishu-summary/"
```

上传命令保留服务器已有的配置、数据库、日志和运行文件，也不会删除服务器上的其他文件。本地 macOS 环境不能直接复制到 Linux；Conda 环境同样需要在服务器上创建。

## 3. 创建 Conda 环境并安装依赖

回到服务器终端：

```bash
(
  set -e
  cd "$HOME/feishu-summary"
  if [ -d .venv/conda-meta ]; then
    .venv/bin/python --version
  elif [ -e .venv ]; then
    printf '.venv 已存在且不是 Conda 环境；请先按下文迁移旧环境。\n' >&2
    exit 1
  else
    "$HOME/miniconda3/bin/conda" create \
      --prefix "$HOME/feishu-summary/.venv" python=3.12 pip
  fi
  .venv/bin/python -m pip install -r requirements.txt
)
```

创建环境时，按终端提示完成确认。新环境使用 Python 3.12；复用已有 Conda 环境时，确认显示的 Python 版本至少为 3.10。

后续 `.venv/bin/python` 指向 **Conda 环境中的 Python**。本项目的启动脚本直接调用该绝对路径，不依赖当前 SSH 会话是否执行过 `conda activate`，因此无需保持激活状态，也不需要改动现有的 PID 和运行锁管理。

如果已经按旧流程创建过普通 venv，先停止旧实例，再把原环境重命名备份；不要把 Conda 环境覆盖到旧 venv 中：

```bash
(
  set -e
  cd "$HOME/feishu-summary"
  bash scripts/botctl.sh stop
  if [ -e .venv ] && [ ! -d .venv/conda-meta ]; then
    mv .venv ".venv.backup.$(date +%Y%m%d-%H%M%S)"
  fi
)
```

确认旧实例已经退出后，重新执行本节的创建和安装命令。通过其他命令启动的旧实例，需要先按原方式停止。备份目录保留以便回退，不删除原环境或运行数据。

## 4. 配置机器人和日志

仅在服务器没有配置文件时复制示例，然后编辑：

```bash
cd "$HOME/feishu-summary"
umask 077
if [ ! -f .env ]; then
  cp .env.example .env
fi
chmod 600 .env
nano .env
```

填写以下必填项：

```dotenv
FEISHU_APP_ID=你的飞书应用ID
FEISHU_APP_SECRET=你的飞书应用密钥
LLM_BASE_URL=你的Anthropic兼容模型服务地址
LLM_API_KEY=你的模型服务密钥
LLM_MODEL=服务实际支持的模型名称
```

确认日志设置如下。已有同名项时直接修改原值，避免重复添加：

```dotenv
LOG_LEVEL=INFO
LOG_FILE=logs/bot.log
LOG_MAX_BYTES=20971520
LOG_BACKUP_COUNT=4
LOG_CONSOLE=false
STATE_DB_PATH=data/bot.sqlite3
```

相对日志路径和数据库路径均以项目目录为基准，与执行命令时所在的目录无关。默认日志包括当前文件及最多四个备份：

```text
logs/bot.log
logs/bot.log.1
logs/bot.log.2
logs/bot.log.3
logs/bot.log.4
```

每个文件按 20 MiB 轮转，常规总量约 100 MiB；`.1` 为最近的备份。该方案按容量保留日志，不保证保留固定天数。生产使用 `LOG_CONSOLE=false`，避免再向另一个无限增长的 `nohup.out` 重复输出。

飞书后台的机器人能力、事件订阅、权限及应用发布请按 [README 的飞书配置说明](README.md#飞书应用配置) 完成。

## 5. 启动前检查

在服务器执行：

```bash
cd "$HOME/feishu-summary"
.venv/bin/python main.py --check-config
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python main.py --check-logging
```

`--check-config` 只验证配置格式，不连接飞书或模型，也不创建数据库或日志文件；`--check-logging` 会实际创建并打开滚动日志，验证日志目录可写，同样不访问网络或数据库。两者均不能判断凭据和权限是否有效。测试使用虚构凭据和临时数据库，不读取真实 `.env`。

## 6. 后台启动与确认

```bash
cd "$HOME/feishu-summary"
bash scripts/botctl.sh start
bash scripts/botctl.sh status
tail -n 100 logs/bot.log
```

启动脚本先执行日志预检，再使用 `nohup` 后台运行，管理 `run/bot.lock` 和 `run/bot.pid`。后台标准输出和标准错误重定向到 `/dev/null`，业务和 SDK 日志由统一处理器写入滚动文件，因此不会生成 `nohup.out`。程序入口的未预期异常也通过日志处理器记录安全诊断信息；普通 `print()` 输出不会保存为日志。请持续使用该脚本管理机器人，保持同一数据库只运行一个实例。

如果服务器上已有通过其他命令启动的旧实例，先按原方式停止它，再首次使用该脚本。脚本只管理自身启动并记录的进程。

进程存活和启动日志不能证明飞书已连接或模型可用。启动后在测试群依次验证：

1. 普通群消息不触发机器人。
2. `@机器人 帮助` 能收到功能菜单。
3. 引用普通消息发送 `@机器人 总结`，能收到操作提示。
4. 引用合并转发记录发送 `@机器人 总结`，先收到处理回执，再收到总结。

确认完成后可以断开 SSH。重新登录服务器时，用 `status` 和日志确认状态。

## 7. 查询日志与正常停止

实时查看日志：

```bash
cd "$HOME/feishu-summary"
tail -F logs/bot.log
```

按级别或消息 ID 查询，包括轮转文件：

```bash
rg 'WARNING|ERROR' logs/bot.log*
rg -F '待查询的消息ID' logs/bot.log*
```

如果服务器未安装 `rg`，可用 `grep -E 'WARNING|ERROR' logs/bot.log*` 或 `grep -F '待查询的消息ID' logs/bot.log*`。

正常停止和重启：

```bash
bash scripts/botctl.sh stop
bash scripts/botctl.sh status
bash scripts/botctl.sh start
```

`stop` 发送 SIGTERM，让程序停止领取新任务并等待在途请求结束，最多等待 300 秒；超时不会强制杀死进程，也不会启动新实例。收到停止超时提示时，先检查日志和进程状态，确认退出后再启动；不要在旧进程尚未退出时启动第二份。

## 8. 更新代码

先在服务器停止机器人并确认已退出：

```bash
cd "$HOME/feishu-summary"
bash scripts/botctl.sh stop
bash scripts/botctl.sh status
```

然后在本地重复第 2 步的上传命令，保留服务器现有 `.env`、`data/`、`logs/` 和 `run/`。回到服务器安装依赖、检查并启动：

```bash
cd "$HOME/feishu-summary"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --check-config
.venv/bin/python -m unittest discover -s tests -q
bash scripts/botctl.sh start
bash scripts/botctl.sh status
tail -n 100 logs/bot.log
```

更新代码时保留原数据库，程序会自动执行兼容的表结构升级。更换配置或依赖后均需要重启服务。

## 运行边界

- 服务器重启后需要重新执行 `bash scripts/botctl.sh start`；进程意外退出也需要人工启动。
- 日志轮转不等于数据库备份。数据库及备份管理请单独安排，更新时不要删除 `data/`。
- 文件日志与 SDK 共用统一日志出口，业务默认 INFO，飞书 SDK 和 HTTP 库保持 WARNING。
- 聊天正文、总结、姓名、密钥和原始 HTTP 响应不作为业务日志内容记录。日志文件仍应只向维护人员开放。

参考：[Miniconda 官方安装说明](https://www.anaconda.com/docs/getting-started/miniconda/install)、[Conda 按路径创建环境](https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html#specifying-a-location-for-an-environment)、[Python 滚动文件日志](https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler)、[GNU nohup 说明](https://www.gnu.org/s/coreutils/manual/html_node/nohup-invocation.html)。
