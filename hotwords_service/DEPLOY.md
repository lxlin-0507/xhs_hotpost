# hotwords_service 部署手册

## 1. 安装 Python 3.8+

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-pip

# CentOS / RHEL
sudo yum install -y python3 python3-pip
```

验证：
```bash
python3 --version
# 预期：Python 3.8.x 或以上
```

---

## 2. 安装依赖

```bash
cd new-hot/hotwords_service
pip3 install -r requirements.txt
```

> `requirements.txt` 包含 `hanlp>=2.1.0` 和 `pypinyin>=0.50`。
> hanlp 包本身很小（几 MB），**模型文件**（约 500MB）在第一次运行时自动下载。

---

## 3. 准备 HanLP 模型

HanLP 模型需要联网下载，**部署机如果网速慢或无法访问外网，请提前处理**。

### 方案 A：部署机有网，自动下载

跳过本节，直接进入第 4 步，首次运行时会自动下载（约 500MB，需几分钟）。

### 方案 B：手动下载后传入（推荐用于生产环境）

在本地网络好的机器上下载：

```bash
# 指定模型目录（之后传给部署机）
export HANLP_HOME=/path/to/hanlp_models
python3 -c "
import hanlp
hanlp.load(hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH)
print('下载完成')
"
# 打包传输
tar -czf hanlp_models.tar.gz /path/to/hanlp_models
```

在部署机上解压并设置环境变量：

```bash
tar -xzf hanlp_models.tar.gz -C /opt/
export HANLP_HOME=/opt/hanlp_models
# 建议写入 ~/.bashrc 或启动脚本，持久生效
echo 'export HANLP_HOME=/opt/hanlp_models' >> ~/.bashrc
```

### 验证模型是否就绪

直接跑冒烟测试即可——模型加载失败会在启动时立即报错退出：

```
RuntimeError: HanLP 模型加载失败：...
模型目录：/opt/hanlp_models
可能原因：
  1. 模型尚未下载（需联网或手动放置）
  2. 磁盘空间不足（需约 500MB）
  3. HANLP_HOME 目录无写权限
手动下载：export HANLP_HOME=/your/path && python3 -c "..."
```

---

## 4. 冒烟测试

```bash
python3 run.py --once --base-dir /tmp/hotwords
```

预期输出：
```
[2026-05-08 17:10:15] [INFO] ── 扫描数据来源 (mode=phrase) ──
[2026-05-08 17:10:15] [INFO]   微博热搜: .../weibo_hotsearch_20260508_0710.txt (44 条)
[2026-05-08 17:10:15] [INFO]   抖音热榜: .../douyin_hotlist_20260508_0910.txt (50 条)
[2026-05-08 17:10:15] [INFO]   小红书热点: .../xhs_hot_20260508_1110.txt (20 条)
[2026-05-08 17:10:15] [INFO]   抖音热词: .../douyin_hotwords_20260508_1610.json (600 条)
[2026-05-08 17:10:15] [INFO] 已加载搜狗词库快照 112170 词
[2026-05-08 17:10:15] [INFO] 切词词典 112267 词；主白名单 360 词；次白名单 112170 词；屏蔽词 295 词
[2026-05-08 17:10:24] [INFO] HanLP 模型加载完成
[2026-05-08 17:10:42] [INFO] 输出完成: /tmp/hotwords/rank/20260508/rank_20260508_171042.csv  (477 行)
```

> **首次运行**模型加载约需 10 秒，后续复用进程内缓存无需重新加载。

---

## 5. 后台启动

```bash
nohup python3 run.py --base-dir /tmp/hotwords > run.log 2>&1 &
echo $! > run.pid
```

验证已启动：
```bash
tail -f run.log
# 预期：打印"下次执行: 2026-XX-XX XX:19:00  (等待 XXXs)"
```

服务每小时在 **:19** 和 **:49** 自动触发一次。

---

## 常用命令

```bash
# 查看日志
tail -f run.log

# 停止服务
kill $(cat run.pid)

# 重启
kill $(cat run.pid) && nohup python3 run.py --base-dir /tmp/hotwords > run.log 2>&1 & echo $! > run.pid
```

---

## 重新部署

代码更新后执行：

```bash
cd new-hot/hotwords_service

# 1. 拉取最新代码
git pull

# 2. 停止旧进程
kill $(cat run.pid) 2>/dev/null || true

# 3. 重新安装依赖（依赖有变动时）
pip3 install -r requirements.txt

# 4. 重新启动
nohup python3 run.py --base-dir /tmp/hotwords > run.log 2>&1 &
echo $! > run.pid

# 5. 确认启动
tail -f run.log
```

---

## 搜狗词库更新

搜狗词库以快照形式随代码一起部署（`sogou_snapshot.txt`），不依赖运行时目录。
需要更新词库时，在**有 SFTP 数据的机器上**执行：

```bash
python3 refresh_sogou_snapshot.py --src /path/to/sogou_newwords --days 7
```

然后将更新后的 `sogou_snapshot.txt` 提交并重新部署。

---

## 注意事项

| 事项 | 说明 |
|------|------|
| 必须文件 | `run.py` `segmenter_hanlp.py` `user_dict.txt` `blocklist.txt` `sogou_snapshot.txt` 必须在同一目录，缺失任何一个进程启动时会报错 |
| HanLP 模型 | 默认存放在 `~/.hanlp/`，可通过 `HANLP_HOME` 环境变量指定路径 |
| 内存占用 | HanLP 模型加载后常驻内存约 300MB，请确保机器有足够内存 |
| 首次启动 | 模型加载需约 10 秒，之后不受影响 |
| 词库更新 | `user_dict.txt` / `blocklist.txt` 修改后需**重启进程**才能生效 |
