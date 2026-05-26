# oppo-webserver — 热词爬取定时任务

定时爬取**抖音热词**和**搜狗细胞词库**，生成结构化文件，自动同步到 SFTP 服务器供第三方拉取。

```
┌─────────────────────┐      rsync/cron       ┌─────────────────────┐
│  机器 A（爬虫服务器） │ ──────────────────────▶ │  机器 B（SFTP 服务器） │ ◀── 第三方拉取
│  /opt/oppo-webserver │                        │  /data/hotword/     │
│  output/             │                        │                     │
└─────────────────────┘                        └─────────────────────┘
```

## 目录结构

```
oppo-webserver/
├── config.py              # 统一配置（读取 .env.dev）
├── logger.py              # 日志
├── requirements.txt       # Python 依赖
├── install.sh             # 一键部署脚本
├── sync_to_sftp.sh        # SFTP 同步脚本（cron 调用）
├── auth_state.json        # 🔑 抖音登录态（需手动放置）
├── id_ed25519             # 🔑 SFTP 密钥（需手动放置）
├── .env.dev               # 环境配置（需手动创建）
├── crawler/
│   ├── douyin/            # 抖音热词爬虫
│   │   ├── spider.py      #   核心爬取 + Playwright 会话管理
│   │   └── run.py         #   入口
│   └── sogou/             # 搜狗词库爬虫
│       ├── dict_manager.py#   .scel 下载/解析/导出
│       └── run.py         #   入口
├── pipeline/
│   ├── scheduler.py       # APScheduler 定时调度 + Flask API
│   ├── alerts.py          # 钉钉报警（暂未启用）
│   └── store.py           # 输出文件索引
└── output/                # 爬取输出（自动生成）
    ├── douyin/{YYYYMMDD}/
    └── sogou/{YYYYMMDD}/
```

## 部署步骤

### 1. 准备 `auth_state.json`（抖音登录态）

在**本地有浏览器的机器**上执行扫码登录：

```bash
cd oppo-webserver
pip3 install -r requirements.txt
python3 -m playwright install chromium

python3 -c "
from crawler.douyin.spider import DouyinAuthenticator
auth = DouyinAuthenticator()
auth.login_and_save_state()
"
```

登录成功后项目根目录会生成 `auth_state.json`。

> ⚠️ 此文件含登录 Cookie，**勿提交 Git**。有效期约 1-2 周，过期重新扫码即可。

### 2. 准备 `id_ed25519`（SFTP 密钥）

确保你有连接 SFTP 服务器的私钥文件 `id_ed25519`。可先手动验证连接：

```bash
sftp -i ./id_ed25519 oppo01@82.156.76.22
```

### 3. 上传到服务器

```bash
# 上传代码
rsync -avz --exclude='.git/' --exclude='__pycache__/' \
  --exclude='output/' --exclude='venv/' --exclude='*.scel' \
  -e "ssh -i 你登录服务器的密钥" \
  ./ 用户@爬虫服务器IP:/opt/oppo-webserver/

# 上传两个关键文件
scp -i 你登录服务器的密钥 auth_state.json 用户@爬虫服务器IP:/opt/oppo-webserver/
scp -i 你登录服务器的密钥 id_ed25519     用户@爬虫服务器IP:/opt/oppo-webserver/
```

### 4. 执行安装

```bash
ssh -i 你登录服务器的密钥 用户@爬虫服务器IP
cd /opt/oppo-webserver
bash install.sh
```

`install.sh` 会自动完成以下全部步骤：

| 步骤 | 内容 |
|------|------|
| 1 | 检查 Python 版本 |
| 2 | 创建项目目录 |
| 3 | 检查 `auth_state.json` 和 `id_ed25519` |
| 4 | 创建虚拟环境，安装 Python 依赖 |
| 5 | 安装 Playwright Chromium |
| 6 | 生成 `.env.dev` 配置（如不存在） |
| 7 | 创建输出目录和软链接 |
| 8 | **配置 SFTP 同步 cron 定时任务** |
| 9 | 创建 systemd 服务（开机自启） |

### 5. 启动

```bash
sudo systemctl start oppo-webserver
```

安装完成后，爬取和同步全自动运行，无需手动干预。

## 手动执行

```bash
cd /opt/oppo-webserver
source venv/bin/activate

# 搜狗词库
python -m crawler.sogou.run

# 抖音热词（默认 10-15 页）
python -m crawler.douyin.run

# 抖音热词（指定页数）
python -m crawler.douyin.run --max-pages 3

# 手动触发 SFTP 同步
bash sync_to_sftp.sh
```

## 定时任务时间表

### 爬取（systemd 管理）

| 任务     | 时间                       |
|----------|---------------------------|
| 搜狗词库 | 每天 05:10                |
| 抖音热词 | 每天 07:10 / 15:10 / 23:10 |

### SFTP 同步（cron 管理）

爬取完成后 20 分钟自动推送到 SFTP 服务器：

| 数据     | 同步时间                     |
|----------|------------------------------|
| 搜狗词库 | 每天 05:30                   |
| 抖音热词 | 每天 07:30 / 15:30 / 23:30   |

### 第三方建议拉取时间

| 数据     | 建议拉取时间                 |
|----------|------------------------------|
| 搜狗词库 | 06:10                        |
| 抖音热词 | 08:10 / 16:10 / 01:10        |

## 输出格式

### 搜狗词库

路径: `output/sogou_newwords/{YYYYMMDD}/sogou_{YYYYMMDD}_{HHMM}.txt`

Tab 分隔，每行一条：

```
阿策	ace
爱哎唉变装	aiaiaibianzhuang
一颗赛艇	yikesaiting
```

### 抖音热词

- 处理结果: `output/douyin_hotwords/{YYYYMMDD}/douyin_hotwords_{YYYYMMDD}_{HHMM}.json`

```json
[
  {"词": "天气", "拼音": "tian qi", "热度": 999}
]
```

## 数据过滤规则

词条仅保留由 **中文、a-z、A-Z** 组成的内容，过滤数字、特殊符号、emoji 等。

## 配置说明（.env.dev）

```bash
# ── 搜狗词库 URL（从 https://pinyin.sogou.com/dict/ 获取）
SOGOU_DICT_URL=https://pinyin.sogou.com/d/dict/download_cell.php?id=4&...
SOGOU_DICT_ID=4

# ── SFTP 同步
SFTP_HOST=82.156.76.22       # SFTP 服务器地址
SFTP_USER=oppo01             # SFTP 用户名
SFTP_KEY_PATH=id_ed25519     # 密钥文件（相对项目根目录）
SFTP_REMOTE_DIR=/data/hotword # SFTP 服务器上的目标目录
```

## 运维命令

```bash
# 服务管理
sudo systemctl start oppo-webserver     # 启动
sudo systemctl stop oppo-webserver      # 停止
sudo systemctl restart oppo-webserver   # 重启
sudo systemctl status oppo-webserver    # 状态

# 日志
sudo journalctl -u oppo-webserver -f    # 爬虫日志
tail -f /opt/oppo-webserver/sync.log    # 同步日志

# cron 管理
crontab -l                              # 查看定时任务
crontab -e                              # 编辑定时任务
```

## 常见问题

### Cookie 过期

在有图形界面的机器上重新扫码登录，把新的 `auth_state.json` 拷贝到服务器覆盖。服务无需重启。

### Playwright 安装太慢

```bash
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
  python -m playwright install chromium
```

如果镜像也失败，从本地拷贝浏览器缓存：

```bash
# 本地
tar -czf pw.tar.gz ~/.cache/ms-playwright/
scp pw.tar.gz 用户@服务器:~/

# 服务器
tar -xzf ~/pw.tar.gz -C ~/
```

### Playwright 完全无法使用

爬虫支持降级模式：Playwright 启动失败时自动从 `auth_state.json` 直接加载 Cookie，基本爬取不受影响。

### SFTP 同步失败

```bash
# 检查密钥权限
ls -la /opt/oppo-webserver/id_ed25519  # 应为 600

# 手动测试连接
ssh -i /opt/oppo-webserver/id_ed25519 oppo01@82.156.76.22 "ls /data/hotword/"

# 手动同步
bash /opt/oppo-webserver/sync_to_sftp.sh

# 查看同步日志
tail -20 /opt/oppo-webserver/sync.log
```
