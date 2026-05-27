# xhs_tendencyhotsearch — 小红书趋势热搜（方案二）

> 详细思路见 [../xhs_hotsearch/xhs热搜榜获取方案.md](../xhs_hotsearch/xhs热搜榜获取方案.md) 中"方案二"。

## 思路

不依赖第三方聚合站，把"小红书热搜"定义为：

```
querytrending 候选趋势词  →  逐词调 search/notes 验证互动 + 综合分排序  →  Markdown
```

## 综合分公式

```
综合分 = 30% 趋势词排名分    # 候选越靠前越高
       + 35% 互动分          # log10(liked + 2*comment + collected + 3*share)，10w→1.0
       + 15% 近期占比分      # 近 30 天发布的笔记占比
       + 10% 有效笔记数      # min(items, 20) / 20
       + 10% 标题命中分      # 标题包含关键词的笔记占比
```

满分 100。

## 目录与产物

```
xhs_tendencyhotsearch/
├── __init__.py
├── fetch_trending.py     # Step 1
├── score_and_links.py    # Step 2
├── run.py                # Step 3 + orchestrator
└── output/<YYYYMMDD>/
    ├── trending_candidates_<TS>.txt   # 原始候选词
    ├── trending_ranked_<TS>.txt       # 综合分排序后 TXT
    ├── trending_breakdown_<TS>.json   # 全量打分明细
    └── trending_notes_<TS>.md         # Top N H1 + 笔记链接 H2
```

## 用法

```bash
# 一次性扫码登录（首次或 cookie 失效时）
python -m crawler.xhs_hotpost.login

# 跑全流程（默认候选 30 → 综合分 Top 20，每词 5 条笔记）
python -m xhs_tendencyhotsearch.run

# 常用参数
python -m xhs_tendencyhotsearch.run --max-candidates 40 --top 20 --notes-per-keyword 5
python -m xhs_tendencyhotsearch.run --no-headless    # 调试看浏览器
```

## 约束

- 必须先用 `crawler/xhs_hotpost/login.py` 在 `browser_profile/xhs/` 完成扫码登录。
- `querytrending` 接口对每次调用都有反爬校验，候选词数量取决于 XHS 当前返回。
- 候选词越多打分越准，但耗时线性增长（每词 ~2s）。
