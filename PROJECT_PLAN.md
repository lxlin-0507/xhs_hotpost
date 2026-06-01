# PROJECT_PLAN.md — 小红书无登录热点话题检测系统

> **当前活跃阶段：Phase 2**  
> 每次开始工作前必须读本文件，只执行当前活跃阶段的内容。

---

## 一、项目背景与目标

### 1.1 问题定义

小红书官方已下线"热搜榜"公开入口；第三方聚合站（rebang.today 等）长期未更新，数据失效。业务方需要**每天 2–4 次批量产出小红书当前热点**，用于创作灵感、舆情监控与运营选题。

### 1.2 核心约束

| 约束项 | 要求 |
|--------|------|
| 登录依赖 | **完全无登录**（硬约束，不允许账号 Cookie 进入生产链路） |
| 更新频率 | 每天 2–4 次批处理，非实时 |
| 输出形态 | 双层：A 层榜单短词条 + B 层可运营话题标签（`#话题#`） |
| 风险偏好 | 平衡（中频+限速，不激进不高并发） |
| 运维门槛 | 无人值守可运行，故障自动降载 |

### 1.3 产出定义

- **榜 A — 短词条榜**：类似热搜榜风格，每条 2–10 字，可读可展示  
  文件格式：`词条\t拼音\t热度分值`（与现有 `xhs_hot` TXT 格式兼容）
- **榜 B — 运营话题榜**：来自作者主动打的 `#话题#` 结构化标签，具备话题可运营性  
  文件格式：TSV，字段包含 `rank/topic/total_score/is_hot/note_count/engagement_sum/evidence`

---

## 二、历史方案与废弃原因

以下方案均已验证不可行，**禁止在本项目中重新尝试**：

| # | 方案 | 废弃原因 |
|---|------|---------|
| 1 | rebang.today / 三方热榜聚合站 | 长期无更新，数据已失效 |
| 2 | 登录态"猜你想搜"采集 | 账号个性化过重，结果不代表全站热点；含广告位；推送内容在短期内固定不变 |
| 3 | Guest 模式探测热榜 API | `/hot_list`、`/board/list`、`/trending/query` 等全部 406/失败；游客搜索入口被禁用 |
| 4 | jieba / N-gram 对标题+评论分词 | 切碎词不可用（用户明确否决）；词条无法直接展示为可读话题 |
| 5 | 登录账号池 / 高并发代理 | 超出风险红线，不进入当前方案 |

---

## 三、新方案架构

### 3.1 总体思路

```
多 Profile 无登录 homefeed 采样（去个性化）
  ↓
多轮小批次累积（24h 时间窗，早/中/晚三段）
  ↓
tag_list 三路抽取（API > JS注入 > HTML正则）
  ↓
多维时间窗稳态打分（覆盖×互动×推荐位×复现率×新鲜度×来源可信度）
  ↓
泛标签下压（IDF / 历史均值惩罚）
  ↓
双层输出：榜 A（短词条） + 榜 B（可运营话题）
  ↓
接入现有 pipeline/scheduler 定时调度
```

### 3.2 官方信号分级

| 等级 | 来源 | 说明 |
|------|------|------|
| P0 强信号 | 笔记结构化 `tag_list`（`type:"topic"`字段） | 作者主动打标、平台结构化返回，最可靠 |
| P0 强信号 | 小红书官方活动/话题页（公开无登录可见部分） | 若可抓取，优先级最高 |
| P1 中信号 | homefeed 中高复现话题（去个性化聚合后） | 需多 profile、多时段消除个性化偏差 |
| P2 弱信号 | 标题短语共现（辅助补全，不单独成榜） | 辅助填充 P0/P1 盲区，须附带 P0/P1 证据 |

**每个候选词条须绑定来源证据计数**：同一话题在不同 profile、不同时段命中 ≥ 2 次才可进入最终榜。

### 3.3 去个性化设计

- 设备池：`browser_profile/xhs_noauth_0/` ~ `browser_profile/xhs_noauth_N/`（N ≥ 3）  
- 时段分层：`[08:00–10:00]` / `[12:00–14:00]` / `[20:00–22:00]`  
- 每个 profile 每轮采样 20–30 条笔记，跨 profile 聚合  
- 聚合判断：话题出现在 ≥ 2 个独立采样单元方可进入候选

---

## 四、技术栈（约束性规定）

以下技术选型**不得随意替换**，如需变更须在本文档对应章节更新后再执行：

| 层级 | 技术 | 版本约束 | 说明 |
|------|------|----------|------|
| 浏览器自动化 | Playwright（Python） | `>=1.40` | 持久化 context，不使用 Selenium |
| 反检测 | playwright-stealth | `>=2.0` | `navigator_platform_override="MacIntel"` |
| HTTP 辅助 | requests / httpx | `>=2.28` / `>=0.25` | requests 用于 API 探测，httpx 用于异步场景 |
| 定时任务 | APScheduler | `>=3.10` | BackgroundScheduler，已有实现不变 |
| Web API | Flask | `>=3.0` | 已有实现，新增路由 |
| 话题抽取 | 纯正则（无分词器） | — | **禁止引入 jieba 用于话题抽取**，jieba 仅用于 `pipeline/hot_words.py` 的词频统计 |
| 打分归一化 | 纯 Python + math | — | 不引入 numpy/scipy，保持轻量 |
| 数据持久化 | 本地文件（TXT/JSON/TSV） | — | 与现有 `pipeline/store.py` 格式兼容 |
| 配置管理 | python-dotenv + config.py | `>=1.0` | 所有新配置项加入 `config.py` `Config` 类 |
| 拼音生成 | pypinyin | `>=0.50` | 榜 A 需要输出拼音列 |
| Python 版本 | Python 3.10+ | — | f-string、dataclass、`__future__` annotations 均可用 |

---

## 五、目录结构规划

新增模块统一放在 `crawler/xhs_trend_proxy/` 下：

```
oppo-hotwords-master/
├── PROJECT_PLAN.md                         ← 本文件（必读）
├── .github/
│   └── copilot-instructions.md             ← AI 全局规则文件
├── config.py                               ← 新增 XHS_TREND_PROXY_* 配置项
├── crawler/
│   └── xhs_trend_proxy/                    ← 新增主模块
│       ├── __init__.py
│       ├── run.py                          ← 命令行入口，支持 --profile-pool / --time-slot
│       ├── sampler.py                      ← 多 Profile 多轮小批次采样器
│       ├── extractor.py                    ← 话题抽取 + 可信度附注（复用/扩展 scorer.py 逻辑）
│       └── anti_block.py                   ← 限速/抖动/profile 生命周期管理
├── browser_profile/
│   ├── xhs_noauth_0/                       ← Profile 池（运行时自动创建）
│   ├── xhs_noauth_1/
│   └── xhs_noauth_N/
├── xhs_content_trend/
│   ├── scorer.py                           ← 打分逻辑，Phase 5 扩展时间窗稳态分
│   ├── sampler.py                          ← 已有，被新模块复用
│   └── run.py                              ← 已有，Phase 6 新增双榜入参
├── pipeline/
│   ├── normalize.py                        ← Phase 6 接入新 source
│   ├── hot_words.py                        ← Phase 6 新增 source 分组
│   └── scheduler.py                        ← Phase 6 新增定时任务入口
└── output/
    └── xhs_trend_proxy/                    ← 新输出目录
        └── {YYYYMMDD}/
            ├── xhs_trend_list_{date}_{slot}.txt    ← 榜 A（TXT 三列）
            └── xhs_topic_trend_{date}_{slot}.txt   ← 榜 B（TSV，已有格式）
```

---

## 六、各阶段执行计划

### ✅ Phase 1 — 目标口径与信号定义（已完成）

**交付物**：本文档的第一、二、三章  
**验收**：产出定义、废弃方案清单、官方信号分级已记录在案

---

### 🔄 Phase 2 — 无登录主链路重构（**当前阶段**）

**目标**：将单轮孤立采样改为多 Profile 多轮累积，产出可进入打分的聚合样本。

**任务列表**：

#### 2.1 `config.py` — 新增配置项

```python
# 在 Config 类中新增：
XHS_TREND_PROXY_SAVE_DIR      = _abs(os.getenv("XHS_TREND_PROXY_SAVE_DIR", "output/xhs_trend_proxy"))
XHS_TREND_PROXY_PROFILE_ROOT  = _abs(os.getenv("XHS_TREND_PROXY_PROFILE_ROOT", "browser_profile"))
XHS_TREND_PROXY_PROFILE_COUNT = int(os.getenv("XHS_TREND_PROXY_PROFILE_COUNT", "3"))
XHS_TREND_PROXY_NOTES_PER_PROFILE = int(os.getenv("XHS_TREND_PROXY_NOTES_PER_PROFILE", "25"))
XHS_TREND_PROXY_MIN_EVIDENCE  = int(os.getenv("XHS_TREND_PROXY_MIN_EVIDENCE", "2"))
XHS_TREND_PROXY_HEADLESS      = os.getenv("XHS_TREND_PROXY_HEADLESS", "true").lower() not in ("0", "false", "no")
```

#### 2.2 `crawler/xhs_trend_proxy/sampler.py` — 多 Profile 采样器

- 类 `MultiProfileSampler`，方法 `sample_all() -> List[Dict]`
- 遍历 `xhs_noauth_0` ~ `xhs_noauth_{N-1}`，每个 profile 调用已有 `fetch_noauth_feed_and_comments()`
- 在每条笔记上注入 `_profile_id` 字段，用于下游证据计数
- profile 之间随机等待 30–90 秒（`anti_block.profile_sleep()`）
- 如某 profile 返回空（风控），跳过该 profile 并记录警告，不中断整体任务

#### 2.3 `crawler/xhs_trend_proxy/anti_block.py` — 防封控基础库

- `profile_sleep(base_min=30, base_max=90)` — profile 切换间隔
- `note_sleep(base_min=3, base_max=8)` — 单笔记间隔
- `on_error(code: int) -> float` — 退避秒数：461/406 返回 600；其他非 0 返回 30
- `profile_health(profile_dir: str, stats_dir: str) -> str` — 返回 `"healthy"` / `"degraded"` / `"dead"`
  - degraded：最近 10 次请求中封控码 ≥ 30%
  - dead：最近 10 次全部封控
- `rotate_profile(profile_root: str, count: int) -> List[str]` — 返回按健康度排序的 profile 路径列表

#### 2.4 `crawler/xhs_trend_proxy/run.py` — 命令行入口

```
python -m crawler.xhs_trend_proxy.run
python -m crawler.xhs_trend_proxy.run --profiles 3 --notes-per-profile 25 --headed
```

- 调用 `MultiProfileSampler.sample_all()`
- 原始聚合样本写入 `output/xhs_trend_proxy/{YYYYMMDD}/xhs_trend_sample_{date}_{slot}.json`
- 调用打分器（Phase 5 前先复用现有 `XhsTopicScorer`）产出 TSV
- 写入 `output/xhs_trend_proxy/{YYYYMMDD}/xhs_topic_trend_{date}_{slot}.txt`

**验收标准**：
- 3 个 profile 全部正常运行，总样本量 ≥ 60 条，至少 25% 笔记含非空 tag_list
- 单次运行总耗时 ≤ 15 分钟
- 风控触发时自动跳过，不崩溃

---

### ⏳ Phase 3 — 创作者灵感校准（可选，当前不执行）

**前提**：Phase 2 稳定运行 7 天且 tag 命中率 < 25% 才考虑启动  
**策略**：作为离线评估基准，不进入生产主链路，不依赖账号登录  
**当前行动**：无，等待 Phase 2 数据验证结果

---

### ⏳ Phase 4 — 防封控工程化（依赖 Phase 2，部分已在 2.3 实现）

**剩余任务**（Phase 2 完成后执行）：

- `anti_block.py` 新增：profile 自动淘汰与重建（`dead` 状态 profile 自动 rm + 重新冷启动）
- 风控指标持久化到 `output/xhs_trend_proxy/health/{YYYYMMDD}/anti_block_stats.json`
- 封控率超阈值时通过 `pipeline/alerts.py` 发送通知

---

### ⏳ Phase 5 — 打分升级（依赖 Phase 2/4）

**目标**：将当前单次 max-min 评分升级为时间窗稳态分。

**任务**：
- `xhs_content_trend/scorer.py` 扩展：新增 `freshness_score`、`recurrence_score`、`credibility_score`
- 泛标签下压：维护 `xhs_content_trend/stopwords_topic.txt`，高频泛词权重乘 0.5
- `XhsTopicScorer` 新增 `history_window` 参数（接收历史批次做 IDF 分母）
- 榜 A 产出：对榜 B 热词做拼音生成（`pypinyin`）+ 热度分映射，写 TXT 三列格式

**Phase 5 目标评分权重**：

```
total = 0.25 * coverage      # 命中笔记数（去重 profile）
      + 0.25 * engagement    # log1p(互动总和)
      + 0.20 * exposure      # 推荐位加权
      + 0.15 * recurrence    # 跨采样单元复现次数
      + 0.10 * freshness     # 24h 新鲜度
      + 0.05 * credibility   # 来源可信度（P0/P1/P2 比例）
```

---

### ⏳ Phase 6 — 调度接入与回归验证（依赖 Phase 5）

**任务**：
- `config.py`：source key `xhs_trend_proxy` 已由 Phase 2 添加，本阶段无新增
- `pipeline/normalize.py`：`PLATFORM_WEIGHTS["xhs_trend_proxy"] = 0.15`，新增 TXT 解析分支
- `pipeline/hot_words.py`：`PLATFORM_GROUP["xhs_trend_proxy"] = "xhs"`，追加 `HOT_SOURCES`
- `pipeline/scheduler.py`：新增 `run_xhs_trend_proxy_task()`；cron：`08:30 / 12:30 / 20:30 / 23:45`；CLI `--run-xhs-trend-proxy`
- 影子运行 7–14 天，达标后将 `xhs_trend_proxy` 纳入总榜权重

---

## 七、数据流图

```
[Phase 2] MultiProfileSampler
  ├─ xhs_noauth_0 → fetch_noauth_feed_and_comments() → notes_0[]
  ├─ xhs_noauth_1 → fetch_noauth_feed_and_comments() → notes_1[]
  └─ xhs_noauth_N → fetch_noauth_feed_and_comments() → notes_N[]
        ↓ merge (inject _profile_id per note)
  merged_notes[]
        ↓ save
  xhs_trend_sample_{date}_{slot}.json
        ↓
[Phase 5] XhsTopicScorer.score(merged_notes, history_window)
  ├─ tag_list 三路抽取
  ├─ 多维打分（覆盖/互动/推荐位/复现率/新鲜度/可信度）
  ├─ 泛标签下压
  └─ 双层输出
        ├─ 榜 B → xhs_topic_trend_{date}_{slot}.txt（TSV）
        └─ 榜 A → xhs_trend_list_{date}_{slot}.txt（TXT 三列）
              ↓
[Phase 6] pipeline/normalize.py → hot_words.py → scheduler.py
  └─ 接入统一总榜
```

---

## 八、防封控策略（平衡模式）

### 8.1 限速参数

| 参数 | 默认值 | 层级 |
|------|--------|------|
| 笔记间隔 | 3–8 秒随机 | `noauth_fetcher` 内部 |
| profile 切换间隔 | 30–90 秒随机 | `MultiProfileSampler` |
| 批次运行间隔 | 每天 2–4 次，时段错峰 | `scheduler` |
| 461/406 退避 | 600 秒 | `anti_block.on_error()` |
| 其他错误退避 | 30 秒 | `anti_block.on_error()` |

### 8.2 行为仿真最小集

- 滚动深度随机：800–2000ms（现有 `_jitter()` 实现复用）
- 进入详情页后等待 2–4 秒再触发评论请求
- UA 保持固定（模拟固定设备），不频繁轮换

### 8.3 Profile 生命周期

```
冷启动 → 老化期（前 3 次，可能 461）→ 成熟期（评论 200 稳定）→ 退化期 → 淘汰重建
```

- 生产中保持 ≥ 3 个 profile，任意时刻至少 2 个处于成熟期
- 健康指标写入 `output/xhs_trend_proxy/health/` 供监控

---

## 九、验收标准

### Phase 2 验收

- [ ] 3 个 profile 串行运行无崩溃
- [ ] 总样本量 ≥ 60 条笔记
- [ ] tag 命中率 ≥ 25%
- [ ] 单次运行 ≤ 15 分钟
- [ ] 风控触发时自动跳过该 profile，日志正常记录

### Phase 4 验收

- [ ] profile 健康度状态文件正确生成
- [ ] 7 天连续运行，封控码比例无持续上升

### Phase 5 验收

- [ ] 榜 A（TXT）和榜 B（TSV）双文件同时产出
- [ ] 榜 A 格式与 `pipeline/normalize.py` 的 `xhs_hot` 解析分支兼容
- [ ] Top 20 话题人工评估：可读性 ≥ 70%，无明显碎词

### Phase 6 验收

- [ ] `xhs_trend_proxy` 出现在 `pipeline/hot_words.py` 总榜输出
- [ ] 影子运行 7 天，新晋话题召回率优于现有方案
- [ ] `--run-xhs-trend-proxy` CLI 参数独立触发正常

---

## 十、关键文件速查

| 文件 | 作用 | 当前阶段相关度 |
|------|------|--------------|
| `crawler/xhs_hotpost/noauth_fetcher.py` | 无登录采样核心，Phase 2 直接复用 | ★★★★★ |
| `crawler/xhs_trend_proxy/sampler.py` | Phase 2 新增，多 Profile 采样器 | ★★★★★ |
| `crawler/xhs_trend_proxy/anti_block.py` | Phase 2/4 新增，防封控基础库 | ★★★★★ |
| `crawler/xhs_trend_proxy/run.py` | Phase 2 新增，命令行入口 | ★★★★★ |
| `xhs_content_trend/scorer.py` | 话题打分，Phase 2 直接复用，Phase 5 扩展 | ★★★★☆ |
| `xhs_content_trend/sampler.py` | 单 Profile 采样器，Phase 2 调用基础 | ★★★★☆ |
| `config.py` | Phase 2 新增配置项 | ★★★★☆ |
| `pipeline/scheduler.py` | Phase 6 接入，**当前不改** | ★★☆☆☆ |
| `pipeline/normalize.py` | Phase 6 接入，**当前不改** | ★★☆☆☆ |
| `无登陆采样+推断.md` | 历史方案依据，废弃方案参考 | ★★★☆☆ |

---

*文档版本：v1.0 — 2026-06-01*  
*下次更新触发条件：Phase 2 完成验收后，将顶部"当前活跃阶段"改为 Phase 4。*
