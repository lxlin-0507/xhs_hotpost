# 小红书热搜榜获取方案

> 实现目录：[xhs_hotsearch/](.) ｜ 完成时间：2026-05-26

## 一、背景

- 小红书 PC 站官方"热搜榜"入口已下线，无公开 URL 可直接拿 Top N + 热度。
- 未登录访问 `/search_result` 页面会被登录弹窗拦截（DOM 抓取不可行）。
- 仓库已有可复用能力：[crawler/xhs/spider.py](../crawler/xhs/spider.py) 用过 `rebang.today`；[crawler/xhs_hotpost/browser.py](../crawler/xhs_hotpost/browser.py) 已封装持久化登录 + `window.mnsv2` 签名。

## 二、方案一：第三方平台 rebang 取榜（oppo 热词项目） + 复用 XhsBrowser 调搜索 API

### Step 1 — 抓热搜榜

- 接口：`GET https://api.rebang.today/v1/items?tab=xiaohongshu&sub_tab=hot-search&version=1`
- 解析 JSON，按 `rank` 升序，输出 `hotsearch_<YYYYMMDD_HHMM>.txt`

### Step 2 — 搜索每个热词取笔记链接

- 复用 `XhsBrowser(profile_dir=browser_profile/xhs)`，前置一次性扫码登录：`python -m crawler.xhs_hotpost.login`。
- 调 `POST /api/sns/web/v1/search/notes`，**完整 payload**（缺字段会被软拒绝：`code=0` 但无 `items`）：

  ```python
  payload = {
      "keyword": kw, "page": 1, "page_size": 20,
      "search_id": _gen_search_id(),
      "sort": "general", "note_type": 0,
      "ext_flags": [],
      "filters": [
          {"tags": ["general"], "type": "sort_type"},
          {"tags": ["不限"], "type": "filter_note_type"},
          {"tags": ["不限"], "type": "filter_note_time"},
          {"tags": ["不限"], "type": "filter_note_range"},
          {"tags": ["不限"], "type": "filter_pos_distance"},
      ],
      "geo": "",
      "image_formats": ["jpg", "webp", "avif"],
  }
  ```

- 过滤 `model_type in ("rec_query","hot_query")`，取前 5 条，拼链接：
  `https://www.xiaohongshu.com/explore/{note_id}?xsec_token={quote(token)}&xsec_source=pc_search`
- 每个关键词间隔 ~2s，规避频控。

### Step 3 — 生成 Markdown

- 文件名与 TXT 共用时间戳：`hotsearch_notes_<YYYYMMDD_HHMM>.md`
- 格式：每个热词 H1，每条笔记 URL 一个 H2（无结果则 `## （未抓到笔记链接）`）。

  ```markdown
  # 1. 热词标题（热度 907.8w · 🔥）
  ## https://www.xiaohongshu.com/explore/xxx?xsec_token=...
  ## https://www.xiaohongshu.com/explore/yyy?xsec_token=...
  ```

### 目录与执行

```
xhs_hotsearch/
├── fetch_hotsearch.py   # Step 1
├── search_notes.py      # Step 2
├── run.py               # Step 3 + orchestrator
└── output/<YYYYMMDD>/   # TXT + MD
```

```bash
# 一次性登录（首次或 cookie 失效时）
python -m crawler.xhs_hotpost.login

# 跑全流程（默认 Top 20，每词 5 条笔记，headless）
python -m xhs_hotsearch.run
```

实测耗时：20 词 × 5 链接约 2 分钟。

## 三、方案二：搜索趋势触发 + 站内搜索验证（思路未实现）

> 核心思路：不再依赖小红书已消失的"热搜榜"页面，而是把"小红书热搜"定义为"搜索框推荐趋势词 + 站内搜索结果热度验证"后生成的趋势榜。其优势在于不依赖第三方的聚合平台，数据来源为小红书官方推荐趋势词

该方法可能会根据账号不同有一定随机性，

### Step 1 — 模拟真实用户触发趋势词接口

- 复用项目里的 Playwright 浏览器能力，打开小红书首页，点击搜索框，拦截真实页面触发的接口：
  `/api/sns/web/v1/search/querytrending`
- 获取候选热搜词列表。

### Step 2 — 对候选词逐个搜索验证

- 对每个候选词调用小红书站内搜索接口：
  `/api/sns/web/v1/search/notes`
- 获取前 10-20 条笔记，提取点赞、评论、收藏、分享、发布时间、标题命中情况等信息。

### Step 3 — 计算综合热度分

- 对每个词生成内部热度分：

  ```text
  综合分 =
      趋势词排名分
    + 搜索结果互动分
    + 近期内容占比分
    + 有效笔记数量分
    + 标题/描述命中分
  ```

- 排序后得到小红书趋势榜 Top N。

- 或者可与其他可直接爬取热搜榜平台综合比对确认是否为真实热搜，具体评判与比对标准和方法暂未设计


