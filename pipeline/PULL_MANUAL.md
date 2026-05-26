# OPPO 热词数据拉取 — 操作手册

## 文件清单

| 文件 | 说明 |
|------|------|
| `pull_from_sftp.sh` | 拉取脚本 |
| `id_ed25519` | SFTP 私钥 |

将以上两个文件放在**同一目录**下即可使用。

---

## 一键部署（第一次直接用这个）

```bash
bash pull_from_sftp.sh --install
```

执行后会依次完成：
1. 验证 SFTP 连接是否通
2. 创建本地数据目录 `./hotword_data/`
3. 首次全量拉取所有数据
4. 配置 cron 定时拉取（每天 4 次）

**部署完成后无需任何手动操作，数据会自动更新。**

---

## 命令参数

| 命令 | 作用 | 适用场景 |
|------|------|----------|
| `bash pull_from_sftp.sh --install` | 一键部署（验证+拉取+配cron） | **首次使用，只需执行一次** |
| `bash pull_from_sftp.sh` | 拉取全部历史数据 | 补全历史数据 |
| `bash pull_from_sftp.sh --today` | 只拉取今天的数据 | 手动刷新当天数据 |
| `bash pull_from_sftp.sh --date 20260319` | 拉取指定日期的数据 | 补拉某天的数据 |
| `bash pull_from_sftp.sh --uninstall` | 移除 cron 定时任务 | 不再需要自动拉取时 |
| `bash pull_from_sftp.sh --help` | 显示帮助信息 | — |

> 所有命令都是**增量拉取**，已存在的文件不会重复下载。

---

## 自动拉取时间表

`--install` 配置的 cron 任务（每 30 分钟自动拉取一次，以下为各数据源推送完成的参考时刻）：

| 数据源 | 推送时刻（服务器） | 文件 |
|--------|-------------------|------|
| 抖音热词 | 每天 01:10 / 08:10 / 16:10 | `douyin_hotwords_*.json` |
| 搜狗词库 | 每天 05:10 / 19:10 | `sogou_*.txt` |
| 微博热搜 | 每天 07:10 / 19:10 | `weibo_hotsearch_*.txt` |
| 抖音热榜 | 每天 09:10 / 21:10 | `douyin_hotlist_*.txt` |
| 小红书热点 | 每天 09:30 / 23:30 | `xhs_hot_*.txt` |
| 小红书热帖 | 每天 10:00 / 23:45 | `xhs_hotpost_*.json` |

---

## 数据目录结构

拉取后数据保存在脚本同目录的 `hotword_data/` 下：

```
hotword_data/
├── douyin_hotwords/
│   └── 20260319/
│       └── douyin_hotwords_20260319_0110.json
├── sogou_newwords/
│   └── 20260319/
│       └── sogou_20260319_0510.txt
├── weibo_hotsearch/
│   └── 20260319/
│       └── weibo_hotsearch_20260319_0710.txt
├── douyin_hotlist/
│   └── 20260319/
│       └── douyin_hotlist_20260319_0910.txt
├── xhs_hot/
│   └── 20260319/
│       └── xhs_hot_20260319_1110.txt
└── xhs_hotpost/
    └── 20260319/
        └── xhs_hotpost_20260319_1000.json
```

---

## 数据格式

### 搜狗词库 `sogou_????????_????.txt`

Tab 分隔，每行一条：

```
词条	拼音
阿策	ace
爱哎唉变装	aiaiaibianzhuang
一颗赛艇	yikesaiting
```

### 抖音热词 `douyin_hotwords_????????_????.json`

包含抖音热词 API 返回的完整字段（标题、热度、趋势、排名等），每条数据附带拼音：

```json
[
  {"title": "天气", "score": 999, "rank": 1, "pinyin": "tian qi", "query_day": "20260319", ...},
  {"title": "周杰伦", "score": 888, "rank": 2, "pinyin": "zhou jie lun", ...}
]
```

### 微博热搜 `weibo_hotsearch_????????_????.txt`

Tab 分隔，三列：词条、拼音、热度整数（网页显示的搜索热度原始值）：

```
特朗普	`te`lang`pu	99876543
五一旅游	`wu`yi`lv`you	8765432
```

### 抖音热榜 `douyin_hotlist_????????_????.txt`

Tab 分隔，三列：词条、拼音、热度整数：

```
黑神话悟空	`hei`shen`hua`wu`kong	9876543
春节特辑	`chun`jie`te`ji	8765432
```

### 小红书热点 `xhs_hot_????????_????.txt`

Tab 分隔，三列：词条、拼音、热度整数（view_num 万次浏览转换后的整数）：

```
用万能旅行拍照姿势美美出片	`yong`wan`neng`lv`xing`pai`zhao`zi`shi`mei`mei`chu`pian	9078000
耗时三年拍下古诗词里的中国	`hao`shi`san`nian`pai`xia`gu`shi`ci`li`de`zhong`guo	8973000
```

---

## 环境变量

可通过环境变量覆盖默认配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SFTP_KEY` | 脚本同目录/`id_ed25519` | SFTP 私钥路径 |
| `LOCAL_DIR` | 脚本同目录/`hotword_data` | 数据保存目录 |

示例：

```bash
# 指定密钥和保存目录
SFTP_KEY=/home/user/.ssh/my_key LOCAL_DIR=/data/oppo bash pull_from_sftp.sh --today
```

---

## 运维命令

```bash
# 查看 cron 定时任务
crontab -l

# 查看拉取日志
tail -f pull.log

# 查看已拉取的文件数
find hotword_data/ -type f | wc -l

# 查看今天的文件
ls -la hotword_data/douyin_hotwords/$(date +%Y%m%d)/
ls -la hotword_data/sogou_newwords/$(date +%Y%m%d)/
ls -la hotword_data/weibo_hotsearch/$(date +%Y%m%d)/
ls -la hotword_data/douyin_hotlist/$(date +%Y%m%d)/
    ls -la hotword_data/xhs_hot/$(date +%Y%m%d)/
    ls -la hotword_data/xhs_hotpost/$(date +%Y%m%d)/
```

---

## 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `密钥文件不存在` | `id_ed25519` 不在脚本同目录 | 把密钥放到和脚本同一目录，或用 `SFTP_KEY` 指定路径 |
| `无法连接或远程目录为空` | 网络不通或密钥错误 | `sftp -i ./id_ed25519 oppo01@82.156.76.22` 手动测试 |
| `无数据或目录不存在` | 当天数据还没推送 | 等爬虫推送完成后再拉，或用 `--date` 拉历史日期 |
| `已是最新` | 文件已下载过 | 正常，增量拉取不会重复下载 |
