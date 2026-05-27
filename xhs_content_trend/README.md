# 小红书站内话题热点推断（无登录态）

不依赖小红书热搜榜接口、登录账号或三方聚合站，从无登录推荐流采样笔记，
按"完整 #话题#"维度做多维评分，输出可解读的热点话题榜。

## 设计原则

- **只识别完整话题**，不做分词 / N-gram。来源：
  1. 笔记 `tag_list` 结构化字段（detail API / `__INITIAL_STATE__` / HTML 兜底）
  2. `desc` 正文中 `#XXX[话题]#` 字面正则
  3. 高赞评论里 `#XXX[话题]#` 字面正则（作为提及强度信号）
- **完全无登录**：使用持久化 visitor profile，让 XHS 风控逐步把这台设备
  当成普通游客（多次跑后评论从 461 → 200）。
- **多维评分 → 单一综合分 → 热点判定**。

## 流程

1. 启动 noauth Playwright（`browser_profile/xhs_noauth/`）打开 explore。
2. 滚动触发 `/api/sns/web/v1/homefeed` 拿推荐流，DOM 上提取每条笔记的
   `xsec_token`。
3. 逐条 goto 详情，并行拦截 `/api/sns/web/v4/note/detail` 与
   `/api/sns/web/v2/comment/page`，提取互动数 / desc / tag_list / 评论。
4. 抽取完整话题（结构化 + 正则兜底）。
5. 按下列维度归一化打分。
6. 输出 TSV 与判定。

## 综合热点分公式

```text
total = 0.30 * coverage_score        # 命中笔记数（覆盖广度）
      + 0.30 * engagement_score      # 命中笔记互动总和（log1p）
      + 0.20 * exposure_score        # 推荐位排序权重（1/log2(rank+1.5)）
      + 0.15 * comment_score         # 评论提及次数（社区共鸣）
      + 0.05 * density_score         # 同笔记内重复（弱信号）
```

## 是否热点判定

同时满足以下三个条件才标 `是否热点=是`：

```text
total_score >= 40
命中笔记数 >= 2
命中笔记互动总和 >= 1000
```

## 运行

```bash
./venv/bin/python -m xhs_content_trend.run                          # 默认 80 条
./venv/bin/python -m xhs_content_trend.run --max-notes 40 --limit 50
./venv/bin/python -m xhs_content_trend.run --headed                 # 显示浏览器
```

仅离线复算已有采样：

```bash
./venv/bin/python -m xhs_content_trend.run \
  --from-json xhs_content_trend/output/20260527/xhs_sample_20260527_1548.json
```

## 输出

```text
xhs_content_trend/output/YYYYMMDD/
  xhs_sample_YYYYMMDD_HHMM.json        # 原始采样
  xhs_topic_trend_YYYYMMDD_HHMM.txt    # 话题热点榜（TSV）
```

TSV 列：

```text
排名  话题  综合分  是否热点  命中笔记数  互动总和
覆盖得分  互动得分  推荐位得分  评论提及得分  重复密度得分
评论提及次数  证据标题
```

## 已知约束

- 单次跑只能拿到 ~20%–40% 笔记的完整 tag_list / desc（无登录态下很多笔记走
  HTML 兜底路径，detail API 仅在 a1 老用户化之后命中率才上去）。
- 单次结果中大多数话题"命中笔记数=1"是正常的——需要多次累积才能形成
  稳定热点榜。后续可加多文件合并模式。
- 评论里能直接抽到 `#话题#` 的概率低，`评论提及得分` 多数为 0；这与 XHS
  评论 UI 把 hashtag 渲染为可点击链接而非纯文本有关。
- 不再依赖 jieba / N-gram，原"碎片词条"问题彻底消除。
