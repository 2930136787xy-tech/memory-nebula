# -*- coding: utf-8 -*-
"""记忆召回评测工具：合成语料 + 50 道题，对比「纯向量」与「向量+关键词融合」。

为什么要有这个工具
------------------
`Brain.recall()` 是整套记忆系统最核心、也最容易被改坏的一段：改一个权重、
换一个向量模型、给 FTS 换个分词，都会悄悄影响"该想起来的想起来了吗"。
这个脚本把这件事变成可重复的数字：

1. 现场生成一个**临时 SQLite 库**（表结构取 `memories` 的最小字段集），
   灌入 92 条合成记忆——一对情侣/朋友从 2025 年 9 月到 12 月的多轮对话，
   每条都带时间信息。正式库 `brain.db` 一行都不碰。
2. 跑 50 道题，五类各 10 道（LongMemEval 的五类）：
   信息抽取、跨会话推理、时间推理、知识更新、拒答。题量在启动时用断言锁死。
3. 两种召回跑同一份语料、同一批题：
   - `vector`：纯向量基线（关键词那一路权重为 0）；
   - `fuse`  ：向量 + 关键词融合（关键词权重 alpha=0.3）。
   两路分数都用 `whalegirl.fts.fuse()` 合并，保证"只有关键词那一路不同"。
4. 指标：Hit@1/3/5、MRR@5、拒答题正确拒答率；拒答阈值由 6+8 道校准题
   自动标定（校准题不计入 50 题），所以两个模式的阈值不同、但都可复现。
5. `--json` 出机器可读结果，方便进 CI 或画趋势线；`--db` 对真实库做**只读**
   冒烟测试（临时 FTS 索引，不改任何一行）。

用法
----
    python scripts/eval_memory.py                 # 单模式（融合）+ 指标表
    python scripts/eval_memory.py --auto          # 两模式对比
    python scripts/eval_memory.py --auto --json   # 机器可读
    python scripts/eval_memory.py --db brain.db   # 真实库只读冒烟

说明文档见 `docs/评测集说明.md`。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------- 路径准备

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent                      # 工作目录 = 仓库根，PYTHONPATH 指向它
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 正式库路径：只允许以只读方式打开，任何写操作前都要过 _forbid_write()
REAL_DB = Path(r"brain.db")

CATEGORIES = ("信息抽取", "跨会话推理", "时间推理", "知识更新", "拒答")
PER_CATEGORY = 10
EXPECTED_TOTAL = PER_CATEGORY * len(CATEGORIES)   # 50，下面有断言

# 评测基准时间：语料最后一条是 2025-12-24，取 2026-01-05 算"现在"。
REF_NOW = datetime(2026, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()

SCOPE = "eval"
SPEAKER = "u1001"
AUDIENCE = "private:u1001"

# 打分用的常数（与 companion_brain.Brain.recall 同构，见 docs）
W_IMPORTANCE = 0.22
W_RECENCY = 0.12
W_EMOTION = 0.11
RELEVANCE_SCALE = 0.80          # 把 0.7*sim+0.3*kw 拉到生产同量级（0.56/0.24）
RECENCY_HALF_DAYS = 30.0        # 与生产一致：exp(-days/30)
MIN_KEYWORD_COVERAGE = 0.03     # IDF 覆盖率低于此值算"只撞上一两个常见字"，丢掉
DEFAULT_ALPHA = 0.3             # 关键词权重

# 与生产同构的「时间感知 + 重排 + 拒答 + 查询分流」（C4/C5/C7）。
# --parity 打开后基线/融合都加规则，差异只剩关键词那一路；--route 再让推理题只走语义。
W_RECENCY_TIME = 0.30
DATE_BONUS = 0.12
RERANK_EXACT = 0.12
RERANK_COVER = 0.10
ABSTAIN_TOP = 0.42
ABSTAIN_GAP = 0.06

_TIME_CUE_RE = re.compile(
    r"什么时候|哪天|哪一天|几号|几月|多久|上次|上回|最近|以前|之前|那天|当时|"
    r"昨天|前天|上周|上个月|去年|前年|今年|第一次|最开始")
_REASONING_RE = re.compile(
    r"为什么|怎么|那次|后来|因为|所以|还记得|之前说|当时|然后|之后|结果|是不是")
_DATE_RE = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}[日号]|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"去年|前年|今年|上个月|上周|昨天|前天|生日|第一次|纪念日")

# 向量相似度归一化：先减掉"不相关也有"的基线，再按有效区间拉伸。
# bge-small-zh 在本语料上实测：不相关约 0.30~0.45，相关约 0.52~0.69，
# 所以基线与量程取 0.40 / 0.28（不是拍的，见 docs「阈值与归一化」一节）。
# 启发式哈希向量没有这个基线，用 (cos-0)/1。
DENSE_BASE, DENSE_SCALE = 0.40, 0.28
HASH_BASE, HASH_SCALE = 0.0, 1.0


# ================================================================ 合成语料
#
# 写法约定
# --------
# 每个 session 是"一次对话"，turns 里的每条就是一条记忆（Bot 当时存下来的事实），
# 正文自带时间信息；importance / emo 是写入时的评分。
# SUPERSEDED 记录"旧事实被新事实替代"：旧记忆 status='superseded'，
# 这正是知识更新题的考点——旧的那条不应该再被召回。

@dataclass
class Memory:
    """一条合成记忆（对应 memories 表的一行）。"""

    key: str
    session: str
    date: str
    clock: str
    content: str
    importance: float
    emo: float
    kind: str = "fact"
    status: str = "active"
    superseded_by: str = ""

    @property
    def epoch(self) -> float:
        """按 +08:00 解析成时间戳，保证跨机器可复现。"""
        dt = datetime.strptime(f"{self.date} {self.clock}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()


# 会话：(会话 id, 日期, 时间, 主题, [(记忆键, 正文, 重要度, 情绪显著度)])
SESSIONS: list[dict[str, Any]] = [
    {
        "id": "S01", "date": "2025-09-02", "clock": "21:20", "topic": "第一次聊天",
        "turns": [
            ("m0902a", "2025年9月2日晚上九点多，她第一次找我聊天，说刚从青岛搬到北京，还没找到房子，暂时住在朋友家的沙发上。", 0.55, 0.3),
            ("m0902b", "2025年9月2日，她说她叫团子，是朋友起的外号，因为脸圆。她让我也这么叫她。", 0.70, 0.4),
            ("m0902c", "2025年9月2日，她说她是双鱼座，生日在3月17日，每年都会被朋友忘记。", 0.85, 0.3),
            ("m0902d", "2025年9月2日，她抱怨北京太干，嘴唇一直起皮，问我有没有好用的唇膏。", 0.30, 0.2),
            ("m0902e", "2025年9月2日，她说她留了五年的长直发，舍不得剪，洗头要吹半小时。", 0.55, 0.2),
            ("m0902f", "2025年9月2日，她说她的 iPhone 13 mini 用了三年，电池一天要充两次。", 0.50, 0.1),
            ("m0902g", "2025年9月2日，她说北京的地铁比青岛复杂，她已经坐反过两次。", 0.30, 0.2),
        ],
    },
    {
        "id": "S02", "date": "2025-09-05", "clock": "22:40", "topic": "加班",
        "turns": [
            ("m0905a", "2025年9月5日，她说她在蓝色光标做活动策划，案子堆到年底，经常加班到晚上十点。", 0.75, 0.2),
            ("m0905b", "2025年9月5日，她抱怨客户总在半夜十二点改需求，她已经连着三天没睡好。", 0.55, 0.5),
            ("m0905c", "2025年9月5日，她说每天要喝三杯美式才撑得住，办公室那台咖啡机是她的命。", 0.50, 0.2),
            ("m0905d", "2025年9月5日，她说她最想做品牌策划，不想一直写执行方案。", 0.60, 0.3),
            ("m0905e", "2025年9月5日，她工位上摆了一盆多肉，同事都拿它当她的标志。", 0.40, 0.2),
        ],
    },
    {
        "id": "S03", "date": "2025-09-08", "clock": "23:10", "topic": "猫",
        "turns": [
            ("m0908a", "2025年9月8日，她说她养了一只三花猫，是2024年12月的一个雪夜在楼道里捡的，那时候还没断奶。", 0.90, 0.6),
            ("m0908b", "2025年9月8日，她说那只猫叫汤圆，因为捡回来时圆滚滚的。", 0.70, 0.4),
            ("m0908c", "2025年9月8日，她说汤圆特别黏人，她一加班回家就被堵在门口。", 0.50, 0.5),
            ("m0908d", "2025年9月8日，她说猫粮一个月要两百多，比她自己吃饭还贵。", 0.30, 0.3),
            ("m0908e", "2025年9月8日，她说她给猫拍了三千多张照片，手机内存快满了。", 0.25, 0.3),
        ],
    },
    {
        "id": "S04", "date": "2025-09-15", "clock": "20:30", "topic": "家人",
        "turns": [
            ("m0915a", "2025年9月15日，她说她弟弟叫陈宇航，在成都读研究生，学的是材料。", 0.80, 0.3),
            ("m0915b", "2025年9月15日，她说她妈妈膝盖不好，一到下雨天就疼，她一直想买一副护膝。", 0.65, 0.5),
            ("m0915c", "2025年9月15日，她说她和弟弟差五岁，小时候总打架，现在关系最好。", 0.50, 0.4),
            ("m0915d", "2025年9月15日，她说她老家在青岛，最想念海边的蛤蜊和啤酒。", 0.70, 0.5),
            ("m0915e", "2025年9月15日，她说她爸话不多，每次打电话都只问钱够不够。", 0.40, 0.3),
        ],
    },
    {
        "id": "S05", "date": "2025-09-20", "clock": "19:00", "topic": "忌口",
        "turns": [
            ("m0920a", "2025年9月20日，她说她对芒果过敏，吃一口嘴唇就肿，高中时还进过一次急诊。", 0.85, 0.5),
            ("m0920b", "2025年9月20日，她说她喜欢香菜，吃火锅要单独点一份。", 0.35, 0.2),
            ("m0920c", "2025年9月20日，她说她不太能吃辣，但愿意陪我吃，只要求点一碗冰粉。", 0.50, 0.4),
            ("m0920d", "2025年9月20日，她说她一个人吃饭会开着综艺，不然太安静。", 0.35, 0.3),
        ],
    },
    {
        "id": "S06", "date": "2025-09-26", "clock": "23:50", "topic": "音乐",
        "turns": [
            ("m0926a", "2025年9月26日，她说她最喜欢的乐队是落日飞车，去年音乐节听《My Jinji》的时候哭了。", 0.85, 0.7),
            ("m0926b", "2025年9月26日，她说她大学时组过乐队，当贝斯手，只在学校的晚会上演过一场。", 0.60, 0.4),
            ("m0926c", "2025年9月26日，她说她想学吉他，但一直没开始，怕自己三分钟热度。", 0.55, 0.3),
            ("m0926d", "2025年9月26日，她说她想办张健身卡，但是一直拖着没去。", 0.50, 0.2),
            ("m0926e", "2025年9月26日，她说她洗澡的时候会唱歌，唱得很难听但很快乐。", 0.25, 0.2),
        ],
    },
    {
        "id": "S07", "date": "2025-10-02", "clock": "21:00", "topic": "第一次看电影",
        "turns": [
            ("m1002a", "2025年10月2日晚上，我们第一次一起看电影，看的是《宇宙探索编辑部》，九点场，她哭得眼睛红肿。", 0.95, 0.8),
            ("m1002b", "2025年10月2日，她说她看电影一定要坐最后一排靠边的位置，不然脖子疼。", 0.45, 0.2),
            ("m1002c", "2025年10月2日，看完电影我们在路边吃了烤冷面，她说这是她来北京最开心的一晚。", 0.80, 0.8),
            ("m1002d", "2025年10月2日，她说她出门前纠结了半小时穿什么，最后还是穿了那件灰毛衣。", 0.30, 0.2),
        ],
    },
    {
        "id": "S08", "date": "2025-10-06", "clock": "22:15", "topic": "室友",
        "turns": [
            ("m1006a", "2025年10月6日，她说她和林晓琪合租，两个人挤在一个老小区里。", 0.75, 0.3),
            ("m1006b", "2025年10月6日，她说林晓琪学的是平面设计，快毕业了，正在准备作品集。", 0.80, 0.3),
            ("m1006c", "2025年10月6日，她说林晓琪做饭很好吃，尤其是番茄牛腩。", 0.40, 0.3),
            ("m1006d", "2025年10月6日，她说她住在朝阳区，每天通勤要一个多小时。", 0.60, 0.2),
            ("m1006e", "2025年10月6日，她说她跟人合租主要是为了省房租，一个人实在扛不住。", 0.55, 0.3),
        ],
    },
    {
        "id": "S09", "date": "2025-10-11", "clock": "21:40", "topic": "出差",
        "turns": [
            ("m1011a", "2025年10月11日，她说下周三，也就是10月15日，要去杭州出差三天。", 0.85, 0.2),
            ("m1011b", "2025年10月11日，她说这次出差住在西湖边的酒店，会拍照片给我看。", 0.55, 0.3),
            ("m1011c", "2025年10月11日，她说她有点怕坐飞机，起飞时会抓紧扶手。", 0.50, 0.4),
        ],
    },
    {
        "id": "S10", "date": "2025-10-18", "clock": "20:50", "topic": "杭州回来",
        "turns": [
            ("m1018a", "2025年10月18日，她从杭州回来，说满城都是桂花香，还买了两罐龙井。", 0.60, 0.5),
            ("m1018b", "2025年10月18日，她说杭州让她想搬过去住，但舍不得北京的朋友。", 0.50, 0.4),
            ("m1018c", "2025年10月18日，她说她在西湖边一个人坐了一个小时，想明白了一些事。", 0.55, 0.4),
            ("m1018d", "2025年10月18日，她说她一直想去大理，看看洱海，最好能住上一个月。", 0.80, 0.6),
        ],
    },
    {
        "id": "S11", "date": "2025-10-24", "clock": "22:30", "topic": "投简历",
        "turns": [
            ("m1024a", "2025年10月24日，她说她投了小红书的岗位，是内容运营，在等面试通知。", 0.80, 0.3),
            ("m1024b", "2025年10月24日，她说她其实想考研，考新闻传播，但家里希望她先工作。", 0.70, 0.4),
            ("m1024c", "2025年10月24日，她说如果跳槽成功，第一件事是把工位上那盆多肉搬走。", 0.40, 0.2),
            ("m1024d", "2025年10月24日，她说她最近体重涨了六斤，都是加班外卖害的。", 0.35, 0.3),
        ],
    },
    {
        "id": "S12", "date": "2025-11-03", "clock": "21:10", "topic": "入职",
        "turns": [
            ("m1103a", "2025年11月3日，她入职新公司小红书，做内容运营，工位在十二层靠窗。", 0.90, 0.5),
            ("m1103b", "2025年11月3日，她说新公司六点半就能下班，她第一次在天还亮着的时候回家。", 0.75, 0.6),
            ("m1103c", "2025年11月3日，她说她把蓝色光标那盆多肉搬到了新工位，管它叫老同事。", 0.50, 0.3),
            ("m1103d", "2025年11月3日，她说新工作不用再写执行方案了，改成自己定选题。", 0.65, 0.4),
        ],
    },
    {
        "id": "S13", "date": "2025-11-09", "clock": "18:20", "topic": "搬家",
        "turns": [
            ("m1109a", "2025年11月9日，她搬家，从朝阳区搬到海淀区，离公司近了两站地。", 0.85, 0.3),
            ("m1109b", "2025年11月9日，她说新房租贵了一千块，但终于有一个能放下书桌的角落。", 0.65, 0.4),
            ("m1109c", "2025年11月9日，她说搬家那天林晓琪来帮忙，两个人吃掉三盒炸鸡。", 0.50, 0.4),
            ("m1109d", "2025年11月9日，她说搬出来一个人住了，不再合租，第一次有自己的房间。", 0.80, 0.6),
        ],
    },
    {
        "id": "S14", "date": "2025-11-15", "clock": "23:30", "topic": "猫生病",
        "turns": [
            ("m1115a", "2025年11月15日，汤圆发烧到四十度，她带它去宠物医院，花了八百六十块。", 0.80, 0.7),
            ("m1115b", "2025年11月15日，医生说她那只猫太胖了，要控制饮食，不然关节会出问题。", 0.70, 0.4),
            ("m1115c", "2025年11月15日，她说她在医院走廊抱着猫哭，觉得自己没照顾好它。", 0.75, 0.9),
        ],
    },
    {
        "id": "S15", "date": "2025-11-20", "clock": "21:30", "topic": "第二场电影",
        "turns": [
            ("m1120a", "2025年11月20日，我们又去看了一场电影，是个喜剧片，她笑到打嗝。", 0.55, 0.6),
            ("m1120b", "2025年11月20日，她说她喜欢看完电影走一段路再回家，不然像没结束。", 0.40, 0.3),
            ("m1120c", "2025年11月20日，她说她最近在追一部讲海洋的纪录片，看到凌晨两点。", 0.35, 0.3),
        ],
    },
    {
        "id": "S16", "date": "2025-11-21", "clock": "22:00", "topic": "改名",
        "turns": [
            ("m1121a", "2025年11月21日，她把猫的名字从汤圆改成了布丁，因为它现在圆得像一块布丁。", 0.85, 0.5),
            ("m1121b", "2025年11月21日，她说改名之后猫不理她，叫了三天才肯应。", 0.45, 0.4),
            ("m1121c", "2025年11月21日，她说她给猫买了个新窝，猫更喜欢装窝的纸箱。", 0.30, 0.3),
        ],
    },
    {
        "id": "S17", "date": "2025-11-27", "clock": "21:50", "topic": "学吉他",
        "turns": [
            ("m1127a", "2025年11月27日，她开始学吉他，报的是小区门口琴行的课，一周两次。", 0.80, 0.4),
            ("m1127b", "2025年11月27日，她说她学的第一首歌是《晴天》，和弦按得手指疼。", 0.60, 0.4),
            ("m1127c", "2025年11月27日，她说等学会了要弹给我听，不许我笑她。", 0.65, 0.7),
            ("m1127d", "2025年11月27日，她说琴行的老师夸她节奏感好，她高兴了一晚上。", 0.35, 0.5),
        ],
    },
    {
        "id": "S18", "date": "2025-12-05", "clock": "23:40", "topic": "吵架",
        "turns": [
            ("m1205a", "2025年12月5日，我们因为她回消息太慢吵了一架，她说她不是不在乎，是真的忙。", 0.80, 0.8),
            ("m1205b", "2025年12月5日深夜，她主动发消息说对不起，我们和好了，她说下次会提前说一声。", 0.75, 0.7),
            ("m1205c", "2025年12月5日，她说她吵架时会躲起来，是因为小时候家里吵架没人哄她。", 0.85, 0.8),
            ("m1205d", "2025年12月5日，她说她最近总做梦，梦见自己在一班赶不上的地铁上。", 0.30, 0.4),
        ],
    },
    {
        "id": "S19", "date": "2025-12-12", "clock": "20:00", "topic": "健身与咖啡",
        "turns": [
            ("m1212a", "2025年12月12日，她在悦动空间办了年卡，三千二，说这次一定要坚持。", 0.80, 0.5),
            ("m1212b", "2025年12月12日，她说她请了私教，一周三练，目标是能跑完五公里。", 0.55, 0.4),
            ("m1212c", "2025年12月12日，她说她戒了咖啡，改喝红茶，因为喝咖啡会心悸。", 0.75, 0.3),
            ("m1212d", "2025年12月12日，她终于给妈妈买了护膝，寄回青岛，还塞了两盒膏药。", 0.60, 0.5),
            ("m1212e", "2025年12月12日，她说她想清楚了，不考研了，先在小红书好好做两年。", 0.75, 0.3),
            ("m1212f", "2025年12月12日，她说她瘦了四斤，虽然看不出来，但她自己知道。", 0.30, 0.4),
            ("m1212g", "2025年12月12日，她说健身房的镜子让她第一次看清自己的体态。", 0.30, 0.3),
        ],
    },
    {
        "id": "S20", "date": "2025-12-18", "clock": "21:00", "topic": "剪短发",
        "turns": [
            ("m1218a", "2025年12月18日，她把留了五年的长直发剪短了，还染了红棕色。", 0.75, 0.5),
            ("m1218b", "2025年12月18日，她换了新手机，是一加十二，说旧的 iPhone 13 mini 电池不行了。", 0.60, 0.2),
            ("m1218c", "2025年12月18日，她说剪短发是因为想重新开始，跟过去这一年告个别。", 0.80, 0.7),
            ("m1218d", "2025年12月18日，她说弟弟陈宇航明年三月要来北京实习，可能会住她那儿。", 0.70, 0.4),
            ("m1218e", "2025年12月18日，她说理发师问她要不要剪更短，她犹豫了一下还是没敢。", 0.30, 0.2),
        ],
    },
    {
        "id": "S21", "date": "2025-12-24", "clock": "20:30", "topic": "平安夜",
        "turns": [
            ("m1224a", "2025年12月24日平安夜，我们一起吃了铜锅涮肉，她一个人吃了两盘羊肉。", 0.70, 0.7),
            ("m1224b", "2025年12月24日，她说她明年一定要去看一次海，什么都不干，就在海边躺着。", 0.80, 0.6),
            ("m1224c", "2025年12月24日，她送了我一条手织的围巾，说学了三个晚上才织完。", 0.85, 0.9),
            ("m1224d", "2025年12月24日，她说她今年最大的变化是学会了跟人说自己累。", 0.50, 0.5),
        ],
    },
]

# 旧事实 → 新事实：旧记忆落库时 status='superseded'，召回默认不返回。
SUPERSEDED: dict[str, str] = {
    "m0905a": "m1103a",   # 公司：蓝色光标 → 小红书
    "m1006d": "m1109a",   # 住址：朝阳 → 海淀
    "m0908b": "m1121a",   # 猫名：汤圆 → 布丁
    "m0902e": "m1218a",   # 发型：长直发 → 短发红棕
    "m0902f": "m1218b",   # 手机：iPhone 13 mini → 一加十二
    "m1024b": "m1212e",   # 学业：想考研 → 不考了
    "m1006e": "m1109d",   # 居住：合租省钱 → 一个人住
    "m0926d": "m1212a",   # 健身：想办卡 → 办了年卡
    "m0905d": "m1103d",   # 工作内容：写执行方案 → 自己定选题
}


# ================================================================ 题库

@dataclass
class Question:
    """一道评测题。"""

    qid: str
    category: str
    query: str
    gold: tuple[str, ...] = ()          # 应命中的记忆键
    abstain: bool = False               # 是否应该拒答
    hops: tuple[str, ...] = ()          # 跨会话题的另一跳（只用于自检/统计）
    decoys: tuple[str, ...] = ()        # 已知的强干扰项
    note: str = ""                      # 这题考什么


def _q(qid, category, query, gold=(), abstain=False, hops=(), decoys=(), note=""):
    return Question(qid, category, query, tuple(gold), abstain, tuple(hops), tuple(decoys), note)


QUESTIONS: list[Question] = [
    # ---------------- 信息抽取：一句话里就有答案 ----------------
    _q("E1", "信息抽取", "她说和她合租的那个女孩叫什么名字？", ["m1006a"], note="人名召回，靠字面"),
    _q("E2", "信息抽取", "她养的那只猫是什么花色的？", ["m0908a"], note="属性抽取：三花"),
    _q("E3", "信息抽取", "她的生日是哪一天？", ["m0902c"], note="日期抽取：3月17日"),
    _q("E4", "信息抽取", "她最喜欢的乐队叫什么？", ["m0926a"], note="专有名词：落日飞车"),
    _q("E5", "信息抽取", "她对什么水果过敏？", ["m0920a"], note="健康信息：芒果"),
    _q("E6", "信息抽取", "她弟弟叫什么名字？", ["m0915a"], note="人名：陈宇航"),
    _q("E7", "信息抽取", "她大学时在乐队里演过几场？", ["m0926b"], note="数量：一场"),
    _q("E8", "信息抽取", "她出差住的酒店在哪儿附近？", ["m1011b"], note="地点：西湖边"),
    _q("E9", "信息抽取", "她从杭州带回来的茶叶是什么？", ["m1018a"], note="物品：龙井"),
    _q("E10", "信息抽取", "她妈妈身体哪里不舒服？", ["m0915b"], note="身体部位：膝盖"),

    # ---------------- 跨会话推理：答案与线索分居两次对话 ----------------
    _q("X1", "跨会话推理", "她一直想去、有洱海的那个地方是哪儿？",
       ["m1018d"], hops=["m1224b"], decoys=["m1224b"], note="10月说大理，12月只说看海"),
    _q("X2", "跨会话推理", "她弟弟明年什么时候来北京实习？",
       ["m1218d"], hops=["m0915a"], note="9月知道弟弟，12月才知道实习时间"),
    _q("X3", "跨会话推理", "她以前每天三杯美式，后来因为心悸改喝什么了？",
       ["m1212c"], hops=["m0905c"], decoys=["m0905c"], note="9月习惯 + 12月更新"),
    _q("X4", "跨会话推理", "她九月说想学但一直没开始，后来真的去报班学的是什么乐器？",
       ["m1127a"], hops=["m0926c"], decoys=["m0908e", "m1127c"], note="9月愿望 + 11月行动"),
    _q("X5", "跨会话推理", "搬家那天来帮忙的那个室友，学的是什么专业？",
       ["m1006b"], hops=["m1109c"], note="11月的人名要靠10月那条解释"),
    _q("X6", "跨会话推理", "她一直想给妈妈买的东西，最后寄到哪儿了？",
       ["m1212d"], hops=["m0915b"], note="9月心愿 + 12月兑现"),
    _q("X7", "跨会话推理", "她跳槽时说要从工位搬走的那盆多肉，后来放到哪儿了？",
       ["m1103c"], hops=["m1024c"], note="10月计划 + 11月结果"),
    _q("X8", "跨会话推理", "她2024年雪夜捡回来的那只猫，医生说什么问题？",
       ["m1115b"], hops=["m0908a"], note="9月来历 + 11月体检"),
    _q("X9", "跨会话推理", "她刚来北京时住在朋友家，后来自己租的房子贵了多少？",
       ["m1109b"], hops=["m0902a"], note="9月起点 + 11月房租"),
    _q("X10", "跨会话推理", "她大学组过乐队，现在又开始学乐器，她当时在乐队里弹的是什么？",
       ["m0926b"], hops=["m1127a"], decoys=["m1127b"], note="两段音乐经历不能混"),

    # ---------------- 时间推理：需要挑中带正确日期的那条 ----------------
    _q("T1", "时间推理", "我们第一次一起看电影是哪一天？",
       ["m1002a"], decoys=["m1120a"], note="10月2日，别混成11月那场"),
    _q("T2", "时间推理", "她什么时候从朝阳搬到海淀的？",
       ["m1109a"], decoys=["m0902a"], note="11月9日"),
    _q("T3", "时间推理", "她哪天入职小红书的？",
       ["m1103a"], decoys=["m1024a"], note="11月3日，别混成投简历"),
    _q("T4", "时间推理", "她去杭州出差是哪一天？",
       ["m1011a"], decoys=["m1018a"], note="10月15日，别混成回来那天"),
    _q("T5", "时间推理", "她那只猫是哪天捡到的？",
       ["m0908a"], decoys=["m0908c"], note="2024年12月雪夜"),
    _q("T6", "时间推理", "我们上次吵架是哪天？",
       ["m1205a"], decoys=["m1205b"], note="12月5日，别混成和好那条"),
    _q("T7", "时间推理", "她办健身年卡是哪一天？",
       ["m1212a"], decoys=["m1212b"], note="12月12日"),
    _q("T8", "时间推理", "她的猫发烧去医院是哪一天？",
       ["m1115a"], decoys=["m1115b"], note="11月15日"),
    _q("T9", "时间推理", "她什么时候投的小红书简历？",
       ["m1024a"], decoys=["m1024c"], note="10月24日，别混成入职"),
    _q("T10", "时间推理", "她什么时候剪的短发？",
       ["m1218a"], decoys=["m1218c"], note="12月18日"),

    # ---------------- 知识更新：旧事实已被替代，要拿新的 ----------------
    _q("K1", "知识更新", "她现在在哪家公司上班？",
       ["m1103a"], decoys=["m1024a"], note="蓝色光标 → 小红书"),
    _q("K2", "知识更新", "她现在住在哪个区？",
       ["m1109a"], decoys=["m1109b"], note="朝阳 → 海淀"),
    _q("K3", "知识更新", "她的猫现在叫什么名字？",
       ["m1121a"], decoys=["m1115a"], note="汤圆 → 布丁"),
    _q("K4", "知识更新", "她现在是什么发型？",
       ["m1218a"], decoys=["m1218c"], note="长直发 → 短发红棕"),
    _q("K5", "知识更新", "她现在用什么手机？",
       ["m1218b"], decoys=["m0908e"], note="iPhone → 一加十二"),
    _q("K6", "知识更新", "她现在还打算考研吗？",
       ["m1212e"], note="想考研 → 不考了"),
    _q("K7", "知识更新", "她现在还是和别人合租吗？",
       ["m1109d"], decoys=["m1006a"], note="合租 → 一个人住"),
    _q("K8", "知识更新", "她现在几点能下班？",
       ["m1103b"], decoys=["m0905b"], note="十点 → 六点半"),
    _q("K9", "知识更新", "她后来到底办没办健身卡？",
       ["m1212a"], decoys=["m1212b"], note="一直拖着 → 办了年卡"),
    _q("K10", "知识更新", "她现在还需要写执行方案吗？",
       ["m1103d"], decoys=["m1103b"], note="写方案 → 自己定选题"),

    # ---------------- 拒答：语料里根本没有这个事实 ----------------
    _q("R1", "拒答", "她养的那只狗叫什么名字？", abstain=True,
       decoys=["m1121a"], note="只有猫，没有狗"),
    _q("R2", "拒答", "她大学的专业是什么？", abstain=True,
       decoys=["m1006b"], note="只说过室友的专业"),
    _q("R3", "拒答", "她老家的县城叫什么？", abstain=True,
       decoys=["m0915d"], note="只知道青岛"),
    _q("R4", "拒答", "她爸妈的生日分别是哪天？", abstain=True,
       decoys=["m0902c"], note="只知道她自己的生日"),
    _q("R5", "拒答", "她的车牌号是多少？", abstain=True,
       decoys=["m1109a"], note="没提过车"),
    _q("R6", "拒答", "她第一份工作的工资是多少？", abstain=True,
       decoys=["m0905a"], note="只说加班，没说钱"),
    _q("R7", "拒答", "她的婚礼打算在哪儿办？", abstain=True,
       decoys=["m1205c"], note="没提过结婚"),
    _q("R8", "拒答", "她最喜欢的电影导演是谁？", abstain=True,
       decoys=["m1002a"], note="只说过看过的片"),
    _q("R9", "拒答", "她的身高和体重具体是多少？", abstain=True,
       decoys=["m1212f"], note="只说过瘦了四斤"),
    _q("R10", "拒答", "她的护照什么时候过期？", abstain=True,
       decoys=["m1011c"], note="只说怕坐飞机"),
]

# 校准题：只用来定拒答阈值，不计入 50 题，也不参与指标。
# 无解校准题要和 50 题里的拒答题同分布：都是"这个话题有记录，但这个属性没提过"。
# 用"明天股市会涨吗"这种完全跑题的句子来标定，阈值会被压得极低，测出来的拒答率是假的。
CALIBRATION_NULL = [
    "她开的什么车？",
    "她大学在哪个学校读的？",
    "她的猫打过哪些疫苗？",
    "她今年的年终奖发了多少？",
    "她会弹钢琴吗？",
    "她最常去的那家咖啡店叫什么？",
    "她爸妈做什么工作？",
    "她租的房子有多少平米？",
]
CALIBRATION_GOLD = [
    ("她坐飞机的时候会紧张吗？", ["m1011c"]),
    ("她们家的猫粮一个月花多少钱？", ["m0908d"]),
    ("她一个人吃饭的时候会做什么？", ["m0920d"]),
    ("她洗澡的时候有什么习惯？", ["m0926e"]),
    ("她爸爸打电话都问什么？", ["m0915e"]),
    ("她给猫买的新窝，猫喜欢吗？", ["m1121c"]),
    ("她一个人住之后有什么变化？", ["m1109d"]),
    ("她最近一次哭是因为什么？", ["m1115c"]),
    ("她对什么调料特别喜欢？", ["m0920b"]),
    ("她手机内存为什么快满了？", ["m0908e"]),
]


# ================================================================ 自检
#
# 题量必须是真的 50 道、五类各 10 道；引用到的记忆键必须存在；
# 跨会话题的"另一跳"必须真的在别的会话里。这些都用断言锁住，改坏了当场报错。

def validate() -> dict[str, Any]:
    """启动时自检，返回语料统计。"""
    corpus = {m.key: m for m in _build_corpus()}
    assert len(QUESTIONS) == EXPECTED_TOTAL, (
        f"题量必须是 {EXPECTED_TOTAL} 道，现在是 {len(QUESTIONS)} 道")
    for cat in CATEGORIES:
        n = sum(1 for q in QUESTIONS if q.category == cat)
        assert n == PER_CATEGORY, f"「{cat}」类必须是 {PER_CATEGORY} 道，现在是 {n} 道"
    assert len({q.qid for q in QUESTIONS}) == EXPECTED_TOTAL, "题号有重复"

    for q in QUESTIONS:
        assert q.category in CATEGORIES, f"{q.qid} 的类别不对：{q.category}"
        if q.abstain:
            assert not q.gold, f"{q.qid} 是拒答题，不该有标准答案"
        else:
            assert q.gold, f"{q.qid} 不是拒答题，必须给标准答案"
        for key in q.gold + q.hops:
            assert key in corpus, f"{q.qid} 引用了不存在的记忆键：{key}"
            assert corpus[key].status == "active", (
                f"{q.qid} 把已失效的记忆 {key} 当答案/线索，它永远不可能被召回")
        for key in q.decoys:
            assert key in corpus, f"{q.qid} 引用了不存在的干扰项：{key}"
        if q.category == "跨会话推理":
            gs = {corpus[k].session for k in q.gold}
            hs = {corpus[k].session for k in q.hops}
            assert q.hops, f"{q.qid} 是跨会话题，必须至少给一个 hops"
            assert gs.isdisjoint(hs), (
                f"{q.qid} 的答案和线索落在同一个会话（{gs}），不算跨会话")
    for key in SUPERSEDED:
        assert key in corpus and SUPERSEDED[key] in corpus, f"替代关系写错了：{key}"
    for key, text in CALIBRATION_GOLD:
        for g in text:
            assert g in corpus and corpus[g].status == "active", f"校准题引用了坏的记忆：{g}"

    return {
        "memories": len(corpus),
        "sessions": len({m.session for m in corpus.values()}),
        "questions": len(QUESTIONS),
        "per_category": {c: sum(1 for q in QUESTIONS if q.category == c) for c in CATEGORIES},
        "superseded": len(SUPERSEDED),
    }


def _build_corpus() -> list[Memory]:
    """把 SESSIONS 摊平成记忆列表，并按 SUPERSEDED 打上失效标记。"""
    out: list[Memory] = []
    for s in SESSIONS:
        for key, text, imp, emo in s["turns"]:
            out.append(Memory(
                key=key, session=s["id"], date=s["date"], clock=s["clock"],
                content=text, importance=float(imp), emo=float(emo),
                status="superseded" if key in SUPERSEDED else "active",
                superseded_by=SUPERSEDED.get(key, ""),
            ))
    return out


# ================================================================ 临时库

SCHEMA = """
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL DEFAULT 'default',
    speaker_id    TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'fact',
    content       TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,
    confidence    REAL NOT NULL DEFAULT 0.8,
    strength      REAL NOT NULL DEFAULT 1.0,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_access_at REAL,
    created_at    REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    superseded_by INTEGER,
    source        TEXT NOT NULL DEFAULT 'chat',
    audience      TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    layer         TEXT NOT NULL DEFAULT 'episodic',
    emo_valence   REAL NOT NULL DEFAULT 0,
    emo_arousal   REAL NOT NULL DEFAULT 0,
    emo_affection REAL NOT NULL DEFAULT 0,
    emo_salience  REAL NOT NULL DEFAULT 0,
    recall_count  INTEGER NOT NULL DEFAULT 0,
    half_life_mult REAL NOT NULL DEFAULT 1.0,
    valid_from    TEXT NOT NULL DEFAULT '',
    valid_to      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_memories_scope ON memories(scope, status);
"""


def _forbid_write(path: Path) -> None:
    """任何写库动作前先过这一关：正式库绝对不许写。"""
    p = Path(os.path.abspath(path))
    real = Path(os.path.abspath(REAL_DB))
    if p == real:
        raise SystemExit(f"拒绝写入正式库：{p}；评测只能用临时库。")


def build_corpus_db(conn: sqlite3.Connection, memories: Sequence[Memory]) -> dict[str, int]:
    """建表并灌入合成记忆，返回 {记忆键: 行 id}。"""
    conn.executescript(SCHEMA)
    key_to_id: dict[str, int] = {}
    for m in memories:
        key_to_id[m.key] = 0
    for m in memories:
        sup_id = key_to_id.get(m.superseded_by, 0) or None
        cur = conn.execute(
            "INSERT INTO memories(scope, speaker_id, kind, content, importance, confidence,"
            " strength, access_count, last_access_at, created_at, updated_at, status,"
            " superseded_by, source, audience, subject, pinned, layer,"
            " emo_valence, emo_arousal, emo_affection, emo_salience, recall_count,"
            " half_life_mult, valid_from, valid_to)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (SCOPE, SPEAKER, m.kind, m.content, m.importance, 0.8,
             1.0, 0, None, m.epoch, m.epoch, m.status,
             sup_id, "chat", AUDIENCE, "", 0, "core" if m.importance >= 0.85 else "episodic",
             0.0, 0.0, 0.0, m.emo, 0, 1.0, m.date, "2026-01-05"),
        )
        # superseded_by 得等新记忆有 id 才能回填，这里先记行号
        key_to_id[m.key] = int(cur.lastrowid)
    # 回填 superseded_by（指向新的那条记忆 id）
    for old_key, new_key in SUPERSEDED.items():
        conn.execute("UPDATE memories SET superseded_by=? WHERE id=?",
                     (key_to_id[new_key], key_to_id[old_key]))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == len(memories)
    return key_to_id


# ================================================================ 向量与关键词

class Embedder:
    """向量来源：优先本地真模型，缺依赖就退回启发式，保证离线可跑。"""

    def __init__(self, prefer: str = "auto"):
        from whalegirl import providers as _providers

        reason = ""
        if prefer == "heuristic":
            prov = _providers.get_provider("heuristic")
            reason = "按参数强制使用启发式"
        else:
            try:
                from whalegirl.embedding import load_provider

                prov = load_provider("auto")
                reason = str(getattr(prov, "fallback_reason", "") or "")
            except Exception as exc:  # noqa: BLE001
                prov = _providers.get_provider("heuristic")
                reason = f"{type(exc).__name__}: {exc}"
        self.provider = prov
        self.name = str(getattr(prov, "name", "heuristic"))
        self.dim = int(getattr(prov, "dim", 0) or 0)
        self.reason = reason
        self.dense = self.name != "heuristic"
        self.base, self.scale = (DENSE_BASE, DENSE_SCALE) if self.dense else (HASH_BASE, HASH_SCALE)
        self._cache: dict[str, list[float]] = {}

    def _raw(self, text: str, query: bool) -> list[float]:
        key = ("q:" if query else "d:") + text
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fn = getattr(self.provider, "embed_query", None) if query else None
        vec = (fn(text) if callable(fn) else self.provider.embed(text)) or []
        self._cache[key] = vec
        return vec

    def doc(self, text: str) -> list[float]:
        return self._raw(text, query=False)

    def query(self, text: str) -> list[float]:
        return self._raw(text, query=True)

    def sim_norm(self, qvec: Sequence[float], dvec: Sequence[float]) -> float:
        """余弦相似度归一化到 0~1：减掉该模型"不相关也有"的基线。"""
        if not qvec or not dvec:
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for a, b in zip(qvec, dvec):
            dot += a * b
            na += a * a
            nb += b * b
        if na <= 0 or nb <= 0:
            return 0.0
        cos = dot / math.sqrt(na * nb)
        return _clamp((cos - self.base) / self.scale, 0.0, 1.0)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


class RecallEngine:
    """两种召回：'vector' 纯向量基线 / 'fuse' 向量 + FTS5 关键词融合。

    两路都用 `whalegirl.fts.fuse()` 合并，区别只在关键词那一路是否为空——
    这样"差异"只可能来自关键词，不会混进实现细节。
    """

    def __init__(self, conn: sqlite3.Connection, embedder: Embedder, mode: str,
                 alpha: float = DEFAULT_ALPHA, k: int = 5,
                 parity: bool = False, route: bool = False):
        from whalegirl import fts

        self.fts = fts
        self.conn = conn
        self.emb = embedder
        self.mode = mode
        self.alpha = float(alpha)
        self.k = int(k)
        self.parity = bool(parity)
        self.route = bool(route)
        self.rows = conn.execute(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " AND (speaker_id=? OR speaker_id='')"
            " ORDER BY updated_at DESC",
            (SCOPE, SPEAKER),
        ).fetchall()
        self.by_id = {int(r["id"]): r for r in self.rows}
        self.n_docs = len(self.rows)
        self._vec: dict[int, list[float]] = {
            int(r["id"]): embedder.doc(r["content"]) for r in self.rows}
        self._tok: dict[int, set[str]] = {
            int(r["id"]): set(fts.to_tokens(r["content"])) for r in self.rows}
        self._df: dict[str, int] = {}

    # ------------------------------------------------------------ 关键词那一路

    def _idf(self, token: str) -> float:
        df = self._df.get(token)
        if df is None:
            df = sum(1 for toks in self._tok.values() if token in toks)
            self._df[token] = df
        return math.log(1.0 + self.n_docs / (1.0 + df))

    def _coverage(self, query: str) -> dict[int, float]:
        """IDF 加权覆盖率：查询里的词有多大比例真的出现在这条记忆里。

        bm25 分数是按"本次查询的最高分"归一化的，永远有结果能拿到 1.0，
        单看它会以为"什么都相关"。乘上覆盖率才有绝对意义，拒答题才能判得动。
        """
        q_tokens = self.fts.to_query_tokens(query)
        if not q_tokens:
            return {}
        weights = {t: self._idf(t) for t in q_tokens}
        total = sum(weights.values()) or 1.0
        out: dict[int, float] = {}
        for mid, toks in self._tok.items():
            got = sum(w for t, w in weights.items() if t in toks)
            out[mid] = got / total
        return out

    def _reasoning_query(self, query: str) -> bool:
        """推理型查询：关键词会把字面相近的邻条顶上来（实测跨会话掉 10 个点）。"""
        q = (query or "").strip()
        return bool(_REASONING_RE.search(q)) or len(q) >= 14

    def _keyword_scores(self, query: str) -> list[tuple[int, float]]:
        if self.mode == "vector":
            return []
        if self.route and self._reasoning_query(query):
            return []                      # --route：推理题只走语义通道
            return []                      # 纯向量基线：这一路整体关掉
        hits = self.fts.search(self.conn, query, k=max(20, self.n_docs),
                               scope=SCOPE, speaker_id=SPEAKER, active_only=True)
        if not hits:
            return []
        cov = self._coverage(query)
        out: list[tuple[int, float]] = []
        for mid, score in hits:
            c = cov.get(int(mid), 0.0)
            if c < MIN_KEYWORD_COVERAGE:
                continue               # 只搭上一两个常见字，不算命中
            out.append((int(mid), round(float(score) * c, 4)))
        return out

    # ------------------------------------------------------------ 融合与打分

    def relevance(self, query: str) -> dict[int, float]:
        """把向量与关键词两路合并成 {记忆 id: 0~1 相关度}。"""
        qvec = self.emb.query(query)
        vec_rows = [(mid, self.emb.sim_norm(qvec, v)) for mid, v in self._vec.items()]
        kw_rows = self._keyword_scores(query)
        fused = dict(self.fts.fuse(vec_rows, kw_rows, alpha=self.alpha))
        for mid, _ in vec_rows:            # 保证每个候选都有值
            fused.setdefault(int(mid), 0.0)
        return {int(k): float(v) for k, v in fused.items()}

    def _prior(self, row: sqlite3.Row, time_q: bool = False) -> float:
        """记忆自身的先验：重要度 + 新近度 + 情绪显著度（与生产同构）。"""
        days = max(0.0, (REF_NOW - float(row["updated_at"])) / 86400.0)
        recency = math.exp(-days / RECENCY_HALF_DAYS)
        rec_w = W_RECENCY_TIME if (self.parity and time_q) else W_RECENCY
        return (W_IMPORTANCE * float(row["importance"])
                + rec_w * recency
                + W_EMOTION * float(row["emo_salience"] or 0.0))

    def _rerank(self, scored: list[tuple[float, int]], query: str) -> list[tuple[float, int]]:
        """规则重排（C5）：只动前 20 条，加「整句命中」和「双字覆盖率」。"""
        head, tail = scored[:20], scored[20:]
        qn = (query or "").strip()
        q_tokens = set(self.fts.to_query_tokens(qn))
        out: list[tuple[float, int]] = []
        for score, mid in head:
            content = str(self.by_id[mid]["content"] or "")
            bonus = 0.0
            if qn and qn in content:
                bonus += RERANK_EXACT
            if q_tokens:
                toks = self._tok[mid]
                bonus += RERANK_COVER * (len(q_tokens & toks) / float(len(q_tokens)))
            out.append((score + bonus, mid))
        out.sort(reverse=True)
        return out + tail

    def rank(self, rel: dict[int, float], limit: Optional[int] = None,
             tau: Optional[float] = None, query: str = "") -> list[dict[str, Any]]:
        """按综合分降序取前 limit 条；给了 tau 就先把证据不足的丢掉。"""
        time_q = bool(_TIME_CUE_RE.search(query or "")) if self.parity else False
        scored: list[tuple[float, int]] = []
        for mid, r in rel.items():
            if tau is not None and r < tau:
                continue                   # 没有相关证据 → 宁可不想起来
            row = self.by_id[mid]
            score = RELEVANCE_SCALE * r + self._prior(row, time_q)
            if self.parity and _DATE_RE.search(str(row["content"] or "")):
                score += DATE_BONUS
            scored.append((score, mid))
        scored.sort(reverse=True)
        if self.parity:
            scored = self._rerank(scored, query)
            # 拒答校准（C7）：分数低且挤在一起 → 这条查询没有真正对得上的
            if len(scored) >= 2:
                top = scored[0][0]
                third = scored[2][0] if len(scored) > 2 else top
                if top < ABSTAIN_TOP and (top - third) < ABSTAIN_GAP:
                    return []
        out: list[dict[str, Any]] = []
        for score, mid in scored[: (limit or self.k)]:
            out.append({
                "id": mid,
                "content": self.by_id[mid]["content"],
                "score": round(score, 4),
                "relevance": round(rel[mid], 4),
                "via": "recall",
            })
        return out

    def recall(self, query: str, limit: Optional[int] = None,
               tau: Optional[float] = None) -> list[dict[str, Any]]:
        """按综合分降序返回。

        tau 为空时就是"排序"（评测 Hit@k 用这一路）；
        给了 tau 就多一道证据门控：相关度低于 tau 的一律不返回，
        相当于系统决定"这件事我没印象，宁可不答"（评测拒答用这一路）。
        """
        return self.rank(self.relevance(query), limit=limit, tau=tau, query=query)


# ================================================================ 阈值标定

def calibrate(engine: RecallEngine) -> dict[str, Any]:
    """用校准题定拒答阈值 tau：在"有解/无解"两边取平衡准确率最高的那个切点。

    为什么不写死一个数：纯向量和融合的相关度分布量级不同，共用一个常数等于偏袒一方。
    为什么用平衡准确率：中文向量对"任何一句中文"都给 0.4 以上的底分，两边的分布是重叠的，
    没有哪个阈值能两全；只能选一个"该拒的拒掉、能答的尽量别误伤"的折中点，
    并把代价（误伤几道）如实报出来。18 道校准题不计入 50 题，也不进任何指标。
    """
    null_top: list[float] = []
    for q in CALIBRATION_NULL:
        rel = engine.relevance(q)
        null_top.append(max(rel.values()) if rel else 0.0)
    gold_top: list[float] = []
    for text, keys in CALIBRATION_GOLD:
        rel = engine.relevance(text)
        vals = [rel[_KEY_CACHE[k]] for k in keys if _KEY_CACHE[k] in rel]
        gold_top.append(max(vals) if vals else 0.0)

    points = sorted({round(x, 4) for x in null_top + gold_top})
    best: Optional[dict[str, float]] = None
    for i in range(len(points) - 1):
        t = (points[i] + points[i + 1]) / 2.0
        tnr = sum(1 for x in null_top if x < t) / len(null_top)    # 无解题被挡住的比例
        tpr = sum(1 for x in gold_top if x >= t) / len(gold_top)   # 有解题没被误伤的比例
        ba = (tnr + tpr) / 2.0
        # 平衡准确率打平时选更保守的那个（宁可多拒答）
        if best is None or ba > best["ba"] + 1e-9 or (
                abs(ba - best["ba"]) <= 1e-9 and tnr > best["tnr"]):
            best = {"tau": t, "ba": ba, "tnr": tnr, "tpr": tpr}
    if best is None:                     # 校准题全同分这种极端情况
        best = {"tau": 0.2, "ba": 0.0, "tnr": 0.0, "tpr": 0.0}
    return {
        "tau": round(max(0.02, best["tau"]), 4),
        "balanced_acc": round(best["ba"], 4),
        "tnr": round(best["tnr"], 4),
        "tpr": round(best["tpr"], 4),
        "null_max": round(max(null_top), 4),
        "gold_median": round(statistics.median(gold_top), 4),
        "separated": max(null_top) < min(gold_top),
    }


_KEY_CACHE: dict[str, int] = {}


def _key_id(engine: RecallEngine, key: str) -> int:
    """记忆键 → 行 id（建库时记下来的）。"""
    if not _KEY_CACHE:
        raise RuntimeError("记忆键映射还没初始化")
    return _KEY_CACHE[key]


# ================================================================ 指标

@dataclass
class QuestionResult:
    qid: str
    category: str
    query: str
    gold_ids: list[int]
    gold_keys: list[str]
    returned: list[dict[str, Any]]
    rank: Optional[int]
    abstain_ok: Optional[bool]
    top_score: float
    decoy_outranks_gold: Optional[bool]
    note: str
    top_relevance: float = 0.0       # 排序第一那条的相关度
    max_relevance: float = 0.0       # 候选池里最高的相关度（门控看的是这个）
    gated_out: bool = False          # 加上证据门控后，这题会不会被拒答

    @property
    def hit_at_1(self) -> bool:
        return self.rank == 1

    @property
    def hit_at_3(self) -> bool:
        return self.rank is not None and self.rank <= 3

    @property
    def hit_at_5(self) -> bool:
        return self.rank is not None and self.rank <= 5

    @property
    def rr(self) -> float:
        return 0.0 if self.rank is None else 1.0 / self.rank


def run_mode(engine: RecallEngine, questions: Sequence[Question], tau: float,
             k: int) -> dict[str, Any]:
    """跑一批题，返回逐题结果与汇总指标。"""
    results: list[QuestionResult] = []
    for q in questions:
        gold_ids = [_KEY_CACHE[key] for key in q.gold]
        # 相关度只算一次，排序和门控都用它
        rel = engine.relevance(q.query)
        # 排序一路：不加门控，纯看"该在的人有没有排上来"
        got = engine.rank(rel, limit=k)
        # 门控一路：加 tau，看系统会不会说"不记得"
        gated = engine.rank(rel, limit=k, tau=tau)
        ids = [g["id"] for g in got]
        rank = None
        for i, mid in enumerate(ids, start=1):
            if mid in gold_ids:
                rank = i
                break
        abstain_ok = (len(gated) == 0) if q.abstain else None
        decoy_outranks_gold = None
        if q.decoys:
            # 只统计"候选池里还在"的干扰项；已失效的旧记忆根本进不了候选池，
            # 那属于结构上就该挡住的东西，不该算成干扰胜出。
            decoy_ids = [_KEY_CACHE[d] for d in q.decoys
                         if d in _KEY_CACHE and _KEY_CACHE[d] in engine.by_id]
            decoy_ranks = [i for i, mid in enumerate(ids, start=1) if mid in decoy_ids]
            if decoy_ids:
                best = min(decoy_ranks) if decoy_ranks else None
                decoy_outranks_gold = best is not None and (rank is None or best < rank)
        results.append(QuestionResult(
            qid=q.qid, category=q.category, query=q.query,
            gold_ids=gold_ids, gold_keys=list(q.gold),
            returned=got, rank=rank, abstain_ok=abstain_ok,
            top_score=(got[0]["score"] if got else 0.0),
            top_relevance=(got[0]["relevance"] if got else 0.0),
            max_relevance=(max(rel.values()) if rel else 0.0),
            gated_out=(len(gated) == 0),
            decoy_outranks_gold=decoy_outranks_gold, note=q.note,
        ))

    def _agg(items: Sequence[QuestionResult]) -> dict[str, Any]:
        retr = [r for r in items if r.gold_keys]
        abst = [r for r in items if r.abstain_ok is not None]
        hit1 = sum(1 for r in retr if r.hit_at_1) / len(retr) if retr else 0.0
        hit3 = sum(1 for r in retr if r.hit_at_3) / len(retr) if retr else 0.0
        hit5 = sum(1 for r in retr if r.hit_at_5) / len(retr) if retr else 0.0
        mrr = sum(r.rr for r in retr) / len(retr) if retr else 0.0
        abst_acc = sum(1 for r in abst if r.abstain_ok) / len(abst) if abst else None
        decoys = [r for r in items if r.decoy_outranks_gold is not None]
        decoy_rate = (sum(1 for r in decoys if r.decoy_outranks_gold) / len(decoys)
                      if decoys else None)
                # 端到端：门控之后还答得对才算对（被门控挡掉的一律算错，这是安全阈值该付的钱）
        correct = (sum(1 for r in retr if r.hit_at_5 and not r.gated_out)
                   + sum(1 for r in abst if r.abstain_ok))
        # 证据门控误伤：本该能答的题，被 tau 判成"没印象"（代价要单独看见）
        gated_out = sum(1 for r in retr if r.gated_out)
        return {
            "n": len(items),
            "n_retrieval": len(retr),
            "n_abstain": len(abst),
            "hit1": round(hit1, 4),
            "hit3": round(hit3, 4),
            "hit5": round(hit5, 4),
            "mrr": round(mrr, 4),
            "abstain_acc": None if abst_acc is None else round(abst_acc, 4),
            "abstain_false_positive": len(abst) - sum(1 for r in abst if r.abstain_ok),
            "decoy_outranks_gold": None if decoy_rate is None else round(decoy_rate, 4),
            "gated_out": gated_out,
            "correct_rate": round(correct / len(items), 4) if items else 0.0,
        }

    by_cat = {c: _agg([r for r in results if r.category == c]) for c in CATEGORIES}
    overall = _agg(results)

    per_q = []
    for r in results:
        per_q.append({
            "qid": r.qid,
            "category": r.category,
            "query": r.query,
            "gold_ids": r.gold_ids,
            "gold_keys": r.gold_keys,
            "rank": r.rank,
            "hit5": r.hit_at_5 if r.gold_keys else None,
            "abstain_ok": r.abstain_ok,
            "decoy_outranks_gold": r.decoy_outranks_gold,
            "top_score": r.top_score,
            "top_relevance": r.top_relevance,
            "max_relevance": r.max_relevance,
            "gated_out": r.gated_out,
            "returned": [{"id": g["id"], "score": g["score"], "relevance": g["relevance"]}
                         for g in r.returned],
            "note": r.note,
        })
    return {"metrics": {"overall": overall, "by_category": by_cat}, "per_question": per_q}


# ================================================================ 输出

def _width(text: str) -> int:
    """按终端显示宽度算（中文占两格），否则表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: Any, width: int, right: bool = False) -> str:
    s = str(text)
    gap = max(0, width - _width(s))
    return (" " * gap + s) if right else (s + " " * gap)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           aligns: Optional[Sequence[str]] = None) -> str:
    aligns = list(aligns or ["l"] * len(headers))
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line]
    out.append("| " + " | ".join(_pad(h, widths[i], aligns[i] == "r")
                                 for i, h in enumerate(headers)) + " |")
    out.append(line)
    for row in rows:
        out.append("| " + " | ".join(_pad(c, widths[i], aligns[i] == "r")
                                     for i, c in enumerate(row)) + " |")
    out.append(line)
    return "\n".join(out)


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def metric_table(metrics: dict[str, Any], title: str) -> str:
    """把一整套指标排成一张表。"""
    rows = []
    for cat in CATEGORIES:
        m = metrics["by_category"][cat]
        if m["n_abstain"]:
            rows.append([cat, "-", "-", "-", "-", _pct(m["abstain_acc"]),
                         f"{m['gated_out']}/{m['n_retrieval']}" if m["n_retrieval"] else "-"])
        else:
            rows.append([cat, _pct(m["hit1"]), _pct(m["hit3"]), _pct(m["hit5"]),
                         f"{m['mrr']:.3f}", "-",
                         f"{m['gated_out']}/{m['n_retrieval']}"])
    o = metrics["overall"]
    rows.append(["总体", _pct(o["hit1"]), _pct(o["hit3"]), _pct(o["hit5"]),
                 f"{o['mrr']:.3f}", _pct(o["abstain_acc"]),
                 f"{o['gated_out']}/{o['n_retrieval']}"])
    rows.append(["端到端正确率", _pct(o["correct_rate"]), "", "", "", "", ""])
    body = _table(["类别", "Hit@1", "Hit@3", "Hit@5", "MRR@5", "拒答正确率", "门控误伤"], rows,
                  ["l", "r", "r", "r", "r", "r", "r"])
    return f"{title}\n{body}"


def compare_table(base: dict[str, Any], fuse: dict[str, Any]) -> str:
    """两模式差异表（融合 - 基线，单位为百分点）。"""
    rows = []
    for cat in list(CATEGORIES) + ["总体"]:
        b = base["overall"] if cat == "总体" else base["by_category"][cat]
        f = fuse["overall"] if cat == "总体" else fuse["by_category"][cat]
        cells = [cat]
        for key in ("hit1", "hit3", "hit5", "mrr", "correct_rate"):
            cells.append(f"{(f[key] - b[key]) * 100:+.1f}")
        ab = "" if b["abstain_acc"] is None else f"{(f['abstain_acc'] - b['abstain_acc']) * 100:+.1f}"
        cells.append(ab)
        rows.append(cells)
    return _table(["类别", "Hit@1", "Hit@3", "Hit@5", "MRR@5", "端到端正确率", "拒答正确率"],
                  rows, ["l", "r", "r", "r", "r", "r", "r"])


def sweep_table(per_question: Sequence[dict[str, Any]], title: str) -> str:
    """阈值扫描：tau 从松到紧，"拒答正确"和"误伤能答题"怎么此消彼长。

    拒答率永远是阈值调出来的，只看一个点会误以为"拒答已经 100% 了"。
    这张表告诉你在别的阈值上分别会付多少代价。
    """
    ab = [r for r in per_question if r["category"] == "拒答"]
    retr = [r for r in per_question if r["category"] != "拒答"]
    rows = []
    for i in range(1, 15):
        t = i * 0.05
        correct = sum(1 for r in ab if r["max_relevance"] < t)
        gated = sum(1 for r in retr if r["max_relevance"] < t)
        rows.append([f"{t:.2f}", f"{correct}/{len(ab)}", f"{gated}/{len(retr)}"])
    return title + "\n" + _table(["tau", "拒答正确", "误伤能答题"], rows, ["r", "r", "r"])


def rank_compare_table(modes: dict[str, Any], qids: Sequence[str], questions: dict[str, Question],
                       title: str = "逐题对比（V=纯向量，F=融合；数字是 gold 的排名，× 表示未进前 k）",
                       ) -> str:
    """把两种模式的逐题排名并排列出来，方便看"融合到底改了哪几题"。"""
    rows = []
    for qid in qids:
        q = questions[qid]
        cells = [qid, q.category]
        for mode in ("vector", "fuse"):
            row = modes.get(mode, {}).get("per_question") or []
            item = next((x for x in row if x["qid"] == qid), None)
            if item is None:
                cells.append("-")
            elif q.abstain:
                cells.append("拒答" if item["abstain_ok"] else "误召")
            else:
                cells.append(str(item["rank"]) if item["rank"] else "×")
        if len(cells) >= 4 and cells[2] != cells[3]:
            cells[0] = str(cells[0]) + " ●"
        rows.append(cells)
    return _table(["题号", "类别", "V", "F"], rows, ["l", "l", "r", "r"])


# ================================================================ 主流程

def resolve_embedder(prefer: str) -> Embedder:
    emb = Embedder(prefer)
    if not emb.dense:
        print("⚠️ 没有可用的向量模型，已退回启发式字符哈希向量（离线可跑，语义能力弱）。",
              file=sys.stderr)
        if emb.reason and emb.reason != "按参数强制使用启发式":
            print(f"   原因：{emb.reason}", file=sys.stderr)
    return emb


def run_synthetic(args: argparse.Namespace, stats: dict[str, Any]) -> int:
    """默认流程：临时库 + 50 题 + 两种召回。"""
    from whalegirl import fts

    tmpdir = Path(tempfile.mkdtemp(prefix="eval_memory_"))
    db_path = tmpdir / "eval_brain.db"
    _forbid_write(db_path)
    t0 = time.time()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        memories = _build_corpus()
        key_to_id = build_corpus_db(conn, memories)
        _KEY_CACHE.clear()
        _KEY_CACHE.update(key_to_id)
        n_fts = fts.rebuild(conn)

        embedder = resolve_embedder(args.provider)
        modes = ["vector", "fuse"] if args.auto else ["fuse"]
        questions = QUESTIONS
        report: dict[str, Any] = {
            "meta": {
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "corpus_db": "临时文件（评测结束即删除）",
                "memories": stats["memories"],
                "sessions": stats["sessions"],
                "fts_rows": n_fts,
                "questions": stats["questions"],
                "per_category": stats["per_category"],
                "superseded": stats["superseded"],
                "provider": embedder.name,
                "provider_dim": embedder.dim,
                "provider_dense": embedder.dense,
                "provider_reason": embedder.reason,
                "alpha": args.alpha,
                "k": args.k,
                "ref_now": datetime.fromtimestamp(REF_NOW).strftime("%Y-%m-%d %H:%M:%S"),
                "scoring": {
                    "relevance": f"(1-alpha)*sim_norm + alpha*kw_cov, alpha={args.alpha}",
                    "score": (f"{RELEVANCE_SCALE}*relevance + {W_IMPORTANCE}*importance"
                              f" + {W_RECENCY}*recency + {W_EMOTION}*emo_salience"),
                    "sim_norm": f"(cos - {embedder.base}) / {embedder.scale}",
                    "min_keyword_coverage": MIN_KEYWORD_COVERAGE,
                },
            },
            "modes": {},
        }

        for mode in modes:
            engine = RecallEngine(conn, embedder, mode, alpha=args.alpha, k=args.k,
                                  parity=bool(getattr(args, "parity", False)),
                                  route=bool(getattr(args, "route", False)))
            calib = calibrate(engine)
            tau_used = calib["tau"] if args.tau is None else float(args.tau)
            res = run_mode(engine, questions, tau=tau_used, k=args.k)
            report["modes"][mode] = {
                "label": "纯向量基线" if mode == "vector" else "向量+关键词融合",
                "threshold": dict(calib, tau_used=tau_used, manual=args.tau is not None),
                "metrics": res["metrics"],
                "per_question": res["per_question"],
            }

        if args.auto:
            b = report["modes"]["vector"]["metrics"]
            f = report["modes"]["fuse"]["metrics"]
            report["comparison"] = {
                "hit1_delta": round(f["overall"]["hit1"] - b["overall"]["hit1"], 4),
                "hit3_delta": round(f["overall"]["hit3"] - b["overall"]["hit3"], 4),
                "hit5_delta": round(f["overall"]["hit5"] - b["overall"]["hit5"], 4),
                "mrr_delta": round(f["overall"]["mrr"] - b["overall"]["mrr"], 4),
                "abstain_delta": (None if f["overall"]["abstain_acc"] is None
                                  else round(f["overall"]["abstain_acc"]
                                             - b["overall"]["abstain_acc"], 4)),
                "correct_rate_delta": round(
                    f["overall"]["correct_rate"] - b["overall"]["correct_rate"], 4),
            }
        report["meta"]["elapsed_s"] = round(time.time() - t0, 2)

        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0

        # ---------------- 人类可读输出 ----------------
        print("记忆召回评测 · 合成语料")
        print("=" * 78)
        print(f"语料：{stats['sessions']} 次对话 / {stats['memories']} 条记忆"
              f"（其中 {stats['superseded']} 条已被新事实替代）")
        print(f"题库：{stats['questions']} 道 —— " + "，".join(
            f"{c} {stats['per_category'][c]} 道" for c in CATEGORIES))
        print(f"向量：{embedder.name}" + (f"（{embedder.dim} 维）" if embedder.dim else "")
              + ("  [稠密模型]" if embedder.dense else "  [启发式兜底]"))
        print(f"基准时间：{report['meta']['ref_now']}；k={args.k}；alpha={args.alpha}")
        print(f"临时库：{db_path}（评测结束自动删除，正式库未触碰）")
        print()

        for mode in modes:
            m = report["modes"][mode]
            calib = m["threshold"]
            note = "" if calib["separated"] else "（校准题两边的分数有重叠，选的是平衡准确率最高的切点）"
            print(f"拒答阈值[{m['label']}]：tau={calib['tau']}"
                  f"（校准集：无解题挡住 {calib['tnr'] * 100:.0f}%，"
                  f"有解题保住 {calib['tpr'] * 100:.0f}%，平衡准确率 {calib['balanced_acc']:.2f}）{note}")
        print()
        for mode in modes:
            m = report["modes"][mode]
            print(metric_table(m["metrics"], f"【{m['label']}】"))
            o = m["metrics"]["overall"]
            print(f"  注：40 道检索题里，有 {o['gated_out']} 道会被证据门控判成"
                  f"「没印象」而拒答（这是提高拒答正确率的代价）；"
                  f"10 道拒答题里误召回 {o['abstain_false_positive']} 道。")
            print()
        if args.auto:
            print("【融合 - 基线 差值（百分点）】")
            print(compare_table(report["modes"]["vector"]["metrics"],
                                report["modes"]["fuse"]["metrics"]))
            print()

        if args.verbose:
            qmap = {q.qid: q for q in questions}
            print(rank_compare_table(report["modes"], [q.qid for q in questions], qmap))
            print("  ● = 两种模式排名不同；× = 没进前 k。")
            print()
        if args.sweep:
            for mode in modes:
                m = report["modes"][mode]
                print(sweep_table(m["per_question"],
                                  f"阈值扫描【{m['label']}】"
                                  f"（当前 tau={m['threshold']['tau']}）"))
                print()

        print(f"完成，用时 {report['meta']['elapsed_s']} 秒。")
        return 0
    finally:
        try:
            conn.close()  # noqa: F821  (try 里才定义，失败时忽略)
        except Exception:
            pass
        if args.keep_tmp:
            print(f"临时库已保留：{db_path}", file=sys.stderr)
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 真实库冒烟

SMOKE_QUERIES = [
    "最近在忙什么",
    "她喜欢吃什么",
    "上次说的那个游戏",
]


def run_smoke(args: argparse.Namespace) -> int:
    """对真实库做只读冒烟：临时 FTS 索引，不写库、不改 access_count。"""
    from whalegirl import fts

    db = Path(os.path.abspath(args.db))
    if not db.exists():
        print(f"找不到数据库：{db}", file=sys.stderr)
        return 2
    stat_before = (db.stat().st_size, db.stat().st_mtime_ns)
    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    t0 = time.time()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        if not cols:
            print("这个库里没有 memories 表。", file=sys.stderr)
            return 2
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        scope = conn.execute(
            "SELECT scope, COUNT(*) c FROM memories GROUP BY scope ORDER BY c DESC"
        ).fetchone()
        scope = scope["scope"] if scope else ""
        spk = conn.execute(
            "SELECT speaker_id, COUNT(*) c FROM memories WHERE scope=? AND status='active'"
            " GROUP BY speaker_id ORDER BY c DESC", (scope,)).fetchone()
        speaker = str(spk["speaker_id"]) if spk else ""

        # 临时的 FTS 索引：只存在于 temp schema，主库一个字节都不写。
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{fts.TABLE} USING fts5("
            "body, scope UNINDEXED, speaker_id UNINDEXED, status UNINDEXED,"
            " tokenize='unicode61')")
        n_index = fts.rebuild(conn)

        rows = conn.execute(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " AND (speaker_id=? OR speaker_id='') ORDER BY updated_at DESC",
            (scope, speaker)).fetchall()
        embedder = resolve_embedder(args.provider)
        print("真实库只读冒烟测试")
        print("=" * 78)
        print(f"库：{db}")
        print(f"记忆：{total} 条（active {active} 条）；scope={scope}；"
              f"speaker={speaker or '(空)'}；FTS 临时索引 {n_index} 条")
        print(f"向量：{embedder.name}" + (f"（{embedder.dim} 维）" if embedder.dim else ""))
        print("说明：本次只读打开，临时索引建在 temp schema，不执行任何 UPDATE。")
        print()

        for mode in (["vector", "fuse"] if args.auto else ["fuse"]):
            engine = _SmokeEngine(conn, embedder, mode, rows, args.alpha, args.k)
            print(f"【{'纯向量基线' if mode == 'vector' else '向量+关键词融合'}】")
            for q in SMOKE_QUERIES:
                got = engine.recall(q, tau=args.smoke_tau)
                if not got:
                    print(f"  「{q}」→ 无结果（低于阈值 {args.smoke_tau}）")
                    continue
                top = got[0]
                print(f"  「{q}」→ #{top['id']} score={top['score']} "
                      f"{top['content'][:46].replace(chr(10), ' ')}")
            print()

        stat_after = (db.stat().st_size, db.stat().st_mtime_ns)
        same = stat_before == stat_after
        print(f"只读校验：文件大小/修改时间{'未变 ✅' if same else '变了 ❌'}，"
              f"耗时 {time.time() - t0:.2f} 秒。")
        return 0 if same else 1
    finally:
        conn.close()


class _SmokeEngine(RecallEngine):
    """真实库版：候选集不再按评测语料筛，直接吃传进来的 rows。"""

    def __init__(self, conn, embedder, mode, rows, alpha, k):
        from whalegirl import fts

        self.fts = fts
        self.conn = conn
        self.emb = embedder
        self.mode = mode
        self.alpha = float(alpha)
        self.k = int(k)
        self.rows = rows
        self.by_id = {int(r["id"]): r for r in rows}
        self.n_docs = len(rows)
        self._vec = {int(r["id"]): embedder.doc(r["content"]) for r in rows}
        self._tok = {int(r["id"]): set(fts.to_tokens(r["content"])) for r in rows}
        self._df = {}

    def _keyword_scores(self, query: str) -> list[tuple[int, float]]:
        if self.mode == "vector":
            return []
        # 真实库的 scope 不一定是评测 scope，交给 fts 自己按全部候选过滤
        hits = self.fts.search(self.conn, query, k=max(20, self.n_docs),
                               active_only=True)
        if not hits:
            return []
        cov = self._coverage(query)
        allowed = set(self.by_id)
        return [(int(mid), round(float(sc) * cov.get(int(mid), 0.0), 4))
                for mid, sc in hits
                if int(mid) in allowed and cov.get(int(mid), 0.0) >= MIN_KEYWORD_COVERAGE]


# ================================================================ 入口

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="记忆召回评测：合成语料 + 50 题，对比纯向量与向量+关键词融合。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--auto", action="store_true", help="同时跑纯向量基线与融合，并出对比表")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON（客户端只读这一份）")
    p.add_argument("--db", default="", help="改用真实库做只读冒烟测试（不会写入任何一行）")
    p.add_argument("--k", type=int, default=5, help="召回条数上限，默认 5")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="关键词权重，默认 0.3")
    p.add_argument("--parity", action="store_true",
                   help="按生产规则打分：时间感知 + 重排 + 拒答")
    p.add_argument("--route", action="store_true",
                   help="推理型查询只走语义通道，关键词不参与")
    p.add_argument("--provider", choices=["auto", "heuristic"], default="auto",
                   help="向量来源：auto=优先本地模型，heuristic=强制哈希兜底")
    p.add_argument("--tau", type=float, default=None,
                   help="手动指定拒答阈值，覆盖自动标定（只想看排序时可以给 0）")
    p.add_argument("--smoke-tau", type=float, default=0.10, help="冒烟测试用的相关度阈值")
    p.add_argument("--verbose", action="store_true", help="打印逐题明细（两模式并排）")
    p.add_argument("--sweep", action="store_true", help="打印拒答阈值扫描表（松紧不同的代价）")
    p.add_argument("--keep-tmp", action="store_true", help="保留临时库以便手工检查")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 输出统一 UTF-8：管道/重定向到文件时也不会变成乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args(argv)
    if args.k < 1:
        print("--k 必须 ≥ 1", file=sys.stderr)
        return 2
    if not (0.0 <= args.alpha <= 1.0):
        print("--alpha 必须在 0~1 之间", file=sys.stderr)
        return 2
    if args.db:
        return run_smoke(args)
    try:
        stats = validate()
    except AssertionError as exc:
        print(f"题库自检失败：{exc}", file=sys.stderr)
        return 2
    return run_synthetic(args, stats)


if __name__ == "__main__":
    raise SystemExit(main())
