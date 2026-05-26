# xhs_hotsearch

抓取小红书官方热搜榜（经 rebang.today 聚合）+ 在小红书官网逐个搜索热搜词，
导出 Markdown 笔记链接清单。

## 文件

- `fetch_hotsearch.py` — 从 `api.rebang.today` 抓取热搜榜，写入带时间戳的 TXT。
- `search_notes.py` — 复用项目已有的 `crawler.xhs_hotpost.browser.XhsBrowser`
  调用 `/api/sns/web/v1/search/notes` 拿真实 `note_id` + `xsec_token`，
  拼出可访问的 explore 链接。
- `run.py` — 串起整个流程：抓热搜 → 搜笔记 → 生成 Markdown。

## ⚠️ 前置条件：必须先登录 XHS

XHS 搜索接口（包括搜索结果页本身）对未登录访问完全屏蔽。
首次使用前必须扫码登录一次：

```bash
# 有显示器（如本机）
python -m crawler.xhs_hotpost.login

# 无显示器（服务器，截图二维码后用 App 扫）
python -m crawler.xhs_hotpost.login --headless
```

登录态会持久化到 `browser_profile/xhs/`，本工具默认复用同一目录，
之后无需重复登录（XHS web_session 一般有效数周）。

## 用法

```bash
# 默认：Top 20 热搜，每个词取前 5 个笔记链接
python -m xhs_hotsearch.run

# 调整参数
python -m xhs_hotsearch.run --top 20 --notes-per-keyword 5
python -m xhs_hotsearch.run --no-headless        # 看浏览器执行过程
python -m xhs_hotsearch.run --profile-dir /path/to/another/profile
```

## 输出

所有结果落到 `xhs_hotsearch/output/<YYYYMMDD>/`：

- `hotsearch_<YYYYMMDD_HHMM>.txt` — 热搜榜原始数据，
  每行 `rank<TAB>title<TAB>heat<TAB>icon`。
- `hotsearch_notes_<YYYYMMDD_HHMM>.md` — Markdown 报告，
  Top N 热搜作为一级标题，每条热搜下的笔记链接作为二级标题。

## 依赖

均已包含在项目根 `requirements.txt` 中：

- `requests`
- `playwright`（首次需 `python -m playwright install chromium`）
- `httpx`
- `playwright-stealth`（可选，未装会有告警但不影响功能）
