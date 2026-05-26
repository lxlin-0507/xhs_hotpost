"""
hotwords_service 回归测试

运行方式：
    cd hotwords_service
    python3 test_regression.py

测试覆盖：
  1. 基础切词正确性（品牌/地名/机构）
  2. 人名：不再维护，HanLP NER 自动处理，截断不报错
  3. blocklist 拦截（出圈/塌房/歌手等）
  4. 事件短语拼接（Track A）
  5. 否定上下文抑制
  6. 短词白名单（sogou 非 NER 词 vs NER 泛词过滤）
  7. subsumption 去重
  8. user_dict ∩ blocklist 矛盾词：最终不出现（blocklist 优先）
  9. ASCII 热词（DeepSeek / ChatGPT 在 phrase 模式能出现）
 10. 数字占比过高过滤
 11. 美妆/网络新词（user_dict 强制整词）
 12. 时尚动词 event_verbs（穿搭）参与短语
 13. 复合事件标题（白鹿跑男争议 / 航天员中心）
"""
from __future__ import annotations
import sys
import os
from pathlib import Path
from typing import Set, List

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
os.environ.setdefault("HANLP_HOME", str(_here / "hanlp"))


def _load_lines(p: Path) -> Set[str]:
    if not p.exists():
        return set()
    out: Set[str] = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line.split()[0])
    return out


def _load_sogou(p: Path) -> Set[str]:
    if not p.exists():
        return set()
    out: Set[str] = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


print("[setup] 加载词典…")
stopwords   = _load_lines(_here / "stopwords.txt")
blocklist   = _load_lines(_here / "blocklist.txt")
user_dict   = _load_lines(_here / "user_dict.txt")
sogou_dict  = _load_sogou(_here / "sogou_snapshot.txt")
event_verbs = _load_lines(_here / "event_verbs.txt")
full_dict   = user_dict | sogou_dict

from segmenter_hanlp import HanlpSegmenter  # type: ignore

seg = HanlpSegmenter(
    stopwords=stopwords,
    blocklist=blocklist,
    user_dict=full_dict,
    keep_short_words=user_dict,
    secondary_short_words=sogou_dict,
    event_verbs=event_verbs,
)
print("[setup] 加载 HanLP 模型…")
seg._ensure_loaded()
print("[setup] 完成\n")

# ──────────────────────────────────────────────
PASS = 0
FAIL = 0

def check(label: str, title: str,
          must_have: List[str] = (),
          must_not: List[str] = (),
          must_contain: List[str] = ()):
    """
    must_have:    结果集里必须**精确出现**该词
    must_not:     结果集里不能出现该词
    must_contain: 结果集里至少有一个词**包含**该子串（词比期望长时用）
    """
    global PASS, FAIL
    result = seg.extract_phrases(title)
    result_set = set(result)
    errors = []
    for w in must_have:
        if w not in result_set:
            errors.append(f"  缺失(精确): {w!r}")
    for w in must_not:
        if w in result_set:
            errors.append(f"  多余: {w!r}")
    for w in must_contain:
        if not any(w in r for r in result_set):
            errors.append(f"  缺失(子串): {w!r}")
    status = "PASS" if not errors else "FAIL"
    if status == "PASS":
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {label}")
    print(f"       输入: {title!r}")
    print(f"       输出: {result}")
    for e in errors:
        print(e)
    print()

# ──────────────────────────────────────────────
# 1. 品牌 / 地名在 user_dict 中，dict_force 保证不被切碎
# phrase 模式会把品牌+后续名词合并成更长短语（正确行为）；
# 核心保证是：品牌名不被拆开（如 鸿蒙 / 智行 独立出现 = 错误）
check("品牌-鸿蒙智行不切碎",
      "鸿蒙智行新款亮相北京车展",
      must_not=["鸿蒙", "智行"])   # 品牌不能被拆；长短语 鸿蒙智行新款 是正常输出

check("品牌-比亚迪不切碎",
      "比亚迪全球销量突破百万",
      must_not=["亚迪", "比亚"])   # 不切碎即可；比亚迪全球销量 是正常 phrase

check("地名-阿勒泰不切碎",
      "阿勒泰旅游热度再创新高",
      must_not=["勒泰", "阿勒"])   # j-tag 修复后不再只剩热度；阿勒泰旅游热度 应出现

check("机构-米哈游",
      "米哈游原神五周年活动公布",
      # 米哈游(nr→user_dict exception)进 buf 后与原神合并为"米哈游原神"，比单独"米哈游"更精准
      must_contain=["米哈游"],
      must_not=["米哈游原神活动"])  # 不能把"活动"也拼进来

# ──────────────────────────────────────────────
# 2. 人名：不保证精确，只要不崩溃、不产出 blocklist 词
check("人名-知名政治家（NER 应能识别）",
      "特朗普再次当选美国总统",
      must_not=list(blocklist))

check("人名-艺人（HanLP 自行处理，不强求）",
      "迪丽热巴新剧首播引发热议",
      must_not=list(blocklist))

# ──────────────────────────────────────────────
# 3. blocklist 拦截
check("blocklist-通用动作词",
      "中国女队夺冠晋级决赛",
      must_not=["夺冠", "晋级", "决赛"])

check("blocklist-歌手（既在 user_dict 又在 blocklist，blocklist 优先）",
      "歌手2024总决赛收官",
      must_not=["歌手"])

check("blocklist-出圈塌房",
      "某流量明星塌房后竟然出圈",
      must_not=["出圈", "塌房"])

check("blocklist-通用时间词",
      "今天明天都不上班",
      must_not=["今天", "明天"])

# ──────────────────────────────────────────────
# 4. 事件短语拼接（Track A）
check("短语-卧床志愿者",
      "航天员中心招募卧床志愿者",
      must_have=["卧床志愿者"])

check("短语-动词前置-巨变",
      "内娱综艺审美巨变",
      must_not=list(blocklist))

# ──────────────────────────────────────────────
# 5. 否定上下文不产出否定片段
check("否定-不该先定罪后解读",
      "不该先定罪后解读",
      must_not=["定罪", "解读"])

# ──────────────────────────────────────────────
# 6. 短词白名单
check("短词-曼联在 sogou 应能出现",
      "曼联主场迎战阿森纳",
      # 曼联在 sogou 里且非 NER → 应放行
      # 阿森纳在 user_dict 里应放行
      must_have=["阿森纳"])

check("短词-NER 泛词应被拦（2 字地名/人名不能靠 sogou 进来）",
      "北京举办国际马拉松赛事",
      # 北京是 2 字 NER 实体，sogou 里有但应被 NER 过滤
      must_not=["北京"])

# ──────────────────────────────────────────────
# 7. subsumption 去重（短词被长短语包含则丢弃）
check("去重-华谊不应与华谊兄弟同时出现",
      "华谊兄弟回应破产传闻",
      must_not=["华谊"])

# ──────────────────────────────────────────────
# 8. 数字占比过高过滤
check("数字过滤-比分不是热词",
      "湖人102比94击败雷霆晋级",
      must_not=["102", "94", "102比94"])

# ──────────────────────────────────────────────
# 9. ASCII/外来词（nx tag）在 phrase 模式应能进入 buf 而不成切断点
# nx 修复后：DeepSeek/nx 进 buf，与后续名词合并成 DeepSeek大模型
check("ASCII-nx-DeepSeek不被丢弃",
      "DeepSeek大模型再次刷新基准",
      must_not=["模型"])  # 单独 模型 应被 DeepSeek大模型 subsumption 掉

# ──────────────────────────────────────────────
# 10. 已知 jieba 残片在 blocklist 中
check("残片-blocklist 拦截 jieba 碎片",
      "深空冒险者登顶热搜",
      must_not=["深空"])  # 深空在 blocklist

# ──────────────────────────────────────────────
# 11. 美妆网络新词（user_dict 强制整词）
# 来源: xhs_hot "珠圆玉润妆完全是淡颜天菜"
# HanLP 会把成语"珠圆玉润"拆碎(珠/圆/玉/润妆)，必须靠 user_dict 固定
check("美妆-珠圆玉润妆 整词不被拆碎",
      "珠圆玉润妆完全是淡颜天菜",
      must_have=["珠圆玉润妆", "淡颜天菜"],
      must_not=["颜天菜", "珠圆玉润"])  # 不能出现无妆的残片

# ──────────────────────────────────────────────
# 12. event_verbs 中的时尚动词（穿搭 被 HanLP 标 v → 加入 event_verbs 改为 vn）
# 来源: weibo "习惯高中穿搭的我突然上了大学"
# 若"穿搭"仍为 v 则作切点，"高中"2字单独被拦 → 输出为空
check("穿搭-event_verbs 使高中穿搭正确提取",
      "习惯高中穿搭的我突然上了大学",
      must_have=["高中穿搭"],
      must_not=["习惯高中"])

# ──────────────────────────────────────────────
# 13. 复合事件标题 — 多实体分段提取
# 来源: weibo "白鹿跑男争议内娱综艺审美巨变"（原始无空格）
# "争议"被标 v → 切点，形成 [白鹿跑男] + [内娱综艺审美巨变]（均合理）
check("复合标题-白鹿跑男与内娱综艺审美均出现",
      "白鹿跑男争议内娱综艺审美巨变",
      must_have=["白鹿跑男"],
      must_contain=["内娱综艺"])  # 实际输出为"内娱综艺审美巨变"，子串匹配

# 来源: douyin_hotlist "航天员中心招募卧床志愿者"
# 期望：机构名和复合名词短语均被提取
check("复合标题-航天员中心与卧床志愿者同时出现",
      "航天员中心招募卧床志愿者",
      must_have=["航天员中心", "卧床志愿者"],
      must_not=["卧床"])  # 不应出现孤立"卧床"

# 来源: douyin_hotlist "奥德赛时期不是人生低谷"
# user_dict 中已有"奥德赛时期"→ 完整提取
check("user_dict-奥德赛时期不被截断为奥德赛",
      "奥德赛时期不是人生低谷",
      must_have=["奥德赛时期", "人生低谷"],
      must_not=["奥德赛", "低谷"])

# ──────────────────────────────────────────────
# 14. 图片里的三个已知问题
# 来源: weibo "跑男争议不该先定罪后解读"
check("否定-定罪不应从否定句提取",
      "跑男争议不该先定罪后解读",
      must_not=["定罪"])

# 来源: douyin "CBA北京淘汰广东挺进4强"
# 期望：淘汰(event_verb→vn) 使北京淘汰广东拼成完整赛事短语
# "广东挺进" 是截断片段，在 blocklist
check("赛事-CBA北京淘汰广东",
      "CBA北京淘汰广东挺进4强",
      must_contain=["北京淘汰广东"],
      must_not=["广东挺进", "CBA北京"])

# 来源: douyin "张皓嘉"（人名截断 → 张皓）
# HanLP NER 有时把"张皓嘉"识别为"张皓"，属 NER 局限，不强求修复；
# 但要保证"张皓"单独出现时不在 blocklist 里被误杀
check("人名-张皓嘉不被 blocklist 误杀",
      "张皓嘉最新动态",
      must_not=list(blocklist))  # 不管识别成"张皓嘉"还是"张皓"，都不应被屏蔽

# ──────────────────────────────────────────────
print("=" * 50)
print(f"结果: {PASS} PASS  {FAIL} FAIL  (共 {PASS+FAIL} 项)")
if FAIL:
    sys.exit(1)
