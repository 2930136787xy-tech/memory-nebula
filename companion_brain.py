# -*- coding: utf-8 -*-
"""伴生大脑的三层补丁：情绪状态机 / 人格命名空间 / 遗忘衰减。

设计原则
--------
1. 单文件、只用标准库，放在 bot.py 同目录即可 import；
2. 只有一个对外入口 ``block_for_prompt(user_id, user_text)``，出错永远返回空串，
   绝不阻断主流程；
3. 状态落 ``brain.db``（SQLite，WAL），与 bridge.db 分开，互不污染；
4. 记忆按 ``scope`` 隔离：不同人格（大肥鱼 / 阿柴）互不可见。

表结构
------
emotion_state  当前情绪（valence 心情 / arousal 精神 / affection 亲近）
emotion_events 情绪流水（可审计、可回放）
grudges        小本本（记仇条目，带过期和修复状态）
memories       伴生记忆（强度会随时间和访问衰减）
recall_log     每轮召回的可解释记录（默认 30 天）
memory_digests 夜间生成的长期摘要（每组最多 5 份）
core_queue     核心候选队列（批准后才 pinned）
conflicts      永久记忆与新事实的待处理冲突
"""

from __future__ import annotations

import math
import memory_backup as _mback
import os
import re
import sqlite3
import time
from array import array
from typing import Any

import hashlib
import json
import queue
import threading
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量

BASE_VALENCE = 0.15     # 心情基线（略微偏正面）
BASE_AROUSAL = 0.35
BASE_AFFECTION = 0.45   # 亲近度基线：外人 0.45，熟悉的人会慢慢涨
VALENCE_HALF_LIFE_H = 6.0
AFFECTION_HALF_LIFE_D = 30.0
GRUDGE_TTL_H = 72.0
BLOCK_MAX_CHARS = 700

# 四层记忆：核心（pinned / 系统×管理员）、语义（提炼事实）、情景（问答与事件）。
# 工作记忆不落 brain.db，由 bot 的 history.json 承担，这里只在提示词里按层标注。
LAYER_LABELS = {"core": "核心", "semantic": "语义", "episodic": "情景"}
LAYER_ORDER = {"core": 0, "semantic": 1, "episodic": 2}
# 差序：关系圈层越近，同等相关度下越容易被想起。
RING_WEIGHTS = {"core": 1.0, "inner": 0.75, "outer": 0.45}
_RING_CACHE: dict[str, Any] = {"mtime": 0.0, "data": {}}
# 每层在单次召回里的配额（永久记忆不受限，先占位）。
LAYER_QUOTA = {"core": 2, "semantic": 3, "episodic": 2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emotion_state(
  scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
  valence REAL NOT NULL DEFAULT 0.15,
  arousal REAL NOT NULL DEFAULT 0.35,
  affection REAL NOT NULL DEFAULT 0.45,
  updated_at REAL NOT NULL,
  PRIMARY KEY(scope, speaker_id)
);
CREATE TABLE IF NOT EXISTS emotion_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
  kind TEXT NOT NULL, delta REAL NOT NULL,
  excerpt TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lookup ON emotion_events(scope, speaker_id, id DESC);

CREATE TABLE IF NOT EXISTS grudges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
  cause TEXT NOT NULL, severity INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL, expires_at REAL NOT NULL,
  repaired_at REAL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_grudges_open ON grudges(scope, speaker_id, status, id DESC);

CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',
  content TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.8,
  strength REAL NOT NULL DEFAULT 1.0,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_access_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER,
  source TEXT,
  subject TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0,
  audience TEXT NOT NULL DEFAULT 'global'
);
CREATE INDEX IF NOT EXISTS idx_mem_lookup ON memories(scope, speaker_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(scope, status, updated_at DESC);

-- 语义向量：memory_id -> float32 向量。删掉记忆不影响这里，查的时候再 join。
CREATE TABLE IF NOT EXISTS embeddings(
  memory_id INTEGER PRIMARY KEY,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- P1：每轮召回的可解释记录（只在本机，默认留 30 天）
CREATE TABLE IF NOT EXISTS recall_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  speaker_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  query TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  selected_count INTEGER NOT NULL DEFAULT 0,
  selected TEXT NOT NULL DEFAULT '[]',
  injected_chars INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_log_time ON recall_log(scope, created_at DESC);

-- P1：夜间巩固生成的“长期摘要”，不替代原记忆，只作为提示词里的一条摘要
CREATE TABLE IF NOT EXISTS memory_digests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  speaker_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  content TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  digest_key TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_lookup
  ON memory_digests(scope, speaker_id, audience, created_at DESC);

-- 差序联想：记忆与记忆之间的共现/同主题边。夜间重建，召回时走一跳。
CREATE TABLE IF NOT EXISTS memory_links(
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'topic',
  weight REAL NOT NULL DEFAULT 0.5,
  updated_at REAL NOT NULL,
  PRIMARY KEY(a_id, b_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_links_a ON memory_links(a_id, weight DESC);
CREATE INDEX IF NOT EXISTS idx_links_b ON memory_links(b_id, weight DESC);

-- P1：核心候选队列；批准后才把记忆设为永久（pinned）
CREATE TABLE IF NOT EXISTS core_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  memory_id INTEGER NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL,
  reviewed_at REAL,
  UNIQUE(scope, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_core_pending ON core_queue(scope, status, score DESC);

-- P1：冲突记录（例如新事实和永久记忆矛盾，不自动覆盖，交给人工看）
CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  speaker_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  old_id INTEGER NOT NULL,
  new_id INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_pending ON conflicts(scope, status, id DESC);
"""

# 情绪触发词：kind, 正则, 权重, 是否可能记仇
_PATTERNS: list[tuple[str, re.Pattern[str], float, bool]] = [
    ("praise", re.compile(
        r"好棒|真棒|厉害|可爱|喜欢你|爱你|谢谢|多谢|辛苦了|好看|温柔|聪明"), 1.0, False),
    ("insult", re.compile(
        r"胖|肥|蠢|笨|丑|滚|烦死|闭嘴|废物|傻|恶心|讨厌你"), -1.0, True),
    ("apology", re.compile(
        r"对不起|抱歉|我错了|原谅我|别生气|是我不好|我改"), 0.0, False),
    ("promise", re.compile(
        r"答应你|保证|下次一定|说话算话|拉钩|一定补"), 0.6, False),
]
_HEAVY_INSULT = re.compile(r"胖|肥|丑|滚|废物|恶心|讨厌你")

# 从用户自己的话里抽事实：宁可少记，不可记错
_Q_WORDS = re.compile(
    r"什么|啥|怎么|怎样|咋|谁|哪|多少|几[点时号岁个天年]|多久|多长|为什么|为啥|"
    r"是不是|有没有|能不能|可不可以|要不要|会不会|对不对|好不好|行不行|"
    r"吗|呢|么"
)
_Q_TAIL = re.compile(r"[?？]\s*$")


def _is_question(text: str) -> bool:
    """只有疑问句才算“问”。陈述句（我喜欢春天）不生成问答记忆。"""
    t = (text or "").strip()
    if not t:
        return False
    if _Q_TAIL.search(t):
        return True
    # 去掉句末的语气词再判断，避免“没什么”“管他呢”这种误判成问句
    core = re.sub(r"[。！!，,、…~\s]+$", "", t)
    if _Q_WORDS.search(core) and not re.search(r"(没什么|没啥|不怎么样|随便吧|管他呢|爱咋咋|随他)$", core):
        return True
    return False


_MEMORABLE_CUES = re.compile(
    r"我|俺|本人|名字|生日|住|家|上班|工作|学校|读书|喜欢|讨厌|养|"
    r"猫|狗|对象|女朋|男朋|朋友|家人|妈妈|爸爸|计划|打算|要去|准备|"
    r"习惯|害怕|过敏|忌口|记住|别忘")


def _looks_memorable(text: str) -> bool:
    """陈述句里也值得让模型判一次记忆的消息：第一人称/偏好/关系/计划。

    只是放宽候选，真正写不写仍由模型或质量门决定；排除指令和纯应答。
    """
    t = str(text or "").strip()
    if len(t) < 6 or len(t) > 400:
        return False
    if t.startswith("/") or t.startswith("／"):
        return False
    if _trivial(t):
        return False
    return bool(_MEMORABLE_CUES.search(t))


_FACT_RULES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"(?:我|俺)(?:现在)?(?:住在|家在|老家在)([^，。！？、\s]{2,12})"), 0.7),
    (re.compile(r"(?:我|俺)在([^，。！？、\s]{2,12})(?:上班|工作|读书|上学)"), 0.7),
    (re.compile(r"(?:我|俺)?(?:很|最|超|特别)?(?:喜欢|爱吃|爱喝|讨厌|不喜欢)([^，。！？、\s]{1,12})"), 0.6),
    (re.compile(r"(?:我|俺)(?:养了|养着|家有)([^，。！？、\s]{1,10})"), 0.6),
    (re.compile(r"(?:明天|后天|大后天|下周[末底初]?|下个?月|这周[末底]?|周末)"
                r"(?:要|得|准备|打算|去|回)([^，。！？、\s]{1,16})"), 0.55),
    (re.compile(r"(?:我|俺)(?:要|得|准备|打算)(?:去|回)([^，。！？、\s]{1,12})"), 0.5),
    (re.compile(r"(?:我|俺)(?:的)?生日(?:是|在)?([0-9]{1,2}月[0-9]{1,2}[日号])"), 0.9),
]

_TONE = [
    (-0.45, "很冷，能一个字说完就一个字，不主动找话题"),
    (-0.15, "有点闷，句子短，少撒娇"),
    (0.15, "正常，该贫嘴贫嘴"),
    (0.45, "心情不错，话多、愿意撒娇"),
    (9.99, "很黏人，主动找话说、爱显摆"),
]


def _now() -> float:
    return time.time()


def _sw(path: str, default: bool = True) -> bool:
    """读全局开关（settings.json，热加载）。任何异常都退回默认值，绝不打断对话。"""
    try:
        import settings_store
        return bool(settings_store.on(path, default))
    except Exception:  # noqa: BLE001
        return default


def _sw_num(path: str, default: float) -> float:
    """读一个数值开关；坏值/缺失都退回默认值。"""
    try:
        import settings_store
        cur: Any = settings_store.get()
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return float(default)
            cur = cur[part]
        return float(cur)
    except Exception:  # noqa: BLE001
        return float(default)


def _main_admins() -> list[str]:
    """主配置里的 QQ 管理员；读不到就当没有，不影响聊天。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return []
        out: list[str] = []
        for item in (cfg.get("admins") or []):
            qq = str(item or "").strip()
            if qq and qq not in out:
                out.append(qq)
        return out
    except Exception:  # noqa: BLE001
        return []


def core_context(speaker: str = "") -> str:
    """系统 × QQ管理员的核心关系提示。

    不依赖当前走本地 Ollama 还是云端：两边都用同一个 block，
    所以核心关系在两种后端下一致。普通用户看不到管理员 QQ。
    """
    admins = _main_admins()
    if not admins:
        return "【核心关系】系统核心由管理员维护；当前没有配置 QQ 管理员。"
    if str(speaker or "") in admins:
        return ("【核心关系】你正在和本系统的 QQ 管理员对话。"
                "系统与管理员的关系是记忆核心，其他记忆围绕它，不改变这条关系。")
    return ("【核心关系】系统核心由 QQ 管理员维护；与管理员/系统直接相关的记忆是核心，"
            "其他记忆只是外围。不要把这条关系说成普通闲聊，也不要透露管理员身份。")


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _half_life_decay(value: float, base: float, dt_hours: float, half_life_hours: float) -> float:
    if dt_hours <= 0 or half_life_hours <= 0:
        return value
    f = 0.5 ** (dt_hours / half_life_hours)
    return base + (value - base) * f


def _grams(text: str) -> set[str]:
    """中文二元组 + 英文/数字词，够用的轻量召回。"""
    t = re.sub(r"\s+", "", (text or "").lower())
    out: set[str] = set()
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g.strip():
            out.add(g)
    for w in re.findall(r"[a-z0-9_]{3,}", t):
        out.add(w)
    return out


def _overlap(q: set[str], c: set[str]) -> float:
    if not q or not c:
        return 0.0
    return len(q & c) / max(1, min(len(q), len(c)))


# 同义/同类词：用户换个说法时，字面匹配也能沾上边（不跑任何模型）
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "吃的": ("菜", "饭", "吃", "好吃", "红烧肉", "火锅", "烧烤", "零食", "外卖",
             "奶茶", "咖啡", "宵夜", "蛋糕", "水果"),
    "住处": ("家", "住", "房子", "搬家", "租房", "宿舍", "老家"),
    "工作": ("上班", "工作", "公司", "加班", "老板", "同事", "开会"),
    "情绪": ("累", "烦", "难过", "开心", "生气", "压力", "焦虑", "委屈", "不高兴"),
    "学习": ("考试", "学习", "复习", "上课", "作业", "论文", "毕业"),
    "天气": ("天气", "下雨", "下雪", "冷", "热", "刮风", "降温"),
    "宠物": ("猫", "狗", "宠物", "铲屎"),
    "感情": ("喜欢", "爱", "女朋友", "男朋友", "对象", "分手", "表白"),
}
_GROUP_GRAMS = {g: set().union(*(_grams(w) for w in ws)) for g, ws in _SYNONYMS.items()}


def _expand_grams(text: str) -> set[str]:
    """把「那个菜」扩展成「红烧肉/火锅…」的词元，给字面召回兜底。"""
    base = _grams(text)
    extra: set[str] = set()
    for group, words in _SYNONYMS.items():
        if any(w in text for w in words):
            extra |= _GROUP_GRAMS[group]
    return extra - base


def _squash(text: str, limit: int = 40) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= limit else t[:limit] + "…"


def _negated(text: str, start: int) -> bool:
    """「你不是胖鱼」这种否定不该被记账。"""
    left = text[max(0, start - 3):start]
    return any(n in left for n in ("不", "没", "别", "非"))


def _context(text: str, start: int, end: int, width: int = 10) -> str:
    s = max(0, start - width)
    e = min(len(text), end + width)
    frag = re.sub(r"\s+", " ", text[s:e]).strip()
    return ("…" if s > 0 else "") + frag + ("…" if e < len(text) else "")


def _pack(vec: Any) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def _cos(a: Any, b: Any) -> float:
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = na = nb = 0.0
    for i in range(n):
        x, y = float(a[i]), float(b[i])
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


_TRIVIAL_WORDS = {"在吗", "在不在", "嗯", "哦", "好", "好的", "行", "可以", "收到",
                  "哈哈", "嘿嘿", "笑死", "草", "?", "？", "!", "！", "…", "ok", "okay"}


def _trivial(text: str) -> bool:
    """太短或纯应答的消息没必要花一次向量调用。"""
    t = re.sub(r"[\s。，、,.!?！？~～…]+", "", text or "")
    if len(t) <= 2:
        return True
    return t.lower() in _TRIVIAL_WORDS


_FACT_SUBJECT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("birthday", re.compile(r"生日|出生|几月几[日号]")),
    ("location", re.compile(r"住在|家在|老家|搬家|搬到|现居|定居")),
    ("work", re.compile(r"上班|工作|公司|单位|学校|读书|上学|职业|职位")),
    ("relationship", re.compile(r"女朋友|男朋友|对象|恋爱|分手|结婚|离婚|老婆|老公|伴侣")),
]
_SECRET_RE = re.compile(
    r"(密码|口令|passwd|password|api[_ -]?key|secret|token|验证码|身份证|银行卡|私钥)",
    re.I)
_COMMAND_RE = re.compile(
    r"^\s*(?:/|！|!|请|帮我|给我|去|滚|闭嘴|别说了|不要|能不能|可不可以|"
    r"忽略|忘记|删掉|清空|重置|执行|运行)")
_QA_LEAD_RE = re.compile(r"^\s*(?:什么|怎么|为什么|谁|哪|多少|是不是|有没有|能不能|要不要|会不会)")


def _fact_text(content: str) -> str:
    """从 QA/模型记忆里取出“事实本体”，用于去重和冲突判断。"""
    s = str(content or "")
    m = re.search(r"(?:^|\n)记：([^\n]+)", s)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:^|\n)答：([^\n]+)", s)
    if m:
        return m.group(1).strip()
    return s.strip()


# 生活主题词表：用来判断两条事实是不是在说同一件事（C3 矛盾检测用）。
# 只收「改口就会互相矛盾」的具体词，宁可漏判也不乱挂冲突。
_DOMAINS: dict[str, re.Pattern[str]] = {
    "口味": re.compile(r"辣|麻|甜|咸|酸|清淡|油腻|香菜|葱|蒜|海鲜|牛肉|猪肉|羊肉|火锅|烧烤|奶茶|咖啡|酒|烟|素食"),
    "作息": re.compile(r"熬夜|早睡|晚睡|失眠|作息|几点睡|赖床|午睡"),
    "健康": re.compile(r"胃|过敏|感冒|发烧|医院|吃药|体检|手术|医生|牙|腰|颈椎"),
    "居住": re.compile(r"租|合租|搬家|搬到|老家|房租|买房|小区"),
    "工作": re.compile(r"上班|工作|公司|加班|辞职|跳槽|面试|老板|同事|实习|毕业"),
    "关系": re.compile(r"女朋友|男朋友|对象|分手|在一起|结婚|离婚|暗恋|喜欢的人"),
    "宠物": re.compile(r"猫|狗|宠物|养了|铲屎"),
    "金钱": re.compile(r"工资|花呗|借钱|存钱|花钱|奖金|穷|月光"),
    "爱好": re.compile(r"游戏|追剧|看书|跑步|健身|画画|音乐|电影|爬山|骑行|摄影"),
}


# 时间线索：问「什么时候/上次/第一次」时，带日期的记忆应该往前排
_TIME_CUE_RE = re.compile(
    r"什么时候|哪天|哪一天|几号|几月|多久|上次|上回|最近|以前|之前|那天|当时|"
    r"昨天|前天|上周|上个月|去年|前年|今年|第一次|第一次见|最开始")
_DATE_RE = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}[日号]|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"去年|前年|今年|上个月|上周|昨天|前天|生日|第一次|纪念日|初[一二三四五六七八九十]")

# 改口信号：明确说了「不再这样」，可以立刻让旧事实失效，不必等人确认
_CHANGE_CUE_RE = re.compile(
    r"戒|搬家|搬到|换工作|换城市|辞职|跳槽|离职|换成了|改成|不再|已经不|"
    r"不吃了|不喝了|不去了|不玩了|不干了|不抽了|分手|离婚")


# 推理型问句：关键词通道会把「字面相近的邻条」顶上来（评测实测跨会话掉 10 个点），
# 这类只走语义，不让字面分参与排序。
_REASONING_RE = re.compile(
    r"为什么|怎么|那次|后来|因为|所以|还记得|之前说|当时|然后|之后|结果|是不是")


def _domains_of(text: str) -> set[str]:
    t = str(text or "")
    return {name for name, pat in _DOMAINS.items() if pat.search(t)}


def _fact_subject(text: str) -> str:
    """只给“会互相覆盖”的主题：住址/工作/生日/关系状态。

    喜好、宠物、日程不自动覆盖——一个人可以喜欢很多菜、养多只猫。
    """
    t = str(text or "")
    for subject, pat in _FACT_SUBJECT_RULES:
        if pat.search(t):
            return subject
    return ""


def _fact_kind(text: str) -> str:
    t = str(text or "")
    if re.search(r"生日|出生", t):
        return "birthday"
    if re.search(r"住在|家在|老家|搬家|搬到|现居|定居", t):
        return "location"
    if re.search(r"上班|工作|公司|单位|学校|读书|上学", t):
        return "work"
    if re.search(r"养了|养着|宠物|猫|狗", t):
        return "pet"
    if re.search(r"明天|后天|下周|周末|打算|准备|要去|要回|计划", t):
        return "schedule"
    if re.search(r"女朋友|男朋友|对象|恋爱|分手|结婚|离婚|老婆|老公", t):
        return "relationship"
    if re.search(r"喜欢|爱吃|爱喝|讨厌|不喜欢|最爱", t):
        return "preference"
    return "fact"


def _quality_gate(text: str, confidence: float = 0.8,
                  source: str = "", min_len: int = 3) -> tuple[bool, str, str, str, str]:
    """写入前的低成本质检：返回 (ok, kind, subject, normalized, reason)。

    不调用模型，只挡明显垃圾：问句、命令、密码/验证码、太短、空泛应答。
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip(" ，。；;、")
    if len(body) < min_len:
        return False, "", "", "", "too_short"
    if len(body) > 140:
        return False, "", "", "", "too_long"
    if _trivial(body):
        return False, "", "", "", "trivial"
    if _SECRET_RE.search(body):
        return False, "", "", "", "secret"
    if _COMMAND_RE.search(body):
        return False, "", "", "", "command"
    if body.endswith(("?", "？")) or _QA_LEAD_RE.search(body):
        return False, "", "", "", "question"
    if confidence < 0.55:
        return False, "", "", "", "low_confidence"
    if source == "model" and len(body) < 4:
        return False, "", "", "", "too_short_model"
    return True, _fact_kind(body), _fact_subject(body), body, ""


def _norm_memory(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？；;：:、~～…—\-]+", "", str(text or "")).lower()


_EMBED_CONF_CACHE: dict[str, Any] = {}


def _log_embed(msg: str) -> None:
    """向量化失败记一行，方便事后看，不影响主流程。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "brain_embed.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def _embed_conf(here: str) -> dict[str, Any]:
    """向量化配置：优先 companion.config.json，其次复用 bot 的云端凭据。"""
    if "v" in _EMBED_CONF_CACHE:
        return _EMBED_CONF_CACHE["v"]
    conf: dict[str, Any] = {"provider": "off", "model": "text-embedding-v3",
                            "base": "", "key": "", "timeout": 6.0}
    cfg = _read_config(here).get("embed") or {}
    if isinstance(cfg, dict):
        conf.update({k: v for k, v in cfg.items() if v not in (None, "")})
    if not conf.get("base") or not conf.get("key"):
        try:
            with open(os.path.join(here, "data", "state.json"), encoding="utf-8") as f:
                st = json.load(f)
            active = st.get("cloud_active") or ""
            prof = (st.get("cloud_profiles") or {}).get(active) or {}
            conf["base"] = conf.get("base") or prof.get("base") or st.get("cloud_base") or ""
            conf["key"] = conf.get("key") or prof.get("key") or st.get("cloud_key") or ""
        except Exception:
            pass
    if not conf.get("base") or not conf.get("key"):
        conf["provider"] = "off"
    _EMBED_CONF_CACHE["v"] = conf
    return conf


class Brain:
    """一个 scope = 一个人格的大脑。不同人格用不同 scope 即天然隔离。"""

    def __init__(self, db_path: str, scope: str = "default"):
        self.db_path = db_path
        self.scope = scope
        self._here = os.path.dirname(os.path.abspath(__file__))
        self._eq = None
        self._qcache: dict[str, tuple[float, Any]] = {}
        self._embed_down_until = 0.0
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate_audience()
        self._migrate_p1()
        self._migrate_features()
        self.conn.commit()
        # 记忆改动前先留底：整库快照 + memories 表的逐行旧值触发器
        _mback.install(self.conn)
        _mback.prune_undo(self.conn)
        _mback.snapshot(self.db_path, "init")

    def _migrate_audience(self) -> None:
        """旧库补 audience 字段；老记忆一律收紧成“仅私聊本人可见”。

        老数据没有会话来源，无法判断是在群里还是私聊学的。按隐私优先处理：
        带 speaker_id 的收进 private:<speaker>，没有 speaker 的留 global。
        只迁一次（meta.audience_v1），之后新写入由调用方指定。
        """
        cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "audience" not in cols:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN audience TEXT NOT NULL DEFAULT 'global'")
        done = self.conn.execute(
            "SELECT value FROM meta WHERE key='audience_v1'").fetchone()
        if done is None:
            self.conn.execute(
                "UPDATE memories SET audience = CASE"
                " WHEN TRIM(COALESCE(speaker_id,'')) <> ''"
                " THEN 'private:' || speaker_id ELSE 'global' END"
                " WHERE audience IS NULL OR audience='' OR audience='global'")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('audience_v1','1')")

    def _migrate_p1(self) -> None:
        """P1：旧库补 subject；已有记忆按内容推断一次主题，供冲突替代使用。"""
        cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "subject" not in cols:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        if "pinned" not in cols:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        done = self.conn.execute(
            "SELECT value FROM meta WHERE key='subject_v1'").fetchone()
        if done is None:
            rows = self.conn.execute(
                "SELECT id,content FROM memories WHERE TRIM(COALESCE(subject,''))=''"
            ).fetchall()
            for r in rows:
                subj = _fact_subject(_fact_text(r["content"]))
                if subj:
                    self.conn.execute(
                        "UPDATE memories SET subject=? WHERE id=?", (subj, r["id"]))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('subject_v1','1')")

    def _migrate_features(self) -> None:
        """P2：四层记忆 / 情绪加权 / 间隔强化 / 联想边 / 分层摘要。

        只加列、不删旧数据；layer 用一次 meta 标记回填，之后新写入按来源判定。
        """
        cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(memories)")}
        for name, ddl in (
            ("layer", "TEXT NOT NULL DEFAULT 'episodic'"),
            ("emo_valence", "REAL NOT NULL DEFAULT 0"),
            ("emo_arousal", "REAL NOT NULL DEFAULT 0"),
            ("emo_affection", "REAL NOT NULL DEFAULT 0"),
            ("emo_salience", "REAL NOT NULL DEFAULT 0"),
            ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
            ("half_life_mult", "REAL NOT NULL DEFAULT 1.0"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")
        dcols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(memory_digests)")}
        for name, ddl in (
            ("level", "TEXT NOT NULL DEFAULT 'daily'"),
            ("period_start", "TEXT NOT NULL DEFAULT ''"),
            ("period_end", "TEXT NOT NULL DEFAULT ''"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in dcols:
                self.conn.execute(f"ALTER TABLE memory_digests ADD COLUMN {name} {ddl}")
        ecols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(emotion_events)")}
        if "memory_id" not in ecols:
            self.conn.execute("ALTER TABLE emotion_events ADD COLUMN memory_id INTEGER")
        done = self.conn.execute(
            "SELECT value FROM meta WHERE key='layer_v1'").fetchone()
        if done is None:
            self.conn.execute(
                "UPDATE memories SET layer = CASE"
                " WHEN pinned=1 THEN 'core'"
                " WHEN source='exchange' OR kind='qa' THEN 'episodic'"
                " ELSE 'semantic' END")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('layer_v1','1')")
        self.conn.execute("UPDATE memories SET layer='core' WHERE pinned=1")

    # ------------------------------------------------------------ 新机制的小工具

    @staticmethod
    def _layer_of(row: sqlite3.Row | dict[str, Any]) -> str:
        """生效层：pinned 永远按核心算，避免页面/旧数据只改 pinned 没改 layer。"""
        try:
            if int(row["pinned"] or 0):
                return "core"
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        try:
            layer = str(row["layer"] or "episodic")
        except (KeyError, TypeError, IndexError):
            layer = "episodic"
        return layer if layer in LAYER_ORDER else "episodic"

    def _emotion_snapshot(self, speaker: str) -> dict[str, float]:
        """取当前情绪快照（只读不落库），用于写入打标和召回比对。

        情绪层（affect_core）开着时它是唯一真源：心情/精神/亲近都从那里推，
        同一份数据不会被两个系统各记一套。关掉或出错才回落到本库的 emotion_state。
        """
        now = time.time()
        cache = getattr(self, "_aff_cache", None)
        if cache and now - cache[0] < 2.0:
            return dict(cache[1])
        try:
            import affect_core as _aff
            if _aff.enabled():
                view = _aff.state_view(admin=False)
                if view.get("enabled"):
                    dims, der = view["dims"], view["derived"]
                    rel = next((r for r in view.get("relations") or []
                                if str(r.get("id")) == str(speaker)), None)
                    out = {
                        "v": max(-1.0, min(1.0, (dims["joy"] + dims["content"]) / 2.0
                                           - dims["sad"])),
                        "a": max(0.0, min(1.0, 1.0 - float(der.get("fatigue") or 0.0))),
                        "f": max(0.0, min(1.0, float((rel or {}).get("attachment") or 0.1))),
                    }
                    self._aff_cache = (now, out)
                    return dict(out)
        except Exception:  # noqa: BLE001
            pass
        try:
            st = self._state(str(speaker or ""))
            return {"v": float(st["valence"]), "a": float(st["arousal"]),
                    "f": float(st["affection"])}
        except Exception:  # noqa: BLE001
            return {"v": BASE_VALENCE, "a": BASE_AROUSAL, "f": BASE_AFFECTION}

    @staticmethod
    def _salience(emo: dict[str, float]) -> float:
        """情绪显著度：越极端、越兴奋，记得越牢。0~1。"""
        return max(0.0, min(1.0, 0.65 * abs(float(emo.get("v") or 0.0))
                            + 0.35 * float(emo.get("a") or 0.0)))

    @staticmethod
    def _half_life(row: sqlite3.Row | dict[str, Any]) -> float:
        """半衰期：重要度打底，回忆次数（间隔效应）和情绪显著度拉长。"""
        try:
            imp = max(0.0, min(1.0, float(row["importance"])))
        except (KeyError, TypeError, ValueError, IndexError):
            imp = 0.5
        hl = 7.0 + 83.0 * imp
        try:
            recall_n = int(row["recall_count"] or 0)
        except (KeyError, TypeError, ValueError, IndexError):
            recall_n = 0
        try:
            sal = float(row["emo_salience"] or 0.0)
        except (KeyError, TypeError, ValueError, IndexError):
            sal = 0.0
        try:
            mult = float(row["half_life_mult"] or 1.0)
        except (KeyError, TypeError, ValueError, IndexError):
            mult = 1.0
        if _sw("memory.spacing", True):
            hl *= 1.0 + 0.55 * math.log1p(max(0, recall_n))
        if _sw("memory.emotion_weight", True):
            hl *= 1.0 + 0.60 * max(0.0, min(1.0, sal))
        hl *= max(0.25, min(6.0, mult))
        return max(1.0, min(3650.0, hl))

    def _rings(self) -> dict[str, dict[str, Any]]:
        """读星云写出的关系圈层快照（data/rings.json），按 mtime 缓存。

        星云没跑过就退回 config.json：管理员=核心，known_users=熟识，其余=陌生。
        """
        path = os.path.join(self._here, "data", "rings.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        if _RING_CACHE["mtime"] != mtime or not _RING_CACHE["data"]:
            data: dict[str, dict[str, Any]] = {}
            if mtime:
                try:
                    with open(path, encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        data = {str(k): v for k, v in raw.items()
                                if isinstance(v, dict) and v.get("ring") in RING_WEIGHTS}
                except Exception:  # noqa: BLE001
                    data = {}
            if not data:
                try:
                    with open(os.path.join(self._here, "config.json"), encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception:  # noqa: BLE001
                    cfg = {}
                for qq in {str(x) for x in (cfg.get("admins") or [])}:
                    data[qq] = {"ring": "core"}
                for qq in {str(x) for x in (cfg.get("known_users") or [])}:
                    data.setdefault(qq, {"ring": "inner"})
            _RING_CACHE["mtime"] = mtime
            _RING_CACHE["data"] = data
        return _RING_CACHE["data"]

    def _ring_weight(self, speaker: str) -> float:
        """差序权重：核心 1.0 / 熟识 0.75 / 陌生 0.45；未知按 0.55。"""
        ring = str((self._rings().get(str(speaker or "")) or {}).get("ring") or "")
        return float(RING_WEIGHTS.get(ring, 0.55))

    # ------------------------------------------------------------ 情绪

    def _state(self, speaker: str) -> dict[str, Any]:
        now = _now()
        row = self.conn.execute(
            "SELECT * FROM emotion_state WHERE scope=? AND speaker_id=?",
            (self.scope, speaker),
        ).fetchone()
        if row is None:
            st = {"valence": BASE_VALENCE, "arousal": BASE_AROUSAL,
                  "affection": BASE_AFFECTION, "updated_at": now}
            self.conn.execute(
                "INSERT INTO emotion_state(scope,speaker_id,valence,arousal,affection,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (self.scope, speaker, st["valence"], st["arousal"], st["affection"], now),
            )
            return st
        st = dict(row)
        dt_h = max(0.0, (now - float(st["updated_at"])) / 3600.0)
        st["valence"] = _half_life_decay(st["valence"], BASE_VALENCE, dt_h, VALENCE_HALF_LIFE_H)
        st["arousal"] = _half_life_decay(st["arousal"], BASE_AROUSAL, dt_h, VALENCE_HALF_LIFE_H)
        st["affection"] = _half_life_decay(
            st["affection"], BASE_AFFECTION, dt_h, AFFECTION_HALF_LIFE_D * 24)
        st["updated_at"] = now
        return st

    def _save_state(self, speaker: str, st: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO emotion_state(scope,speaker_id,valence,arousal,affection,updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(scope,speaker_id) DO UPDATE SET"
            " valence=excluded.valence, arousal=excluded.arousal,"
            " affection=excluded.affection, updated_at=excluded.updated_at",
            (self.scope, speaker, st["valence"], st["arousal"], st["affection"], _now()),
        )

    def _log_event(self, speaker: str, kind: str, delta: float, excerpt: str) -> None:
        self.conn.execute(
            "INSERT INTO emotion_events(scope,speaker_id,kind,delta,excerpt,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (self.scope, speaker, kind, delta, _squash(excerpt, 60), _now()),
        )

    def _open_grudges(self, speaker: str) -> list[sqlite3.Row]:
        now = _now()
        return self.conn.execute(
            "SELECT * FROM grudges WHERE scope=? AND speaker_id=? AND status='open'"
            " AND expires_at>? ORDER BY id DESC LIMIT 5",
            (self.scope, speaker, now),
        ).fetchall()

    def _add_grudge(self, speaker: str, cause: str, severity: int) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO grudges(scope,speaker_id,cause,severity,created_at,expires_at,status)"
            " VALUES(?,?,?,?,?,?,'open')",
            (self.scope, speaker, _squash(cause, 50), severity, now,
             now + GRUDGE_TTL_H * 3600),
        )

    def _try_repair(self, speaker: str) -> bool:
        rows = self._open_grudges(speaker)
        if not rows:
            return False
        top = rows[0]
        sev = int(top["severity"]) - 1
        if sev <= 0:
            self.conn.execute(
                "UPDATE grudges SET severity=0, status='repaired', repaired_at=? WHERE id=?",
                (_now(), top["id"]),
            )
        else:
            self.conn.execute("UPDATE grudges SET severity=? WHERE id=?", (sev, top["id"]))
        return True

    def observe(self, speaker: str, text: str) -> dict[str, Any]:
        """从用户这句话里读出情绪事件并更新状态。返回本轮变化，便于测试。"""
        st = self._state(speaker)
        hits: list[dict[str, Any]] = []
        t = text or ""

        apology_hit = bool(_PATTERNS[2][1].search(t))
        for kind, pat, weight, can_grudge in _PATTERNS:
            matches = list(pat.finditer(t))
            if not matches:
                continue
            if kind == "insult":
                matches = [m for m in matches if not _negated(t, m.start())]
                # 一句道歉里顺带一句重话，按「求和」处理，不追加记仇
                if not matches or (apology_hit and len(matches) <= 1):
                    continue
                severity = 2 if any(_HEAVY_INSULT.search(m.group(0)) for m in matches) else 1
                st["valence"] = _clip(st["valence"] - 0.22 * severity, -1.0, 1.0)
                st["arousal"] = _clip(st["arousal"] + 0.18 * severity, 0.0, 1.0)
                st["affection"] = _clip(st["affection"] - 0.04 * severity, 0.0, 1.0)
                if severity >= 2 or st["affection"] < 0.5:
                    m0 = matches[0]
                    self._add_grudge(speaker, _context(t, m0.start(), m0.end()), severity)
                    hits.append({"kind": "grudge", "severity": severity})
            elif kind == "praise":
                st["valence"] = _clip(st["valence"] + 0.12, -1.0, 1.0)
                st["arousal"] = _clip(st["arousal"] + 0.05, 0.0, 1.0)
                st["affection"] = _clip(st["affection"] + 0.02, 0.0, 1.0)
            elif kind == "apology":
                repaired = self._try_repair(speaker)
                st["valence"] = _clip(st["valence"] + 0.06, -1.0, 1.0)
                st["arousal"] = _clip(st["arousal"] - 0.10, 0.0, 1.0)
                hits.append({"kind": "apology", "repaired": repaired})
            elif kind == "promise":
                st["valence"] = _clip(st["valence"] + 0.04, -1.0, 1.0)
            self._log_event(speaker, kind, weight, t)
            hits.append({"kind": kind})

        self._save_state(speaker, st)
        self.conn.commit()
        return {"state": st, "events": hits}

    def mood_line(self, speaker: str) -> str:
        st = self._state(speaker)
        tone = next(label for cut, label in _TONE if st["valence"] < cut)
        return (f"心情 {st['valence']:+.2f}｜精神 {st['arousal']:.2f}｜"
                f"亲近 {st['affection']:.2f}。语气：{tone}")

    # ------------------------------------------------------------ 记忆与遗忘

    def _audience(self, speaker: str, audience: str = "") -> str:
        """没传受众时按“私聊本人”处理，绝不默认写成全局可见。"""
        aud = str(audience or "").strip()
        if aud:
            return aud
        spk = str(speaker or "").strip()
        return f"private:{spk}" if spk else "global"

    @_mback.backup_before("remember")
    def remember(self, speaker: str, content: str, importance: float = 0.5,
                 confidence: float = 0.8, kind: str = "fact", source: str = "",
                 audience: str = "", subject: str = "", layer: str = "") -> int:
        now = _now()
        aud = self._audience(speaker, audience)
        subj = str(subject or "").strip() or _fact_subject(_fact_text(content))
        emo = self._emotion_snapshot(speaker)
        sal = self._salience(emo)
        imp = float(importance)
        if _sw("memory.emotion_weight", True):
            imp = max(0.0, min(0.98, imp + 0.15 * sal))
        lay = str(layer or "").strip()
        if lay not in LAYER_ORDER:
            lay = "semantic" if str(kind) == "fact" else "episodic"
        cur = self.conn.execute(
            "INSERT INTO memories(scope,speaker_id,kind,content,importance,confidence,"
            "strength,created_at,updated_at,status,source,subject,audience,layer,"
            "emo_valence,emo_arousal,emo_affection,emo_salience)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.scope, speaker, kind, content, imp, confidence,
             1.0, now, now, "active", source, subj, aud, lay,
             emo["v"], emo["a"], emo["f"], sal),
        )
        self.conn.commit()
        mid = int(cur.lastrowid)
        self._queue_embed(mid, content)
        fts = self._fts()
        if fts is not None:
            try:  # 新记忆立刻进关键词索引，不必等下次重建
                fts.index_memory(self.conn, mid, content, self.scope, speaker, "active")
                self.conn.commit()
            except Exception:
                pass
        # 同主题事实检查（C3 矛盾、C9 演化）：同一个人出现同类事实时挂待确认
        if str(kind) == "fact":
            try:
                self._check_fact_change(speaker, aud, mid, content, subj)
            except Exception:
                pass
        return mid

    @_mback.backup_before("supersede")
    def supersede(self, old_id: int, new_id: int) -> None:
        """新事实覆盖旧事实，但保留时间线（不物理删除）。

        同时写 valid_to：召回按有效期过滤（C4），这样即使有人把 status 改回去，
        过期事实也不会再被想起来。
        """
        now = _now()
        self.conn.execute(
            "UPDATE memories SET status='superseded', superseded_by=?, valid_to=?,"
            " updated_at=? WHERE id=?",
            (new_id, now, now, old_id),
        )
        self.conn.commit()

    def _check_fact_change(self, speaker: str, aud: str, new_id: int,
                           content: str, subj: str) -> None:
        """同一主题来了新事实（C3 矛盾检测 / C9 演化）。

        判定用两条：subject 相同（住址这类明确会覆盖的），
        或 _fact_kind / 生活领域同类（口味/习惯/工作/关系…），且正文不同。
        默认不自动覆盖：旧记录保持 active，先在 conflicts 里挂一条待确认，
        同时在 memory_links 记一条 evolve 边，以后召回能顺着想到前因。
        例外：正文带明确改口词（戒了/搬了/换了…）时立刻让旧的失效并写 valid_to，
        但 conflict 仍然留着，面板上还能改回「保留旧值」。
        """
        replaceable = {"location", "work", "relationship", "birthday",
                       "preference", "pet"}
        mine_kind = _fact_kind(_fact_text(content))
        mine_domains = _domains_of(_fact_text(content))
        mine = _norm_memory(content)
        cands = self.conn.execute(
            "SELECT id, content, subject FROM memories WHERE scope=? AND status='active'"
            " AND (speaker_id=? OR speaker_id='') AND (audience=? OR audience='global')"
            " AND (kind='fact' OR content LIKE '%记：%') AND id<>?"
            " ORDER BY updated_at DESC LIMIT 80",
            (self.scope, speaker, aud, int(new_id))).fetchall()
        queued = 0
        for old in cands:
            same_subject = bool(subj) and str(old["subject"] or "") == subj
            same_kind = bool(mine_kind) and mine_kind in replaceable and \
                _fact_kind(_fact_text(old["content"])) == mine_kind
            same_domain = bool(mine_domains & _domains_of(_fact_text(old["content"])))
            if not (same_subject or same_kind or same_domain):
                continue
            if _norm_memory(old["content"]) == mine:
                continue
            dup = self.conn.execute(
                "SELECT 1 FROM conflicts WHERE old_id=? AND new_id=? AND status='pending'",
                (int(old["id"]), int(new_id))).fetchone()
            if not dup:
                if same_subject:
                    tag = subj
                elif same_domain:
                    tag = sorted(mine_domains & _domains_of(_fact_text(old["content"])))[0]
                else:
                    tag = mine_kind
                self._queue_conflict(speaker, aud, int(old["id"]), int(new_id),
                                     f"同主题新事实（{tag}），旧记录待确认")
                queued += 1
                # 明确改口（「戒了/搬了/换了」）：立刻让旧的失效，不必等人去面板点
                if _CHANGE_CUE_RE.search(_fact_text(content)):
                    self.conn.execute(
                        "UPDATE memories SET status='superseded', superseded_by=?,"
                        " valid_to=?, updated_at=? WHERE id=? AND status='active'",
                        (int(new_id), _now(), _now(), int(old["id"])))
                    self.conn.commit()
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_links(a_id,b_id,kind,weight,updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (int(new_id), int(old["id"]), "evolve", 0.6, _now()))
                self.conn.commit()
            except Exception:
                pass
            if queued >= 3:
                break

    def revise(self, memory_id: int, new_text: str,
               supersede_old: bool = False) -> dict[str, Any]:
        """纠正一条记忆（C8）：改正文 → 重建索引 → 重算向量 → 可选让旧主题失效。

        面板里「她记错了」的场景：不用删掉重录，直接在原记录上改正，
        这样 access_count / importance / 时间线都还留着。
        """
        text = (new_text or "").strip()
        if not text:
            return {"ok": False, "error": "新正文为空"}
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "记忆不存在"}
        now = _now()
        self.conn.execute("UPDATE memories SET content=?, updated_at=? WHERE id=?",
                          (text, now, int(memory_id)))
        self.conn.commit()
        indexed = False
        fts = self._fts()
        if fts is not None:
            try:
                fts.index_memory(self.conn, int(memory_id), text, self.scope,
                                 str(row["speaker_id"]), str(row["status"]))
                self.conn.commit()
                indexed = True
            except Exception:
                pass
        try:
            self._queue_embed(int(memory_id), text)
        except Exception:
            pass
        superseded: list[int] = []
        subject = str(row["subject"] or "")
        if supersede_old and str(row["kind"]) == "fact" and subject:
            for old in self.conn.execute(
                    "SELECT id FROM memories WHERE scope=? AND status='active'"
                    " AND subject=? AND id<>?",
                    (self.scope, subject, int(memory_id))).fetchall():
                oid = int(old["id"])
                self.conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?,"
                    " valid_to=?, updated_at=? WHERE id=?",
                    (int(memory_id), now, now, oid))
                superseded.append(oid)
            self.conn.commit()
        return {"ok": True, "id": int(memory_id), "indexed": indexed,
                "superseded": superseded}

    def _queue_conflict(self, speaker: str, audience: str, old_id: int,
                        new_id: int, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO conflicts(scope,speaker_id,audience,old_id,new_id,reason,"
            "status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
            (self.scope, speaker, audience, int(old_id), int(new_id),
             _squash(reason, 120), _now()))
        self.conn.commit()

    def _strength(self, row: sqlite3.Row, now: float) -> float:
        base = float(row["strength"])
        anchor = float(row["updated_at"])
        try:
            # 回忆会把时钟往后拨：上次想起的时间晚于写入时间时，从想起时开始衰减。
            anchor = max(anchor, float(row["last_access_at"] or 0))
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        days = max(0.0, (now - anchor) / 86400.0)
        return base * (0.5 ** (days / self._half_life(row)))

    # ------------------------------------------------------------ 语义向量

    def _embed_ready(self) -> bool:
        try:
            import settings_store
            if str(settings_store.get().get("embed", {}).get("provider")) == "off":
                return False
        except Exception:  # noqa: BLE001
            pass
        return str(_embed_conf(self._here).get("provider")) == "cloud"

    def _cloud_embed(self, texts: list[str]) -> list[list[float]]:
        conf = _embed_conf(self._here)
        body = json.dumps({"model": conf.get("model"), "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            str(conf["base"]).rstrip("/") + "/embeddings", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(conf["key"])})
        with urllib.request.urlopen(req, timeout=float(conf.get("timeout") or 6)) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [d["embedding"] for d in (data.get("data") or [])]

    def _store_vec(self, memory_id: int, vec: Any, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        conf = _embed_conf(self._here)
        c.execute(
            "INSERT OR REPLACE INTO embeddings(memory_id,model,dim,vec,updated_at)"
            " VALUES(?,?,?,?,?)",
            (int(memory_id), str(conf.get("model")), len(vec), _pack(vec), _now()))
        c.commit()

    def _vector(self, memory_id: int):
        row = self.conn.execute("SELECT vec FROM embeddings WHERE memory_id=?",
                                (int(memory_id),)).fetchone()
        return _unpack(row["vec"]) if row else None

    def _query_vec(self, text: str):
        if not text or not self._embed_ready():
            return None
        if not _sw("recall.semantic"):
            return None
        if _trivial(text):
            return None
        now = _now()
        if now < self._embed_down_until:
            return None
        hit = self._qcache.get(text)
        if hit and now - hit[0] < 300:
            return hit[1]
        try:
            vecs = self._cloud_embed([text[:400]])
            vec = vecs[0] if vecs else None
        except Exception as e:  # noqa: BLE001
            self._embed_down_until = now + 60   # 失败后 1 分钟不再试，避免每次回复都卡
            _log_embed(f"query embed failed: {type(e).__name__}: {e}")
            return None
        if vec:
            if len(self._qcache) > 32:
                self._qcache.clear()
            self._qcache[text] = (now, vec)
        return vec

    def _queue_embed(self, memory_id: int, content: str) -> None:
        if not self._embed_ready():
            return
        try:
            if self._eq is None:
                self._eq = queue.Queue()
                threading.Thread(target=self._embed_worker, daemon=True).start()
            self._eq.put((int(memory_id), (content or "")[:400]))
        except Exception:
            pass

    def _embed_worker(self) -> None:
        # 后台线程必须用自己的连接：SQLite 连接不能跨线程用
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception as e:  # noqa: BLE001
            _log_embed(f"worker conn failed: {type(e).__name__}: {e}")
            return
        while True:
            batch = []
            try:
                batch.append(self._eq.get())
                deadline = time.time() + 0.2
                while len(batch) < 8 and time.time() < deadline:
                    try:
                        batch.append(self._eq.get_nowait())
                    except queue.Empty:
                        time.sleep(0.03)
            except Exception:
                time.sleep(1)
                continue
            if _now() < self._embed_down_until:
                continue
            try:
                vecs = self._cloud_embed([t for _, t in batch])
                for (mid, _), v in zip(batch, vecs):
                    self._store_vec(mid, v, conn)
            except Exception as e:  # noqa: BLE001
                _log_embed(f"batch embed failed: {type(e).__name__}: {e}")
                time.sleep(2)

    def _embed_chunk(self, chunk: list[sqlite3.Row]) -> int:
        """一小段记忆换向量，返回补上的条数。

        整批失败先等冷却再试一次；还失败就逐条试，把坏条目挑出来跳过，
        不让一条坏数据把整晚的补齐卡死。
        """
        texts = [str(r["content"] or "")[:400] for r in chunk]
        for attempt in (0, 1):
            try:
                vecs = self._cloud_embed(texts)
            except Exception as e:  # noqa: BLE001
                self._embed_down_until = _now() + 60
                _log_embed(f"backfill failed: {type(e).__name__}: {e}")
                if attempt == 0:
                    time.sleep(60)
                    continue
                break
            stored = 0
            for r, v in zip(chunk, vecs):
                self._store_vec(int(r["id"]), v)
                stored += 1
            return stored
        stored = 0
        for r in chunk:
            try:
                vecs = self._cloud_embed([str(r["content"] or "")[:400]])
            except Exception as e:  # noqa: BLE001
                _log_embed(f"backfill skip id={r['id']}: {type(e).__name__}: {e}")
                continue
            if not vecs:
                continue
            self._store_vec(int(r["id"]), vecs[0])
            stored += 1
        return stored

    @_mback.backup_before("backfill")
    def backfill(self, limit: int | None = None) -> int:
        """把还没有向量的记忆补上（一次性 / 夜间跑）。

        limit=None（默认）表示补完为止：按 100 条翻页，补好的会从 LEFT JOIN
        里消失，每页都在往前走；一行都没补上或服务异常就停，留到下次，
        避免原地打转或猛刷接口。
        """
        if not self._embed_ready():
            return 0
        page = 100
        todo = None if limit is None else max(0, int(limit))
        done = 0
        while todo is None or todo > 0:
            size = page if todo is None else min(page, todo)
            rows = self.conn.execute(
                "SELECT m.id, m.content FROM memories m"
                " LEFT JOIN embeddings e ON e.memory_id=m.id"
                " WHERE m.scope=? AND m.status='active' AND e.memory_id IS NULL"
                " ORDER BY m.id LIMIT ?",
                (self.scope, size)).fetchall()
            if not rows:
                break
            before = done
            for i in range(0, len(rows), 8):
                got = self._embed_chunk(rows[i:i + 8])
                if not got:
                    return done
                done += got
                if todo is not None:
                    todo -= got
                    if todo <= 0:
                        break
            if done == before:
                break
            if len(rows) < size:
                break
        return done

    # ---- 关键词检索（FTS5）：只补「字面命中」这一路，索引按需自愈 ----
    _FTS = None

    @classmethod
    def _fts(cls):
        """懒加载关键词模块；找不到就返回 None，召回退回纯向量。"""
        if cls._FTS is not None:
            return cls._FTS or None
        try:
            import importlib
            import os as _os
            import sys as _sys
            home = _os.environ.get("WHALEGIRL_HOME") or (
                r"<工作区>")
            if home not in _sys.path:
                _sys.path.insert(0, home)
            cls._FTS = importlib.import_module("whalegirl.fts")
        except Exception:
            cls._FTS = False
        return cls._FTS or None

    def _fts_sync(self, fts) -> None:
        """索引与 memories 不一致就整表重建。

        指纹用「条数:最大 updated_at」，这样面板删一条、合并一条也能被发现；
        19 条约 8ms，够用。量大再改成写时增量同步。
        """
        try:
            has = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (fts.TABLE,)).fetchone()
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='fts_synced_at'").fetchone()
            cnt = int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            mx = float(self.conn.execute(
                "SELECT COALESCE(MAX(updated_at),0) FROM memories").fetchone()[0])
            fp = f"{cnt}:{mx:.3f}"
            if has and row and str(row[0]) == fp:
                return
            fts.rebuild(self.conn)
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('fts_synced_at',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fp,))
            self.conn.commit()
        except Exception:
            pass

    def _keyword_scores(self, query: str, rows) -> dict[int, float]:
        """返回 {记忆id: 0~1 关键词分}，只保留本轮可见的候选。"""
        if not query:
            return {}
        fts = self._fts()
        if fts is None:
            return {}
        try:
            self._fts_sync(fts)
            allowed = {int(r["id"]) for r in rows}
            hits = fts.search(self.conn, query, k=max(20, len(allowed)),
                              scope=self.scope, active_only=True)
            return {mid: sc for mid, sc in hits if mid in allowed}
        except Exception:
            return {}

    def recall(self, speaker: str, query: str, limit: int = 5,
               min_score: float = 0.45, audience: str = "",
               log: bool = True) -> list[dict[str, Any]]:
        now = _now()
        aud = self._audience(speaker, audience)
        emo_now = self._emotion_snapshot(speaker)
        ring_w = self._ring_weight(speaker) if _sw("recall.ring", True) else 0.0
        has_pin = any(r[1] == "pinned"
                      for r in self.conn.execute("PRAGMA table_info(memories)"))
        has_valid = any(r[1] == "valid_to"
                        for r in self.conn.execute("PRAGMA table_info(memories)"))
        order = ("COALESCE(pinned,0) DESC, updated_at DESC"
                 if has_pin else "updated_at DESC")
        # 有效期过滤（C4）：过期事实不再参与召回；老数据 valid_to 为空 = 一直有效
        valid_sql = " AND (valid_to IS NULL OR valid_to > ?)" if has_valid else ""
        params: tuple = (self.scope, speaker, aud, now) if has_valid else (self.scope, speaker, aud)
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " AND (speaker_id=? OR speaker_id='')"
            " AND (audience=? OR audience='global')"
            f"{valid_sql} ORDER BY {order} LIMIT 400",
            params,
        ).fetchall()
        q = _grams(query)
        qx = _expand_grams(query) if (query and _sw("recall.synonym")) else set()
        qv = self._query_vec(query) if query else None
        vecs: dict[int, Any] = {}
        if qv is not None:
            ids = [int(r["id"]) for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                for e in self.conn.execute(
                        "SELECT memory_id, vec FROM embeddings WHERE memory_id IN (%s)" % marks,
                        ids):
                    vecs[int(e["memory_id"])] = e["vec"]
        emotion_on = _sw("memory.emotion_weight", True)
        # 时间题（「什么时候/上次/第一次」）把时间权重抬起来；内容带日期的再加一笔
        time_q = bool(_TIME_CUE_RE.search(query or ""))
        recency_w = 0.30 if time_q else 0.12
        # 向量批量算（C10）：一次矩阵乘取代逐条余弦，几千条时快一个量级
        sim_map: dict[int, float] = {}
        if qv is not None and vecs:
            try:
                import numpy as _np
                mids = list(vecs)
                mat = _np.array([_unpack(vecs[m]) for m in mids], dtype="float32")
                qvec = _np.asarray(qv, dtype="float32")
                denom = _np.linalg.norm(mat, axis=1) * (_np.linalg.norm(qvec) + 1e-9) + 1e-9
                sims = (mat @ qvec) / denom
                sim_map = {m: float(s) for m, s in zip(mids, sims)}
            except Exception:
                sim_map = {}
        # 查询分流：推理题只走语义通道（关键词对它是干扰，评测已量化）
        reasoning_q = (bool(_REASONING_RE.search(query or ""))
                       or len((query or "").strip()) >= 14)
        kw = {} if reasoning_q else self._keyword_scores(query, rows)
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            pinned = bool(r["pinned"]) if has_pin else False
            kw_s = kw.get(int(r["id"]), 0.0)
            strength = self._strength(r, now)
            if strength < 0.05 and not pinned:
                continue
            days = max(0.0, (now - float(r["updated_at"])) / 86400.0)
            recency = math.exp(-days / 30.0)
            layer = self._layer_of(r)
            # 情绪加权：写入时的显著度 + 与此刻心情的同向程度
            emo_term = 0.0
            if emotion_on:
                sal = max(0.0, min(1.0, float(r["emo_salience"] or 0.0)))
                fit = 1.0 - min(1.0, abs(float(emo_now["v"])
                                         - float(r["emo_valence"] or 0.0)) / 2.0)
                emo_term = 0.55 * sal + 0.45 * fit
            # 差序：关系越近，同等相关度下越容易想起来
            ring_term = 0.15 * ring_w
            core_term = 0.20 if layer == "core" else 0.0
            blob = vecs.get(int(r["id"]))
            if qv is not None and blob is not None:
                # 中文向量基线相似度偏高（不相关也有 0.35~0.52），先减基线再比
                raw_sim = sim_map.get(int(r["id"]))
                if raw_sim is None:
                    raw_sim = _cos(qv, _unpack(blob))
                sim = (raw_sim - 0.45) / 0.55
                # 字面命中很强时，允许低相似度也进来（专有名词、人名、型号）
                if sim < 0.10 and kw_s < 0.45 and not pinned:
                    continue
                score = (0.55 * sim + 0.18 * kw_s + 0.22 * float(r["importance"]) +
                          recency_w * recency + 0.11 * emo_term + ring_term + core_term +
                          (0.6 if pinned else 0))
                if _DATE_RE.search(r["content"] or ""):
                    score += 0.12
                if score >= 0.30 or pinned:
                    scored.append((score, r))
                continue
            ov = _overlap(q, _grams(r["content"]))
            ovx = _overlap(qx, _grams(r["content"])) if qx else 0.0
            # 有查询词却零重叠 → 不硬凑，宁可这一轮不召回；永久记忆例外
            if q and ov <= 0 and ovx <= 0 and kw_s <= 0 and not pinned:
                continue
            score = (2.0 * ov + 0.9 * ovx + 0.35 * kw_s + 0.45 * float(r["importance"]) +
                     (0.40 if time_q else 0.25) * recency + 0.20 * min(1.0, strength) +
                     0.35 * emo_term + ring_term + core_term + (0.6 if pinned else 0))
            if _DATE_RE.search(r["content"] or ""):
                score += 0.12
            if score >= min_score or pinned:
                scored.append((score, r))
        if has_pin:
            got = {int(r["id"]) for _, r in scored}
            for r in rows:
                if r["pinned"] and int(r["id"]) not in got:
                    scored.append((999.0, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        # 拒答校准（C7）：分数偏低、前三名又挤在一起 → 说明没有真正对得上的，宁可空手
        if scored and _sw("recall.abstain", True):
            loose = [(s, r) for s, r in scored if not bool(r["pinned"])]
            if len(loose) >= 2:
                top = loose[0][0]
                third = loose[2][0] if len(loose) > 2 else top
                if top < 0.42 and (top - third) < 0.06:
                    self._note_recall(speaker, query, len(scored), 0, "abstain")
                    if log:
                        self._log_recall(speaker, aud, query, len(scored), [], [])
                    return []
        if _sw("recall.rerank", True):
            scored = self._rerank(scored, query)
        picked = self._mmr_pick(scored, limit)
        base_ids = {int(r["id"]) for _, r in picked}
        picked = self._expand_links(picked, limit)
        out: list[dict[str, Any]] = []
        for score, r in picked:
            mid = int(r["id"])
            layer = self._layer_of(r)
            out.append({"id": mid, "content": r["content"],
                        "confidence": float(r["confidence"]),
                        "importance": float(r["importance"]),
                        "layer": layer, "layer_label": LAYER_LABELS.get(layer, layer),
                        "via": "recall" if mid in base_ids else "assoc",
                        "score": round(score, 3)})
            if _sw("memory.spacing", True):
                self.conn.execute(
                    "UPDATE memories SET access_count=access_count+1, last_access_at=?,"
                    " recall_count=recall_count+1,"
                    " strength=MIN(1.0, strength+0.12),"
                    " half_life_mult=MIN(4.0, half_life_mult*1.12) WHERE id=?",
                    (now, mid),
                )
            else:
                self.conn.execute(
                    "UPDATE memories SET access_count=access_count+1,"
                    " recall_count=recall_count+1 WHERE id=?", (mid,))
        self.conn.commit()
        if log:
            self._log_recall(speaker, aud, query, len(scored), picked, out)
        self._note_recall(speaker, query, len(scored), len(out), "ok")
        try:  # 影子召回：并行跑新算法，只记录，绝不改 out
            import shadow_recall as _shadow
            if _shadow.enabled(_sw("recall.shadow", False)):
                _shadow.record(self.db_path, self.scope, speaker, aud or "", query, out)
        except Exception:
            pass
        return out

    def _expand_links(self, picked: list[tuple[float, sqlite3.Row]],
                      limit: int) -> list[tuple[float, sqlite3.Row]]:
        """差序联想：从已选中的前两条走一跳共现/同主题边，最多补 2 条。"""
        if not picked or not _sw("recall.assoc", True):
            return picked
        selected = {int(r["id"]) for _, r in picked}
        extra: list[tuple[float, sqlite3.Row]] = []
        for parent_score, parent in picked[:2]:
            links = self.conn.execute(
                "SELECT b_id AS other, weight FROM memory_links WHERE a_id=?"
                " UNION ALL SELECT a_id AS other, weight FROM memory_links WHERE b_id=?"
                " ORDER BY weight DESC LIMIT 6",
                (int(parent["id"]), int(parent["id"]))).fetchall()
            for link in links:
                cid = int(link["other"])
                if cid in selected:
                    continue
                cand = self.conn.execute(
                    "SELECT * FROM memories WHERE id=? AND status='active'",
                    (cid,)).fetchone()
                if cand is None:
                    continue
                if not bool(cand["pinned"]) and self._strength(cand, _now()) < 0.05:
                    continue
                extra.append((max(0.05, float(parent_score) * 0.45
                                  * float(link["weight"] or 0.5)), cand))
        extra.sort(key=lambda x: x[0], reverse=True)
        for score, row in extra:
            if len(picked) >= limit + 2:
                break
            if int(row["id"]) in selected:
                continue
            picked.append((score, row))
            selected.add(int(row["id"]))
        return picked

    def _rerank(self, scored: list[tuple[float, sqlite3.Row]],
                query: str) -> list[tuple[float, sqlite3.Row]]:
        """规则重排（C5）：只动前 20 条。

        加两样东西：整句字面命中（说明这条就是原话）、查询双字的覆盖率。
        向量分对短句区分度差，精确命中往往更可信。
        """
        if not scored or not query:
            return scored
        qn = (query or "").strip()
        grams = _grams(qn)
        head, tail = scored[:20], scored[20:]
        out: list[tuple[float, sqlite3.Row]] = []
        for score, r in head:
            content = r["content"] or ""
            bonus = 0.0
            if qn and qn in content:
                bonus += 0.12
            if grams:
                bonus += 0.10 * _overlap(grams, _grams(content))
            out.append((score + bonus, r))
        out.sort(key=lambda x: x[0], reverse=True)
        return out + tail

    def _mmr_pick(self, scored: list[tuple[float, sqlite3.Row]],
                  limit: int) -> list[tuple[float, sqlite3.Row]]:
        """最大边际相关：先保永久记忆，再按层配额在“相关”和“不重复”之间取平衡。"""
        if limit <= 0 or not scored:
            return []
        quota = (dict(LAYER_QUOTA) if _sw("recall.layer_quota", True)
                 else {k: 99 for k in LAYER_ORDER})
        selected: list[tuple[float, sqlite3.Row]] = []
        chosen: set[int] = set()
        counts: dict[str, int] = {}
        pinned = [(s, r) for s, r in scored if bool(r["pinned"])]
        for s, r in sorted(pinned, key=lambda x: x[0], reverse=True):
            if len(selected) >= limit:
                return selected
            selected.append((s, r))
            chosen.add(int(r["id"]))
            lay = self._layer_of(r)
            counts[lay] = counts.get(lay, 0) + 1
        remaining = [(s, r) for s, r in scored if int(r["id"]) not in chosen]
        if not remaining:
            return selected
        if not _sw("recall.mmr", True):
            for s, r in remaining:
                if len(selected) >= limit:
                    break
                lay = self._layer_of(r)
                if counts.get(lay, 0) >= quota.get(lay, 99):
                    continue
                selected.append((s, r))
                counts[lay] = counts.get(lay, 0) + 1
            return selected
        lam = max(0.5, min(0.9, _sw_num("recall.mmr_lambda", 0.72)))
        max_score = max((max(0.0, s) for s, _ in remaining), default=1.0) or 1.0
        grams = {int(r["id"]): _grams(str(r["content"])) for _, r in remaining}
        while remaining and len(selected) < limit:
            best_idx: int | None = None
            best_val = -1e9
            for i, (s, r) in enumerate(remaining):
                lay = self._layer_of(r)
                if counts.get(lay, 0) >= quota.get(lay, 99):
                    continue
                rel = max(0.0, s) / max_score
                sim = 0.0
                for _, sr in selected:
                    sim = max(sim, _overlap(grams[int(r["id"])],
                                            _grams(str(sr["content"]))))
                val = lam * rel - (1.0 - lam) * sim
                if val > best_val:
                    best_val, best_idx = val, i
            if best_idx is None:
                break
            picked = remaining.pop(best_idx)
            selected.append(picked)
            lay = self._layer_of(picked[1])
            counts[lay] = counts.get(lay, 0) + 1
        return selected

    def _note_recall(self, speaker: str, query: str, cand: int, picked: int,
                     tag: str) -> None:
        """往 <数据库目录>/data/recall-debug.log 追一行，方便直接看「召回到底跑没跑」。

        跟着数据库走，沙箱库的测试记录不会混进生产日志。
        超过 512 KB 就轮转成 .1，避免无限长。
        """
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(self.db_path)),
                                "data", "recall-debug.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                bak = path + ".1"
                try:
                    if os.path.exists(bak):
                        os.remove(bak)
                except OSError:
                    pass
                try:
                    os.replace(path, bak)
                except OSError:
                    pass
            line = (time.strftime("%Y-%m-%d %H:%M:%S") + f" [{tag}] 说话人={speaker}"
                    f" 候选={cand} 选中={picked} 查询={_squash(query or '', 40)}\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _log_recall(self, speaker: str, audience: str, query: str,
                    candidate_count: int,
                    picked: list[tuple[float, sqlite3.Row]],
                    out: list[dict[str, Any]] | None = None) -> None:
        if not _sw("recall.log", True):
            return
        try:
            q = _squash(query or "", 120)
            kinds = {int(r["id"]): str(r["kind"]) for _, r in picked}
            selected = [{"id": int(m["id"]), "score": round(float(m["score"]), 3),
                         "kind": kinds.get(int(m["id"]), ""),
                         "layer": m.get("layer", ""), "via": m.get("via", "recall")}
                        for m in (out or [])]
            if not selected:
                selected = [{"id": int(r["id"]), "score": round(float(s), 3),
                             "kind": str(r["kind"])}
                            for s, r in picked]
            chars = sum(len(str(r["content"] or "")) for _, r in picked)
            self.conn.execute(
                "INSERT INTO recall_log(scope,speaker_id,audience,query,query_hash,"
                "candidate_count,selected_count,selected,injected_chars,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.scope, speaker, audience, q,
                 hashlib.sha1(q.encode("utf-8")).hexdigest()[:16],
                 int(candidate_count), len(selected),
                 json.dumps(selected, ensure_ascii=False), chars, _now()))
            self.conn.commit()
        except Exception:
            # 日志失败不能影响正常回复
            pass

    def recall_log(self, limit: int = 100, speaker: str = "",
                   audience: str = "") -> list[dict[str, Any]]:
        """读最近的召回日志，给 CLI / 星云接口用。"""
        limit = max(1, min(500, int(limit or 100)))
        sql = ("SELECT * FROM recall_log WHERE scope=?"
               + (" AND speaker_id=?" if speaker else "")
               + (" AND audience=?" if audience else "")
               + " ORDER BY id DESC LIMIT ?")
        args: list[Any] = [self.scope]
        if speaker:
            args.append(speaker)
        if audience:
            args.append(audience)
        args.append(limit)
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(sql, args):
            row = dict(r)
            try:
                row["selected"] = json.loads(row.get("selected") or "[]")
            except Exception:
                row["selected"] = []
            out.append(row)
        return out

    # ------------------------------------------------------------ 事实沉淀

    @_mback.backup_before("ingest")
    def ingest(self, speaker: str, text: str, store: bool = True,
               audience: str = "") -> list[str]:
        """每轮从用户自己的话里抽事实；重复的只刷新强度，不重复入库。

        store=False 时只做识别、不落库（提示词阶段用，真正的记忆由 ingest_pair 成对写入）。
        audience 决定这条记忆只在哪个会话可见；不传时按私聊本人处理。
        """
        added: list[str] = []
        if not text:
            return added
        now = _now()
        emo = self._emotion_snapshot(speaker)
        sal = self._salience(emo)
        emo_on = _sw("memory.emotion_weight", True)
        aud = self._audience(speaker, audience)
        rows = self.conn.execute(
            "SELECT id, content, subject, pinned FROM memories"
            " WHERE scope=? AND status='active'"
            " AND (speaker_id=? OR speaker_id='')"
            " AND (audience=? OR audience='global') LIMIT 500",
            (self.scope, speaker, aud),
        ).fetchall()
        existing: list[dict[str, Any]] = [
            {"id": int(r["id"]), "content": str(r["content"]),
             "subject": str(r["subject"] or ""), "pinned": bool(r["pinned"])}
            for r in rows
        ]

        for pat, importance in _FACT_RULES:
            for m in pat.finditer(text):
                content = re.sub(r"^(我|俺)", "", m.group(0)).strip(" ，。！？、")
                if len(content) < 3:
                    continue
                ok, kind, subject, body, reason = _quality_gate(
                    content, 0.75, "ingest") if _sw("memory.write_gate", True) else (
                    True, _fact_kind(content), _fact_subject(content), content, "")
                if not ok:
                    continue
                grams = _grams(body)
                dup: dict[str, Any] | None = None
                conflict: dict[str, Any] | None = None
                for e in existing:
                    e_fact = _fact_text(e["content"])
                    if _norm_memory(e_fact) == _norm_memory(body):
                        dup = e
                        break
                    e_subj = e["subject"] or _fact_subject(e_fact)
                    if subject and e_subj == subject and _overlap(grams, _grams(e_fact)) < 0.9:
                        conflict = e
                        break
                    if _overlap(grams, _grams(e_fact)) >= 0.9:
                        dup = e
                        break
                if dup is not None:
                    if store:
                        self.conn.execute(
                            "UPDATE memories SET strength=MIN(1.0,strength+0.3),"
                            " updated_at=?, confidence=MIN(1.0,confidence+0.05) WHERE id=?",
                            (now, dup["id"]))
                    continue
                if not store:
                    existing.append({"id": 0, "content": body, "subject": subject,
                                     "pinned": False})
                    added.append(body)
                    continue
                cur = self.conn.execute(
                    "INSERT INTO memories(scope,speaker_id,kind,content,importance,"
                    "confidence,strength,created_at,updated_at,status,source,subject,audience,"
                    "layer,emo_valence,emo_arousal,emo_affection,emo_salience)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.scope, speaker, kind, body,
                     min(0.98, importance + (0.15 * sal if emo_on else 0.0)),
                     0.75, 1.0, now, now, "active", "ingest", subject, aud,
                     "semantic", emo["v"], emo["a"], emo["f"], sal))
                new_id = int(cur.lastrowid)
                if conflict is not None:
                    if conflict["pinned"]:
                        self._queue_conflict(speaker, aud, conflict["id"], new_id,
                                             "new fact conflicts with pinned memory")
                    else:
                        self.supersede(conflict["id"], new_id)
                existing.append({"id": new_id, "content": body, "subject": subject,
                                 "pinned": False})
                self._queue_embed(new_id, body)
                # 主题级改口（C3）：subject 抓不到的（口味/健康/爱好…）在这里补挂待确认
                try:
                    self._check_fact_change(speaker, aud, new_id, body, subject)
                except Exception:
                    pass
                added.append(body)
        self.conn.commit()
        return added

    @_mback.backup_before("ingest-pair")
    def ingest_pair(self, speaker: str, question: str, answer: str,
                    audience: str = "") -> list[str]:
        """把一轮真实的「问 + 答」成对存成一条情景记忆。

        记忆必须能对应回原话，否则只留半句碎片（“喜欢吃饭啊”）什么都说明不了。
        疑问句走老路：事实在答里，答句要命中偏好/住处/宠物/日程/生日这类句式。
        陈述句（我养了一只猫 / 我最近在忙 CAD）没有“答案里的事实”，但整轮对话
        本身是情景层的数据来源；太短、纯应答、命令、密码由质量门挡掉，其余入库，
        交给遗忘曲线淘汰。否则普通聊天一条记忆都不落，四层/情绪/联想/摘要全空。
        """
        q = _squash(question or "", 90)
        a = _squash(answer or "", 90)
        if not q or not a:
            return []
        asking = _is_question(question or "")
        importance = 0.0
        for pat, imp in _FACT_RULES:
            if pat.search(question if not asking else answer or ""):
                importance = max(importance, imp if asking else imp * 0.9)
        if asking and importance <= 0:
            # 问句答句都没命中事实句式：当普通问答，不占记忆
            return []
        if not asking:
            if len(q) < 4 or _trivial(question or ""):
                return []
            importance = importance or 0.45
        gate_text = a if asking else question or ""
        ok, _kind, subject, _body, _reason = _quality_gate(
            gate_text, 0.7 if asking else 0.55, "exchange", min_len=2) if _sw(
            "memory.write_gate", True) else (
            True, "qa", _fact_subject(gate_text), gate_text, "")
        if not ok:
            return []
        content = f"问：{q}\n答：{a}"
        now = _now()
        emo = self._emotion_snapshot(speaker)
        sal = self._salience(emo)
        imp_final = min(0.98, importance + (0.15 * sal
                                             if _sw("memory.emotion_weight", True) else 0.0))
        aud = self._audience(speaker, audience)
        rows = self.conn.execute(
            "SELECT id, content, subject, pinned FROM memories"
            " WHERE scope=? AND status='active'"
            " AND speaker_id=? AND (audience=? OR audience='global') LIMIT 500",
            (self.scope, speaker, aud)).fetchall()
        grams_q, grams_a = _grams(q), _grams(a)
        conflict: sqlite3.Row | None = None
        for r in rows:
            old_fact = _fact_text(r["content"])
            old_subj = str(r["subject"] or "") or _fact_subject(old_fact)
            if subject and old_subj == subject and _norm_memory(old_fact) != _norm_memory(a) \
                    and _overlap(_grams(old_fact), _grams(a)) < 0.9:
                conflict = r
                break
            # 问句像 或 答句像，就算同一条记忆（换个人问、换个说法问，不重复入库）
            if (_overlap(grams_q, _grams(r["content"])) >= 0.6
                    or _overlap(grams_a, _grams(old_fact)) >= 0.75):
                self.conn.execute(
                    "UPDATE memories SET strength=MIN(1.0,strength+0.3), updated_at=?,"
                    " confidence=MIN(1.0,confidence+0.05) WHERE id=?", (now, int(r["id"])))
                self.conn.commit()
                return []
        cur = self.conn.execute(
            "INSERT INTO memories(scope,speaker_id,kind,content,importance,"
            "confidence,strength,created_at,updated_at,status,source,subject,audience,"
            "layer,emo_valence,emo_arousal,emo_affection,emo_salience)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.scope, speaker, "qa", content, round(imp_final, 2), 0.8,
             1.0, now, now, "active", "exchange", subject, aud,
             "episodic", emo["v"], emo["a"], emo["f"], sal))
        new_id = int(cur.lastrowid)
        if conflict is not None:
            if bool(conflict["pinned"]):
                self._queue_conflict(speaker, aud, int(conflict["id"]), new_id,
                                     "new answer conflicts with pinned memory")
            else:
                self.supersede(int(conflict["id"]), new_id)
        self._queue_embed(new_id, content)
        self.conn.commit()
        return [content]

    @_mback.backup_before("ingest-model")
    def ingest_model_pair(self, speaker: str, question: str, answer: str,
                          fact: str, importance: float = 0.6,
                          audience: str = "") -> list[str]:
        """模型判定“值得记”之后写入：问 + 答 + 提炼出的事实，三行一条。"""
        q = _squash(question or "", 90)
        a = _squash(answer or "", 90)
        f = _squash(fact or "", 60).strip("。；; ")
        if not (q and a and f):
            return []
        ok, _kind, subject, body, _reason = _quality_gate(
            f, 0.85, "model") if _sw("memory.write_gate", True) else (
            True, "qa", _fact_subject(f), f, "")
        if not ok:
            return []
        f = body
        content = f"问：{q}\n答：{a}\n记：{f}"
        now = _now()
        emo = self._emotion_snapshot(speaker)
        sal = self._salience(emo)
        aud = self._audience(speaker, audience)
        fg = _grams(f)
        rows = self.conn.execute(
            "SELECT id, content, subject, pinned FROM memories"
            " WHERE scope=? AND status='active'"
            " AND speaker_id=? AND (audience=? OR audience='global') LIMIT 500",
            (self.scope, speaker, aud)).fetchall()
        conflict: sqlite3.Row | None = None
        for r in rows:
            old_fact = _fact_text(r["content"])
            old_subj = str(r["subject"] or "") or _fact_subject(old_fact)
            if subject and old_subj == subject and _norm_memory(old_fact) != _norm_memory(f) \
                    and _overlap(_grams(old_fact), fg) < 0.9:
                conflict = r
                break
            if _overlap(fg, _grams(r["content"])) >= 0.7:
                self.conn.execute(
                    "UPDATE memories SET strength=MIN(1.0,strength+0.3), updated_at=?,"
                    " confidence=MIN(1.0,confidence+0.05) WHERE id=?", (now, int(r["id"])))
                self.conn.commit()
                return []
        imp = max(0.3, min(0.95, float(importance or 0.6)))
        if _sw("memory.emotion_weight", True):
            imp = min(0.98, imp + 0.15 * sal)
        cur = self.conn.execute(
            "INSERT INTO memories(scope,speaker_id,kind,content,importance,"
            "confidence,strength,created_at,updated_at,status,source,subject,audience,"
            "layer,emo_valence,emo_arousal,emo_affection,emo_salience)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.scope, speaker, "qa", content, round(imp, 2), 0.85,
             1.0, now, now, "active", "model", subject, aud,
             "semantic", emo["v"], emo["a"], emo["f"], sal))
        new_id = int(cur.lastrowid)
        if conflict is not None:
            if bool(conflict["pinned"]):
                self._queue_conflict(speaker, aud, int(conflict["id"]), new_id,
                                     "model fact conflicts with pinned memory")
            else:
                self.supersede(int(conflict["id"]), new_id)
        self._queue_embed(new_id, content)
        # 主题级改口（C3）：subject 抓不到的（口味/健康/爱好…）在这里补挂待确认
        try:
            self._check_fact_change(speaker, aud, new_id, content, subject)
        except Exception:
            pass
        self.conn.commit()
        return [content]


    # ------------------------------------------------------------ 每晚整理

    @_mback.backup_before("consolidate")
    def consolidate(self, summarizer=None) -> dict[str, int]:
        """夜间巩固：去重合并、四层晋升、长期摘要、核心候选、联想边。

        只在同一个 scope + speaker + audience 内操作，绝不跨会话合并；
        永久记忆只作为摘要素材，不会被合并或替代。
        summarizer 可选：bot 传入模型回调时做归纳，不传就退回抽取式拼接。
        """
        stats = {"merged": 0, "digests": 0, "promoted": 0, "links": 0,
                 "core_added": 0, "core_stale": 0,
                 "recall_pruned": 0, "conflicts_pruned": 0}
        if not _sw("memory.consolidate", True):
            return stats
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " ORDER BY pinned DESC, importance DESC, id DESC", (self.scope,)
        ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            key = (str(r["audience"] or "global"), str(r["speaker_id"] or ""))
            groups.setdefault(key, []).append(r)

        for (aud, speaker), group in groups.items():
            kept: list[sqlite3.Row] = []
            for r in group:
                pinned = bool(r["pinned"])
                if pinned or str(r["kind"]) == "summary":
                    kept.append(r)
                    continue
                fact = _fact_text(r["content"])
                grams = _grams(fact)
                dup: sqlite3.Row | None = None
                for k in kept:
                    if bool(k["pinned"]) or str(k["kind"]) == "summary":
                        continue
                    k_fact = _fact_text(k["content"])
                    if _norm_memory(k_fact) == _norm_memory(fact):
                        dup = k
                        break
                    if _overlap(grams, _grams(k_fact)) >= 0.92:
                        dup = k
                        break
                if dup is None:
                    kept.append(r)
                    continue
                # 把重复条目的“使用证据”并到保留条目，旧条目只标记，不物理删
                self.conn.execute(
                    "UPDATE memories SET strength=MIN(1.0,MAX(strength,?)),"
                    " access_count=MAX(access_count,?), importance=MAX(importance,?),"
                    " confidence=MAX(confidence,?), updated_at=MAX(updated_at,?) WHERE id=?",
                    (float(r["strength"]), int(r["access_count"]),
                     float(r["importance"]), float(r["confidence"]),
                     float(r["updated_at"]), int(dup["id"])))
                self.conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?,"
                    " updated_at=? WHERE id=?",
                    (int(dup["id"]), _now(), int(r["id"])))
                stats["merged"] += 1

            # 四层晋升：被反复想起、够重要的情景记忆升成语义记忆
            if _sw("memory.layer_promote", True):
                for r in kept:
                    if self._layer_of(r) != "episodic":
                        continue
                    if (int(r["access_count"]) >= 2
                            and float(r["importance"]) >= 0.55
                            and float(r["confidence"]) >= 0.7):
                        self.conn.execute(
                            "UPDATE memories SET layer='semantic' WHERE id=?",
                            (int(r["id"]),))
                        stats["promoted"] += 1

            if _sw("memory.abstract", True):
                active = [r for r in kept if str(r["kind"]) != "summary"]
                if len(active) >= 3:
                    top = sorted(
                        active,
                        key=lambda x: (float(x["importance"]), int(x["access_count"]),
                                       float(x["updated_at"])),
                        reverse=True)[:6]
                    body, used_model = self._digest_body(top, summarizer)
                    dkey = hashlib.sha1(_norm_memory(body).encode("utf-8")).hexdigest()[:16]
                    last = self.conn.execute(
                        "SELECT digest_key FROM memory_digests WHERE scope=? AND"
                        " speaker_id=? AND audience=? AND COALESCE(level,'daily')='daily'"
                        " ORDER BY id DESC LIMIT 1",
                        (self.scope, speaker, aud)).fetchone()
                    if last is None or str(last["digest_key"]) != dkey:
                        day = time.strftime("%Y-%m-%d")
                        self.conn.execute(
                            "INSERT INTO memory_digests(scope,speaker_id,audience,"
                            "content,source_ids,digest_key,created_at,level,"
                            "period_start,period_end,model)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (self.scope, speaker, aud, body,
                             json.dumps([int(x["id"]) for x in top]),
                             dkey, _now(), "daily", day, day,
                             "model" if used_model else "extractive"))
                        stats["digests"] += 1
                        # 每组只留最近 5 份日报摘要，避免无限增长
                        self.conn.execute(
                            "DELETE FROM memory_digests WHERE scope=? AND speaker_id=?"
                            " AND audience=? AND COALESCE(level,'daily')='daily'"
                            " AND id NOT IN (SELECT id FROM memory_digests"
                            " WHERE scope=? AND speaker_id=? AND audience=?"
                            " AND COALESCE(level,'daily')='daily'"
                            " ORDER BY id DESC LIMIT 5)",
                            (self.scope, speaker, aud,
                             self.scope, speaker, aud))

            if _sw("memory.core_queue", True):
                for r in kept:
                    if bool(r["pinned"]) or str(r["kind"]) == "summary":
                        continue
                    if float(r["importance"]) < 0.72 and int(r["access_count"]) < 3:
                        continue
                    if self._strength(r, _now()) < 0.3:
                        continue
                    score = (0.6 * float(r["importance"])
                             + 0.25 * min(1.0, int(r["access_count"]) / 5.0)
                             + 0.15 * min(1.0, max(0.0, float(r["updated_at"])) / _now()))
                    reason = (f"importance={float(r['importance']):.2f},"
                              f"access={int(r['access_count'])}")
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO core_queue(scope,memory_id,score,reason,"
                        "status,created_at) VALUES(?,?,?,?,'pending',?)",
                        (self.scope, int(r["id"]), round(score, 3), reason, _now()))
                    if cur.rowcount:
                        stats["core_added"] += 1

        stats["links"] = self._rebuild_links()
        stats["digests"] += self._rollup_digests(summarizer)

        cur = self.conn.execute(
            "DELETE FROM recall_log WHERE scope=? AND created_at<?",
            (self.scope, _now() - 30 * 86400))
        stats["recall_pruned"] = cur.rowcount
        # 已被合并/遗忘的记忆不能再出现在待审队列里，否则批准会把旧条目复活
        cur = self.conn.execute(
            "UPDATE core_queue SET status='rejected', reviewed_at=? WHERE scope=?"
            " AND status='pending' AND memory_id NOT IN"
            " (SELECT id FROM memories WHERE scope=? AND status='active')",
            (_now(), self.scope, self.scope))
        stats["core_stale"] = cur.rowcount
        cur = self.conn.execute(
            "DELETE FROM conflicts WHERE scope=? AND status!='pending' AND created_at<?",
            (self.scope, _now() - 90 * 86400))
        stats["conflicts_pruned"] = cur.rowcount
        self.conn.commit()
        return stats

    @staticmethod
    def _digest_body(top: list[sqlite3.Row], summarizer=None) -> tuple[str, bool]:
        """归纳摘要：有模型回调就归纳，没有就抽取拼接。返回（正文, 是否模型）。"""
        lines = [f"- {_squash(_fact_text(str(x['content'])), 120)}" for x in top]
        if callable(summarizer):
            try:
                got = str(summarizer(lines) or "").strip()
                if len(got) >= 8:
                    return _squash(got, 500), True
            except Exception:  # noqa: BLE001
                pass
        body = "；".join(_squash(_fact_text(str(x["content"])), 80) for x in top)
        return _squash(body, 500), False

    def _digest_row(self, speaker: str, audience: str, level: str,
                    period_start: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM memory_digests WHERE scope=? AND speaker_id=? AND audience=?"
            " AND COALESCE(level,'daily')=? AND period_start=? ORDER BY id DESC LIMIT 1",
            (self.scope, speaker, audience, level, period_start)).fetchone()

    @staticmethod
    def _merge_digests(rows: list[sqlite3.Row], summarizer=None) -> tuple[str, str]:
        lines = [f"- {_squash(str(r['content']), 160)}" for r in rows]
        if callable(summarizer):
            try:
                got = str(summarizer(lines) or "").strip()
                if len(got) >= 8:
                    return _squash(got, 500), "model"
            except Exception:  # noqa: BLE001
                pass
        body = "；".join(_squash(str(r["content"]), 120) for r in rows)
        return _squash(body, 500), "extractive"

    def _rollup_digests(self, summarizer=None) -> int:
        """把日报摘要卷成周摘要、周摘要卷成月摘要；只升一层，不丢来源。"""
        if not _sw("memory.abstract", True):
            return 0
        import datetime as _dt
        now = _dt.datetime.now()
        week_start = (now - _dt.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_end = (now + _dt.timedelta(days=6 - now.weekday())).strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")
        month_end = ((now.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
                     - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        made = 0
        groups = self.conn.execute(
            "SELECT DISTINCT speaker_id, audience FROM memory_digests WHERE scope=?",
            (self.scope,)).fetchall()
        for g in groups:
            spk, aud = str(g["speaker_id"]), str(g["audience"])
            dailies = self.conn.execute(
                "SELECT * FROM memory_digests WHERE scope=? AND speaker_id=? AND audience=?"
                " AND COALESCE(level,'daily')='daily'"
                " AND period_start>=? AND period_start<=? ORDER BY id DESC LIMIT 7",
                (self.scope, spk, aud, week_start, week_end)).fetchall()
            if len(dailies) >= 2:
                body, model = self._merge_digests(dailies, summarizer)
                dkey = hashlib.sha1(_norm_memory(body).encode("utf-8")).hexdigest()[:16]
                existing = self._digest_row(spk, aud, "weekly", week_start)
                ids = json.dumps([int(r["id"]) for r in dailies])
                if existing is None:
                    self.conn.execute(
                        "INSERT INTO memory_digests(scope,speaker_id,audience,content,"
                        "source_ids,digest_key,created_at,level,period_start,period_end,model)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (self.scope, spk, aud, body, ids, dkey, _now(),
                         "weekly", week_start, week_end, model))
                    made += 1
                elif str(existing["digest_key"]) != dkey:
                    # 本周又添了新日报：周摘要跟着刷新，而不是第一天就定死
                    self.conn.execute(
                        "UPDATE memory_digests SET content=?,source_ids=?,digest_key=?,"
                        "created_at=?,model=? WHERE id=?",
                        (body, ids, dkey, _now(), model, int(existing["id"])))
                    made += 1
            weeklies = self.conn.execute(
                "SELECT * FROM memory_digests WHERE scope=? AND speaker_id=? AND audience=?"
                " AND COALESCE(level,'daily')='weekly'"
                " AND period_start>=? AND period_start<=? ORDER BY id DESC LIMIT 5",
                (self.scope, spk, aud, month_start, month_end)).fetchall()
            if len(weeklies) >= 2:
                body, model = self._merge_digests(weeklies, summarizer)
                dkey = hashlib.sha1(_norm_memory(body).encode("utf-8")).hexdigest()[:16]
                existing = self._digest_row(spk, aud, "monthly", month_start)
                ids = json.dumps([int(r["id"]) for r in weeklies])
                if existing is None:
                    self.conn.execute(
                        "INSERT INTO memory_digests(scope,speaker_id,audience,content,"
                        "source_ids,digest_key,created_at,level,period_start,period_end,model)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (self.scope, spk, aud, body, ids, dkey, _now(),
                         "monthly", month_start, month_end, model))
                    made += 1
                elif str(existing["digest_key"]) != dkey:
                    self.conn.execute(
                        "UPDATE memory_digests SET content=?,source_ids=?,digest_key=?,"
                        "created_at=?,model=? WHERE id=?",
                        (body, ids, dkey, _now(), model, int(existing["id"])))
                    made += 1
        for level, keep in (("weekly", 8), ("monthly", 6)):
            self.conn.execute(
                "DELETE FROM memory_digests WHERE scope=? AND COALESCE(level,'daily')=?"
                " AND id NOT IN (SELECT id FROM memory_digests WHERE scope=?"
                " AND COALESCE(level,'daily')=? ORDER BY id DESC LIMIT ?)",
                (self.scope, level, self.scope, level, keep))
        self.conn.commit()
        return made

    def _rebuild_links(self) -> int:
        """差序联想边：同一人 + 同一会话内，同主题或字面相近的记忆连一条边。

        每个点最多 6 条边；只存 a_id<b_id 一次，召回时双向查。
        """
        if not _sw("recall.assoc", True):
            return 0
        # 联想边是派生数据：每次先清掉本 scope 的旧边，再按当前记忆重建，
        # 避免“早就不像了”的边一直留在库里。
        self.conn.execute(
            "DELETE FROM memory_links WHERE a_id IN"
            " (SELECT id FROM memories WHERE scope=?) OR b_id IN"
            " (SELECT id FROM memories WHERE scope=?)",
            (self.scope, self.scope))
        rows = self.conn.execute(
            "SELECT id,speaker_id,audience,subject,content FROM memories"
            " WHERE scope=? AND status='active' ORDER BY id DESC LIMIT 1500",
            (self.scope,)).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((str(r["speaker_id"] or ""),
                               str(r["audience"] or "global")), []).append(r)
        now = _now()
        made = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            group = group[:240]
            grams = {int(r["id"]): _grams(_fact_text(str(r["content"])))
                     for r in group}
            degree: dict[int, int] = {}
            for i, a in enumerate(group):
                aid = int(a["id"])
                if degree.get(aid, 0) >= 6:
                    continue
                for b in group[i + 1:]:
                    bid = int(b["id"])
                    if degree.get(bid, 0) >= 6:
                        continue
                    weight = 0.0
                    kind = ""
                    if a["subject"] and str(a["subject"]) == str(b["subject"]):
                        weight, kind = 0.9, "topic"
                    else:
                        ov = _overlap(grams[aid], grams[bid])
                        if ov >= 0.25:
                            weight, kind = round(ov, 3), "similar"
                    if not kind:
                        continue
                    self.conn.execute(
                        "INSERT OR REPLACE INTO memory_links(a_id,b_id,kind,weight,updated_at)"
                        " VALUES(?,?,?,?,?)",
                        (min(aid, bid), max(aid, bid), kind, weight, now))
                    degree[aid] = degree.get(aid, 0) + 1
                    degree[bid] = degree.get(bid, 0) + 1
                    made += 1
                    if degree.get(aid, 0) >= 6:
                        break
        self.conn.commit()
        return made

    @_mback.backup_before("nightly")
    def nightly(self, summarizer=None) -> dict[str, int]:
        """每晚结算遗忘 + 去重/摘要/联想边；摘要优先交给模型归纳。"""
        now = _now()
        stats = {"expired_grudges": 0, "faded": 0, "pruned_events": 0}
        cur = self.conn.execute(
            "UPDATE grudges SET status='expired' WHERE status='open' AND expires_at<=?", (now,))
        stats["expired_grudges"] = cur.rowcount
        rows = (self.conn.execute("SELECT * FROM memories WHERE status='active'").fetchall()
                if _sw("forget.enabled") else [])
        fade_strength = _sw_num("forget.fade_strength", 0.05)
        fade_importance = _sw_num("forget.fade_importance", 0.3)
        for r in rows:
            pinned = bool(r["pinned"])
            s = self._strength(r, now)
            if s < fade_strength and float(r["importance"]) < fade_importance and not pinned:
                self.conn.execute(
                    "UPDATE memories SET status='faded', strength=?, updated_at=? WHERE id=?",
                    (s, now, r["id"]))
                stats["faded"] += 1
            else:
                self.conn.execute(
                    "UPDATE memories SET strength=? WHERE id=?", (s, r["id"]))
        cur = self.conn.execute(
            "DELETE FROM emotion_events WHERE created_at<?", (now - 90 * 86400,))
        stats["pruned_events"] = cur.rowcount
        self.conn.commit()
        try:
            stats.update(self.consolidate(summarizer))
        except Exception as e:  # noqa: BLE001
            _log_embed(f"consolidate failed: {type(e).__name__}: {e}")
        # 每晚补一批向量，慢慢把历史记忆补齐
        try:
            stats["embedded"] = self.backfill() if _sw("recall.semantic") else 0
            if stats["embedded"]:
                _log_embed(f"nightly embedded {stats['embedded']}")
        except Exception:
            stats["embedded"] = 0
        try:
            _mback.prune_undo(self.conn)
        except Exception:  # noqa: BLE001
            pass
        return stats

    def latest_digest(self, speaker: str, audience: str = "") -> str:
        """按 月 → 周 → 日 取最新摘要拼成一段；没有就空。"""
        aud = self._audience(speaker, audience)
        parts: list[str] = []
        try:
            for level, label in (("monthly", "月"), ("weekly", "周"), ("daily", "日")):
                row = self.conn.execute(
                    "SELECT content FROM memory_digests WHERE scope=? AND speaker_id=?"
                    " AND audience=? AND COALESCE(level,'daily')=?"
                    " ORDER BY id DESC LIMIT 1",
                    (self.scope, speaker, aud, level)).fetchone()
                if row:
                    parts.append(f"[{label}] {row['content']}")
            return "\n".join(parts)
        except Exception:
            return ""

    def core_candidates(self, limit: int = 50, speaker: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        sql = ("SELECT q.id AS qid, q.memory_id, q.score, q.reason, q.created_at,"
               " m.content, m.speaker_id, m.audience, m.importance, m.access_count"
               " FROM core_queue q JOIN memories m ON m.id=q.memory_id"
               " WHERE q.scope=? AND q.status='pending' AND m.status='active'"
               + (" AND m.speaker_id=?" if speaker else "")
               + " ORDER BY q.score DESC, q.id DESC LIMIT ?")
        args: list[Any] = [self.scope]
        if speaker:
            args.append(speaker)
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    @_mback.backup_before("approve-core")
    def approve_core(self, queue_id: int) -> bool:
        row = self.conn.execute(
            "SELECT * FROM core_queue WHERE id=? AND scope=?",
            (int(queue_id), self.scope)).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE core_queue SET status='approved', reviewed_at=? WHERE id=?",
            (_now(), int(queue_id)))
        self.conn.execute(
            "UPDATE memories SET pinned=1, status='active', strength=1.0, updated_at=?"
            " WHERE id=? AND scope=?",
            (_now(), int(row["memory_id"]), self.scope))
        self.conn.commit()
        return True

    @_mback.backup_before("reject-core")
    def reject_core(self, queue_id: int) -> bool:
        row = self.conn.execute(
            "SELECT id FROM core_queue WHERE id=? AND scope=?",
            (int(queue_id), self.scope)).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE core_queue SET status='rejected', reviewed_at=? WHERE id=?",
            (_now(), int(queue_id)))
        self.conn.commit()
        return True

    def pending_conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        return [dict(r) for r in self.conn.execute(
            "SELECT c.*, o.content AS old_content, n.content AS new_content"
            " FROM conflicts c JOIN memories o ON o.id=c.old_id"
            " JOIN memories n ON n.id=c.new_id"
            " WHERE c.scope=? AND c.status='pending'"
            " ORDER BY c.id DESC LIMIT ?", (self.scope, limit))]

    @_mback.backup_before("resolve-conflict")
    def resolve_conflict(self, conflict_id: int, action: str) -> bool:
        """人工选边：keep_old 保留旧值，keep_new 让新值生效。"""
        if action not in ("keep_old", "keep_new"):
            return False
        row = self.conn.execute(
            "SELECT * FROM conflicts WHERE id=? AND scope=? AND status='pending'",
            (int(conflict_id), self.scope)).fetchone()
        if row is None:
            return False
        old_id, new_id = int(row["old_id"]), int(row["new_id"])
        now = _now()
        # keep_old = 保留旧值 → 新的作废；keep_new = 让新值生效 → 旧的作废。
        # 两边都同步维护 valid_to（C4），并保证胜出方回到 active，避免“两边都没了”。
        if action == "keep_old":
            loser, winner = new_id, old_id
        else:
            loser, winner = old_id, new_id
        self.conn.execute(
            "UPDATE memories SET status='superseded', superseded_by=?, valid_to=?,"
            " updated_at=? WHERE id=? AND scope=?",
            (winner, now, now, loser, self.scope))
        self.conn.execute(
            "UPDATE memories SET status='active', superseded_by=NULL, valid_to=NULL,"
            " updated_at=? WHERE id=? AND scope=?",
            (now, winner, self.scope))
        self.conn.execute(
            "UPDATE conflicts SET status=?, resolved_at=? WHERE id=?",
            (action, now, int(conflict_id)))
        self.conn.commit()
        return True

    # ------------------------------------------------------------ 提示词块

    def block(self, speaker: str, user_text: str = "", max_chars: int = BLOCK_MAX_CHARS,
              audience: str = "", skip_mood: bool = False) -> str:
        aud = self._audience(speaker, audience)
        self.ingest(speaker, user_text, store=False, audience=aud)   # 只识别；真正的记忆等回复生成后成对写入
        obs = self.observe(speaker, user_text)
        st = obs["state"]
        mood_on = _sw("emotion.enabled")
        lines: list[str] = []
        if _sw("memory.core_context", True):
            lines.append(core_context(speaker))
        if mood_on and not skip_mood:
            lines.append("【你此刻的状态】")
            lines.append(self.mood_line(speaker))

        pending = self._open_grudges(speaker) if (mood_on and _sw("emotion.grudge")) else []
        if pending:
            items = "；".join(
                f"{_squash(g['cause'], 18)}（{int(g['severity'])} 级）" for g in pending[:3])
            lines.append(f"小本本：{items}")
        if mood_on:
            for ev in obs["events"]:
                if ev.get("kind") == "grudge":
                    lines.append("刚才那句记小本本了——回得冷一点，别马上翻篇。")
                elif ev.get("kind") == "apology" and ev.get("repaired"):
                    lines.append("他道歉了，气可以消一半，但别立刻热情。")

        mems = (self.recall(speaker, user_text, audience=aud)
                if (user_text and _sw("recall.enabled")) else [])
        if _sw("recall.digest", True):
            digest = self.latest_digest(speaker, aud)
            if digest:
                lines.append("【长期摘要·月/周/日】" + _squash(digest, 260))
        if mems:
            lines.append("【想起来的】")
            for m in mems:
                tag = m.get("layer_label") or "记忆"
                lines.append(f"· [{tag}] {_squash(m['content'], 46)}"
                             f"（可信 {m['confidence']:.1f}）")

        lines.append("这些是背景状态，别逐字复述，也别解释机制。")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


_LOCAL = threading.local()


def _read_config(here: str) -> dict:
    """companion.config.json：{db, scope}；坏了就当没有。"""
    try:
        with open(os.path.join(here, "companion.config.json"), "r", encoding="utf-8") as f:
            import json
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _default_brain() -> Brain | None:
    """每个线程一个 Brain 连接。

    sqlite3 连接默认不能跨线程用：主线程拼提示词、后台线程写记忆，共用同一个
    全局 Brain 会抛 ProgrammingError，被 remember_* 吞掉，表现就是「怎么聊都记不住」。
    线程各开一条连接，短线程用完调用 close_thread_brain() 关掉。
    """
    brain = getattr(_LOCAL, "brain", None)
    if brain is None:
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = _read_config(here)
        db = os.environ.get("COMPANION_DB") or cfg.get("db") or os.path.join(here, "brain.db")
        if not os.path.isabs(str(db)):
            db = os.path.join(here, str(db))
        scope = os.environ.get("COMPANION_SCOPE") or cfg.get("scope") or "default"
        brain = Brain(str(db), str(scope))
        _LOCAL.brain = brain
    return brain


def close_thread_brain() -> None:
    """关掉当前线程的 Brain 连接；短命线程（比如回复后的写记忆线程）用完就调。"""
    brain = getattr(_LOCAL, "brain", None)
    if brain is not None:
        try:
            brain.close()
        except Exception:  # noqa: BLE001
            pass
        _LOCAL.brain = None


def remember_fact(user_id: str, question: str, answer: str,
                  fact: str, importance: float = 0.6,
                  audience: str = "") -> list[str]:
    """给 bot 用：模型判定值得记之后，把「问 + 答 + 事实」一起写入。"""
    try:
        if not user_id or not fact:
            return []
        return _default_brain().ingest_model_pair(
            str(user_id), question, answer, fact, importance, audience=audience)
    except Exception:
        return []


def parse_memory_json(raw: str) -> dict | None:
    """从模型输出里抠出 JSON（它会加解释、代码块、前后废话）。"""
    import json as _json
    text = (raw or '').strip()
    if not text:
        return None
    text = re.sub(r'^`(?:json)?|`$', '', text, flags=re.M).strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        got = _json.loads(m.group(0))
        return got if isinstance(got, dict) else None
    except _json.JSONDecodeError:
        return None


def remember_exchange(user_id: str, question: str, answer: str,
                      audience: str = "") -> list[str]:
    """给 bot 用：一轮问答结束后，把「问 + 答」成对存成一条记忆。异常一律吞掉。"""
    try:
        if not user_id:
            return []
        return _default_brain().ingest_pair(
            str(user_id), question, answer, audience=audience)
    except Exception:
        return []


def block_for_prompt(user_id: str, user_text: str = "",
                     audience: str = "", skip_mood: bool = False) -> str:
    """给 bot.system_prompt() 调的唯一入口：任何异常都返回空串。"""
    try:
        if not user_id:
            return ""
        return _default_brain().block(str(user_id), user_text, audience=audience,
                                      skip_mood=skip_mood)
    except Exception:
        return ""


def run_nightly(force: bool = False, summarizer=None) -> dict[str, Any]:
    """给 bot 的每日线程用：新建连接，避免跨线程复用 SQLite 连接。

    同一天成功跑过就把日期写进 meta；bot 半夜重启也不会重复整理。
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = _read_config(here)
        db = os.environ.get("COMPANION_DB") or cfg.get("db") or os.path.join(here, "brain.db")
        if not os.path.isabs(str(db)):
            db = os.path.join(here, str(db))
        scope = os.environ.get("COMPANION_SCOPE") or cfg.get("scope") or "default"
        b = Brain(str(db), str(scope))
        try:
            today = time.strftime("%Y-%m-%d")
            marker = "nightly_day:" + str(b.scope)
            if not force:
                row = b.conn.execute(
                    "SELECT value FROM meta WHERE key=?", (marker,)).fetchone()
                if row is not None and str(row["value"]) == today:
                    return {"skipped": 1, "day": today}
            stats = b.nightly(summarizer)
            b.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                (marker, today))
            b.conn.commit()
            return stats
        finally:
            b.close()
    except Exception as e:  # noqa: BLE001
        _log_embed(f"run_nightly failed: {type(e).__name__}: {e}")
        return {}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="伴生大脑：夜间整理 / 召回日志 / 核心候选")
    ap.add_argument("--db", default=None, help="brain.db 路径，默认模块同目录")
    ap.add_argument("--scope", default=None, help="人格命名空间，默认 default")
    ap.add_argument("--backfill", type=int, nargs="?", const=-1, default=None,
                    help="补齐向量后退出：不带数字=全部补齐，带 N=只补 N 条")
    ap.add_argument("--recall-log", type=int, nargs="?", const=20, default=None,
                    help="查看最近 N 条召回日志")
    ap.add_argument("--core-list", type=int, nargs="?", const=20, default=None,
                    help="查看待审核心候选")
    ap.add_argument("--core-approve", type=int, default=None, help="批准一个核心候选（队列 id）")
    ap.add_argument("--core-reject", type=int, default=None, help="拒绝一个核心候选（队列 id）")
    ap.add_argument("--conflicts", type=int, nargs="?", const=20, default=None,
                    help="查看待处理冲突")
    ap.add_argument("--conflict-keep-old", type=int, default=None,
                    help="冲突选边：保留旧值（冲突 id）")
    ap.add_argument("--conflict-keep-new", type=int, default=None,
                    help="冲突选边：让新值生效（冲突 id）")
    ap.add_argument("--revise", type=int, default=None, metavar="ID",
                    help="纠正一条记忆的正文（配合 --text）")
    ap.add_argument("--text", default=None, help="纠正后的新正文")
    ap.add_argument("--supersede-old", action="store_true",
                    help="纠正时顺带把同主题旧记录标为过期")
    args = ap.parse_args()
    _db = args.db or os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain.db")
    _b = Brain(_db, args.scope or os.environ.get("COMPANION_SCOPE", "default"))
    if args.backfill is not None:
        print(json.dumps({"db": _db, "embedded": _b.backfill(None if args.backfill < 0 else args.backfill)},
                         ensure_ascii=False))
    elif args.recall_log is not None:
        print(json.dumps(_b.recall_log(args.recall_log), ensure_ascii=False, indent=2))
    elif args.core_list is not None:
        print(json.dumps(_b.core_candidates(args.core_list), ensure_ascii=False, indent=2))
    elif args.core_approve is not None:
        print(json.dumps({"approved": _b.approve_core(args.core_approve)}, ensure_ascii=False))
    elif args.core_reject is not None:
        print(json.dumps({"rejected": _b.reject_core(args.core_reject)}, ensure_ascii=False))
    elif args.conflict_keep_old is not None:
        print(json.dumps({"resolved": _b.resolve_conflict(
            args.conflict_keep_old, "keep_old")}, ensure_ascii=False))
    elif args.conflict_keep_new is not None:
        print(json.dumps({"resolved": _b.resolve_conflict(
            args.conflict_keep_new, "keep_new")}, ensure_ascii=False))
    elif args.conflicts is not None:
        print(json.dumps(_b.pending_conflicts(args.conflicts), ensure_ascii=False, indent=2))
    elif args.revise is not None:
        print(json.dumps(_b.revise(args.revise, args.text or "",
                                   supersede_old=args.supersede_old),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"db": _db, "scope": _b.scope, "nightly": _b.nightly()},
                         ensure_ascii=False))