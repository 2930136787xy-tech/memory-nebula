# -*- coding: utf-8 -*-
"""把现有真实数据（会话/日期/消息/人格/记忆）导成一张图，给 3D 页面用。

数据来源全是本机已有文件，不造数据：
  data/memory.json   每日摘要（会话 -> 天 -> 摘要）
  data/history.json  最近的原始对话
  bridge.db          共用记忆（QQ 每轮问答都写，页面里橙色记忆球的主体）
  brain.db           伴生大脑提炼过的事实 + 情绪 + 向量
  personas/*/       人格档案
  config.json        admins/backend/known_users -> 核心 / 熟识 / 陌生三层关系

新增节点类型：在 NODE_TYPES 里加一条，前端 TYPES 里加一条即可。

用法：
    python memory_view.py            # 生成 web/graph.json
    python memory_view.py --stats    # 只看统计
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("NEBULA_DATA") or os.path.join(HERE, "data")
WEB = os.path.join(HERE, "web")
DB = os.environ.get("VIEW_DB") or os.path.join(HERE, "brain.db")
CORE_ID = "core:system"
# 与系统/管理员相关的文字，用来把记忆分到核心还是外围。
# 不依赖 embedding 走本地还是云端：两份记忆库读出来后用同一套规则打标。
CORE_KEYWORDS = (
    "系统", "管理员", "机器人", "指令", "权限", "后台",
    "bot", "admin", "system",
)
# 关系圈层：core=管理员/系统核心，inner=同学/熟人，outer=陌生/暂时不熟。
# 关系判定只看行为证据，不调用模型，也不分本地/云端。
RELATION_KEYWORDS = (
    "同学", "朋友", "熟人", "舍友", "室友", "同事", "老乡",
    "发小", "闺蜜", "哥们", "心上人", "认识你", "认识我",
)
KNOWN_MIN_MESSAGES = 20      # 聊过至少 20 条，算“有来往”
KNOWN_MIN_MEMORIES = 10      # 或在这份关系里留下至少 10 条记忆


def _shared_db_path() -> str:
    """共用记忆库的位置：环境变量 SHARED_DB 优先，否则用仓库根目录的 bridge.db。

    原项目里这份库由 QQ 机器人实时写入；独立运行时把它放在仓库根目录，
    `python demo/make_demo.py` 会生成一份可直接出图的示例库。
    """
    env = os.environ.get("SHARED_DB")
    if env:
        return env
    return os.path.join(HERE, "bridge.db")


SHARED_DB = _shared_db_path()

# 节点类型注册表：加类型只改这里 + 前端 TYPES
NODE_TYPES: dict[str, dict[str, Any]] = {
    "core": {"color": "#ffd166", "size": 18, "label": "记忆核心"},
    "persona": {"color": "#ff6b9d", "size": 11, "label": "人格"},
    "conv": {"color": "#4da3ff", "size": 8, "label": "会话"},
    "day": {"color": "#9b8cff", "size": 5, "label": "日期"},
    "msg": {"color": "#58d68d", "size": 2.6, "label": "消息"},
    "summary": {"color": "#2dd4bf", "size": 4.5, "label": "日摘要"},
    "memory": {"color": "#ffb84d", "size": 5, "label": "记忆"},
}
# 永久记忆单独用一个颜色，和「更快忘 / 更难忘」的明暗区分开
PIN_COLOR = "#e879f9"


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: str, data: Any) -> None:
    """先写临时文件再替换，避免页面改关系时把 config.json 写坏。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    # 原子替换会生成新文件并继承目录 ACL；config.json 含 API token，替换后要重新收紧。
    try:
        import accounts
        accounts._harden(path)
    except Exception:
        pass


def _strength_now(m: dict, now: float) -> float:
    """与 companion_brain.Brain._strength 同一公式：越重要忘得越慢。"""
    base = float(m.get("strength") or 1.0)
    imp = float(m.get("importance") or 0.5)
    days = max(0.0, (now - float(m.get("updated_at") or now)) / 86400.0)
    half_life = 7.0 + 83.0 * imp
    return base * (0.5 ** (days / half_life))


def _num(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _speaker_of(key: str) -> str:
    """会话 key 里取出说话人 QQ：private:QQ / group:群号:QQ。"""
    if key.startswith("private:"):
        return key.split(":", 1)[1]
    if key.startswith("group:"):
        parts = key.split(":")
        return parts[2] if len(parts) > 2 else ""
    return ""


def _conv_label(key: str, nicks: dict[str, str]) -> str:
    if key.startswith("private:"):
        qq = key.split(":", 1)[1]
        return f"私聊 · {nicks.get(qq) or qq}"
    if key.startswith("group:"):
        parts = key.split(":")
        gid = parts[1] if len(parts) > 1 else "?"
        uid = parts[2] if len(parts) > 2 else ""
        # 同一个群里可能有多个人各存一条记忆，带上对方昵称/QQ 才分得清
        if uid:
            return f"群 {gid} · {nicks.get(uid) or uid}"
        return f"群 {gid}"
    return key


def _load_nicks() -> dict[str, str]:
    out: dict[str, str] = {}
    root = os.path.join(HERE, "personas")
    if not os.path.isdir(root):
        return out
    for qq in os.listdir(root):
        meta = _read_json(os.path.join(root, qq, "meta.json"), {})
        if isinstance(meta, dict):
            # 昵称可能为空：QQ 号仍然是凭证，照样要出球，只是显示成「QQ 号」
            out[str(meta.get("qq") or qq)] = str(meta.get("nick") or "")
    return out


def _load_core_config() -> tuple[list[str], str]:
    """读取主配置里的管理员和当前后端。

    管理员是“系统核心”的锚点；后端只用于展示，核心/外围的判定对
    local / cloud 完全一致，切换后端不会让图换一套结构。
    """
    cfg = _read_json(os.path.join(HERE, "config.json"), {})
    admins: list[str] = []
    for item in (cfg.get("admins") or []):
        qq = str(item or "").strip()
        if qq and qq not in admins:
            admins.append(qq)
    backend = str(cfg.get("backend") or "local").strip().lower()
    return admins, backend


def _core_reason(text: str, speaker: str, conv_key: str,
                 admins: list[str], admin_nicks: list[str]) -> str:
    """返回这条内容为什么属于核心；空字符串表示外围。"""
    if speaker and speaker in admins:
        return "管理员本人"
    parts = str(conv_key or "").split(":")
    if any(a in parts[1:] for a in admins):
        return "管理员会话"
    raw = str(text or "")
    low = raw.lower()
    for qq in admins:
        if qq and qq in raw:
            return "提到管理员 QQ"
    for nick in admin_nicks:
        if nick and nick in raw:
            return "提到管理员"
    for kw in CORE_KEYWORDS:
        if kw and kw in low:
            return "系统/权限相关"
    return ""


def _core_related(text: str, speaker: str, conv_key: str,
                  admins: list[str], admin_nicks: list[str]) -> bool:
    return bool(_core_reason(text, speaker, conv_key, admins, admin_nicks))


def _config_users(key: str) -> set[str]:
    cfg = _read_json(os.path.join(HERE, "config.json"), {})
    out: set[str] = set()
    for item in (cfg.get(key) or []):
        qq = str(item or "").strip()
        if qq:
            out.add(qq)
    return out


def _known_from_config() -> set[str]:
    """config.json 里可选的 known_users，用来手动把某个人放进“熟人圈”。"""
    return _config_users("known_users")


_RELATION_LOCK = threading.Lock()


def set_relation_override(qq: Any, ring: str) -> dict[str, Any]:
    """手动改一个人落在哪一圈，写回 config.json。

    ring = inner（熟识）/ outer（陌生）/ auto（清掉手动指定，恢复行为判定）。
    管理员始终由 admins 决定，不写进关系覆盖。
    """
    qq = str(qq or "").strip()
    ring = str(ring or "").strip().lower()
    if not qq or not qq.isdigit() or len(qq) > 16:
        raise ValueError("QQ 号不合法")
    if ring not in ("inner", "outer", "auto"):
        raise ValueError("ring 只能是 inner / outer / auto")
    path = os.path.join(HERE, "config.json")
    with _RELATION_LOCK:
        cfg = _read_json(path, {})
        if not isinstance(cfg, dict):
            raise ValueError("config.json 不是对象，先修好配置")
        admins = {str(x or "").strip() for x in (cfg.get("admins") or [])}
        if qq in admins and ring != "auto":
            raise ValueError("管理员固定在核心圈，不能改成熟识或陌生")

        def _drop(key: str) -> list[str]:
            vals = [str(x).strip() for x in (cfg.get(key) or [])
                    if str(x or "").strip() and str(x).strip() != qq]
            if vals:
                cfg[key] = vals
            else:
                cfg.pop(key, None)
            return vals

        known = _drop("known_users")
        strangers = _drop("stranger_users")
        if ring == "inner":
            known = known + [qq]
            cfg["known_users"] = known
        elif ring == "outer":
            strangers = strangers + [qq]
            cfg["stranger_users"] = strangers
        _write_json_atomic(path, cfg)
    return {"ok": True, "qq": qq, "ring": ring,
            "known_users": known, "stranger_users": strangers}


def _persona_text(qq: str) -> str:
    """读这个人最新一版人格档案，只用于关系词判断；读不到就空。"""
    root = os.path.join(HERE, "personas", str(qq))
    try:
        files = sorted(
            (os.path.join(root, n) for n in os.listdir(root) if n.endswith(".md")),
            key=os.path.getmtime,
        )
        if not files:
            return ""
        with open(files[-1], encoding="utf-8", errors="ignore") as f:
            return f.read(4000)
    except OSError:
        return ""


def _known_people(hist: dict, shared: list[dict], mems: list[dict],
                  admins: list[str], nicks: dict[str, str]) -> set[str]:
    """判断“我”（管理员）和其他人的关系圈层。

    证据按弱到强：
    - 聊过 >= 20 条消息，或沉淀 >= 10 条记忆；
    - 本人档案/记忆里出现同学、朋友、熟人等关系词；
    - config.json 的 known_users 手动指定。
    没有证据的人不硬猜，留在陌生星环。
    """
    admin_set = set(admins)
    stats: dict[str, dict] = {}

    def bump(qq: str, text: str = "", msgs: int = 0, mems_n: int = 0) -> None:
        qq = str(qq or "")
        if not qq or qq in admin_set:
            return
        st = stats.setdefault(qq, {"msgs": 0, "mems": 0, "texts": []})
        st["msgs"] += msgs
        st["mems"] += mems_n
        if text:
            st["texts"].append(text)

    for key, msgs in (hist or {}).items():
        bump(_speaker_of(key), msgs=len(msgs or []))
    for m in (shared or []):
        bump(_speaker_of(str(m.get("conversation_key") or "")),
             (m.get("content") or ""), mems_n=1)
    for m in (mems or []):
        bump(str(m.get("speaker_id") or ""), (m.get("content") or ""), mems_n=1)

    known: set[str] = set()
    for qq, st in stats.items():
        if st["msgs"] >= KNOWN_MIN_MESSAGES or st["mems"] >= KNOWN_MIN_MEMORIES:
            known.add(qq)
        if any(kw in txt for kw in RELATION_KEYWORDS for txt in st["texts"]):
            known.add(qq)

    for qq in nicks:
        if qq in admin_set:
            continue
        txt = _persona_text(qq)
        if any(kw in txt for kw in RELATION_KEYWORDS):
            known.add(qq)

    known.update(_known_from_config())
    known.difference_update(_config_users("stranger_users"))
    known.difference_update(admin_set)
    return known


def _ring_of(qq: str, admins: list[str], known: set[str]) -> str:
    if str(qq) in set(admins):
        return "core"
    return "inner" if str(qq) in known else "outer"


RING_LABEL = {"core": "核心", "inner": "熟识", "outer": "陌生"}


def _load_brain() -> tuple[list[dict], dict[str, list[float]], dict[str, dict]]:
    """返回（记忆, 向量, 各说话人情绪）。库不存在就返回空，不报错。"""
    mems: list[dict] = []
    vecs: dict[str, list[float]] = {}
    moods: dict[str, dict] = {}
    if not os.path.exists(DB):
        return mems, vecs, moods
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        # pinned 是后加的列，老库可能还没有：先探测，避免 SELECT 整个失败
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        pin_col = "pinned" if "pinned" in cols else "0 AS pinned"
        for r in conn.execute(
                "SELECT id,speaker_id,kind,content,importance,confidence,strength,"
                "created_at,updated_at,status,source,superseded_by,access_count,"
                + pin_col +
                " FROM memories ORDER BY id DESC LIMIT 800"):
            mems.append(dict(r))
        try:
            for r in conn.execute("SELECT memory_id,vec,dim FROM embeddings"):
                import array
                a = array.array("f")
                a.frombytes(r["vec"])
                vecs[str(r["memory_id"])] = list(a)
        except Exception:
            pass
        try:
            for r in conn.execute("SELECT speaker_id,valence,arousal,affection FROM emotion_state"):
                moods[str(r["speaker_id"])] = {"v": r["valence"], "a": r["arousal"],
                                               "f": r["affection"]}
        except Exception:
            pass
        conn.close()
    except Exception:
        pass
    return mems, vecs, moods


VEC_CACHE = os.path.join(DATA, "vectors-cache.json")


def _load_shared() -> list[dict]:
    """共用记忆库（bridge.db）里的问答。

    这是 QQ 机器人每轮真实对话都写进去的那份，也是另一个端同步的「QQ 记忆库」。
    只读打开，库不在、表不在、被锁住都当没有，不影响出图。
    """
    out: list[dict] = []
    if not SHARED_DB or not os.path.exists(SHARED_DB):
        return out
    try:
        conn = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute("SELECT * FROM memories ORDER BY id DESC LIMIT 1200"):
                out.append(dict(r))
        finally:
            conn.close()
    except Exception:
        return []
    return out


def _summary_vectors(items: list[tuple[str, str]]) -> dict[str, list[float]]:
    """给文本算向量（带本地缓存）。只算一次，之后离线可用。

    缓存用 base64(float32) 存，比 JSON 数组小 5 倍。
    """
    import base64
    import hashlib
    from array import array

    try:
        import companion_brain as cb
    except Exception:
        # 独立运行（没有向量后端）时仍然可以用缓存里的旧向量，但不再新算。
        cb = None

    def pack(v: list[float]) -> str:
        return base64.b64encode(array("f", v).tobytes()).decode("ascii")

    def unpack(s):
        try:
            a = array("f")
            a.frombytes(base64.b64decode(s))
            return list(a)
        except Exception:
            return s if isinstance(s, list) else None

    cache = _read_json(VEC_CACHE, {})
    out: dict[str, list[float]] = {}
    todo: list[tuple[str, str, str]] = []
    for sid, text in items:
        if not text.strip():
            continue
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        v = cache.get(key)
        if v:
            vec = unpack(v)
            if vec:
                out[sid] = vec
                continue
        todo.append((sid, text, key))
    if todo and cb is not None:
        try:
            brain = cb.Brain(":memory:", "view")
            vecs = brain._cloud_embed([t[:400] for _, t, _ in todo])
            brain.close()
            for (sid, _t, key), v in zip(todo, vecs):
                cache[key] = pack(v)
                out[sid] = v
            with open(VEC_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except Exception as e:  # 没网/没 key 就不连边，不影响出图
            print(f"  语义边跳过：{type(e).__name__}", file=sys.stderr)
    return out


def _semantic_links(vecmap: dict[str, list[float]], threshold: float = 0.15,
                    mem_limit: int = 120) -> list[dict]:
    """余弦先减基线再比，跟召回用的是同一套口径。

    记忆一多，全量两两比是 O(n²)：1000 条要 27 秒。这里分两层：
      粗排：每 8 维取一维（128 维），明显不够格的不再算全量
      精算：粗排过线的才算 1024 维完整余弦
    摘要参与的全部比；记忆↔记忆只比最近的 mem_limit 条，避免边爆炸。
    """
    try:
        from companion_brain import _cos
    except Exception:
        def _cos(a, b):  # 独立运行：用同样的余弦口径，不依赖向量后端
            n = min(len(a), len(b))
            dot = sum(a[i] * b[i] for i in range(n))
            na = sum(x * x for x in a[:n]) ** 0.5
            nb = sum(x * x for x in b[:n]) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

    ids = list(vecmap)
    coarse = {k: v[::8] for k, v in vecmap.items()}
    mems = [i for i in ids if i.startswith("mem:")][:mem_limit]
    sums = [i for i in ids if not i.startswith("mem:")]

    pairs: list[tuple[str, str]] = []
    for a in range(len(sums)):
        for b in range(a + 1, len(sums)):
            pairs.append((sums[a], sums[b]))
        for m in mems:
            pairs.append((sums[a], m))
    for a in range(len(mems)):
        for b in range(a + 1, len(mems)):
            pairs.append((mems[a], mems[b]))

    out: list[dict] = []
    gate = threshold - 0.15   # 粗排只挡明显无关的，宁可放过不可错杀
    for a, b in pairs:
        if (_cos(coarse[a], coarse[b]) - 0.45) / 0.55 < gate:
            continue
        sim = (_cos(vecmap[a], vecmap[b]) - 0.45) / 0.55
        if sim >= threshold:
            out.append({"source": a, "target": b, "kind": "semantic", "sim": round(sim, 3)})
    return out


def build() -> dict:
    mem_cfg = _read_json(os.path.join(DATA, "memory.json"), {})
    hist = _read_json(os.path.join(DATA, "history.json"), {})
    nicks = _load_nicks()
    admins, backend = _load_core_config()
    admin_nicks = [nicks.get(qq) or "" for qq in admins]
    mems, vecs, moods = _load_brain()
    shared = _load_shared()
    known = _known_people(hist, shared, mems, admins, nicks)
    manual_known = _known_from_config()
    manual_outer = _config_users("stranger_users")

    nodes: list[dict] = []
    links: list[dict] = []
    seen: set[str] = set()

    def add(node: dict) -> None:
        if node["id"] in seen:
            return
        seen.add(node["id"])
        t = NODE_TYPES.get(node.get("type"), {})
        if not node.get("color"):
            node["color"] = t.get("color", "#888")
        node.setdefault("size", t.get("size", 3))
        nodes.append(node)

    # 1) 人格：QQ 号是凭证/主键，昵称只用于显示（从 personas/<QQ>/meta.json 读）
    for qq, nick in nicks.items():
        label = f"{nick}（{qq}）" if nick else f"QQ {qq}"
        ring = _ring_of(qq, admins, known)
        add({"id": f"persona:{qq}", "type": "persona", "label": label, "qq": qq,
             "nick": nick, "size": NODE_TYPES["persona"]["size"] + 2,
             "admin": qq in admins, "core_related": qq in admins,
             "known": qq in known, "ring": ring, "ring_label": RING_LABEL[ring],
             "override": ("inner" if qq in manual_known
                          else "outer" if qq in manual_outer else "auto"),
             "core_reason": "管理员本人" if qq in admins else ""})

    # 1.5) 记忆核心：系统 × QQ 管理员。
    #      它是图上的固定中心，不是 facts.json 的事实卡，也不参与删除/遗忘。
    admin_labels = [f"{nicks.get(qq) or 'QQ ' + qq}（{qq}）" for qq in admins]
    if len(admin_labels) == 1:
        core_label = f"系统 × {admin_labels[0]}"
    elif admin_labels:
        core_label = "系统 × QQ管理员（%d 人）" % len(admin_labels)
    else:
        core_label = "系统 × QQ管理员（未配置）"
    known_labels = [f"{nicks.get(qq) or 'QQ ' + qq}（{qq}）" for qq in sorted(known)]
    core_text = (
        "系统与 QQ 管理员的关系核心。"
        + ("管理员：" + "、".join(admin_labels) + "。" if admin_labels else "当前 config.json 没有配置管理员。")
        + "管理员本人和系统相关记忆在中心；有来往的同学/熟人围绕中心生成；"
        + "陌生人或暂时不熟的人隔开一段距离，在最外围形成星环。"
        + f"当前后端：{backend}；本地伴生和共用记忆用同一套关系判定。"
    )
    add({"id": CORE_ID, "type": "core", "label": core_label,
         "text": core_text, "admins": admins, "backend": backend,
         "admin_labels": admin_labels, "known": sorted(known),
         "known_labels": known_labels, "ring": "core", "ring_label": "核心",
         "core_related": True, "core_reason": "系统与管理员的关系",
         "core_role": "system-admin"})
    for qq in admins:
        pid = f"persona:{qq}"
        if pid in seen:
            links.append({"source": CORE_ID, "target": pid, "kind": "core"})
    # 中心只牵“熟识的人”，陌生人不连核心，保持外圈距离。
    for qq in sorted(known):
        pid = f"persona:{qq}"
        if pid in seen:
            links.append({"source": CORE_ID, "target": pid, "kind": "relation"})

    # 2) 会话 + 日期 + 消息
    conv_keys = sorted(set(mem_cfg) | set(hist))
    for key in conv_keys:
        days = (mem_cfg.get(key) or {}).get("days") or {}
        msgs = hist.get(key) or []
        c_reason = _core_reason("", "", key, admins, admin_nicks)
        c_core = bool(c_reason)
        c_ring = "core" if c_core else _ring_of(_speaker_of(key), admins, known)
        add({"id": f"conv:{key}", "type": "conv", "label": _conv_label(key, nicks),
             "raw": key, "days": len(days), "msgs": len(msgs),
             "size": NODE_TYPES["conv"]["size"] + min(6, len(msgs) / 4),
             "core_related": c_core, "core_reason": c_reason,
             "ring": c_ring, "ring_label": RING_LABEL[c_ring]})
        if c_core:
            links.append({"source": CORE_ID, "target": f"conv:{key}", "kind": "core"})
        for day in sorted(days):
            add({"id": f"day:{day}", "type": "day", "label": day[5:], "date": day,
                 "core_related": c_core, "core_reason": c_reason,
                 "ring": c_ring, "ring_label": RING_LABEL[c_ring]})
            links.append({"source": f"conv:{key}", "target": f"day:{day}", "kind": "active"})
            # 这一天的摘要本身就是一条真实内容，单独成球
            summary = (days.get(day) or "").strip()
            if summary:
                sid = f"sum:{key}:{day}"
                add({"id": sid, "type": "summary", "label": summary[:26],
                     "text": summary, "date": day, "conv": key,
                     "core_related": c_core, "core_reason": c_reason,
                     "ring": c_ring, "ring_label": RING_LABEL[c_ring],
                     "size": NODE_TYPES["summary"]["size"] + min(5, len(summary) / 120)})
                links.append({"source": f"conv:{key}", "target": sid, "kind": "about"})
                links.append({"source": f"day:{day}", "target": sid, "kind": "on"})
        for i, m in enumerate(msgs):
            text = (m.get("content") or "").strip()
            # 用内容哈希做 id：历史被裁剪时 id 也不会错位
            import hashlib
            h = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
            mid = f"msg:{key}:{h}"
            # role 缺失只说明“没记录是谁发的”，消息本身仍然有效，按对方的话展示
            _role = str(m.get("role") or "user")
            if _role not in ("user", "assistant"):
                _role = "user"
            add({"id": mid, "type": "msg", "label": text[:24] or "…",
                 "role": _role, "text": text[:400], "conv": key,
                 "core_related": c_core, "core_reason": c_reason,
                 "ring": c_ring, "ring_label": RING_LABEL[c_ring]})
            links.append({"source": mid, "target": f"conv:{key}", "kind": "in"})

    # 3) 人格 -> 会话
    for key in conv_keys:
        if key.startswith("private:"):
            qq = key.split(":", 1)[1]
            if f"persona:{qq}" in seen:
                links.append({"source": f"persona:{qq}", "target": f"conv:{key}", "kind": "owns"})

    # 4) 记忆：两份来源
    #    共用记忆（bridge.db）—— QQ 每轮真实对话都写，数量最多，页面里的主体
    #    本地伴生（brain.db）—— 伴生大脑提炼过的事实，带重要度/强度/向量
    #    节点 id 用 mem:<库>:<id>：s=共用记忆，b=本地伴生；写回接口按这个前缀路由。
    now_ts = time.time()
    sup_pairs: list[tuple[int, int, str]] = []
    conv_set = set(conv_keys)

    for m in shared:
        body = (m.get("content") or "").strip()
        if not body:
            continue
        conv_key = str(m.get("conversation_key") or "")
        ts_up = _num(m.get("updated_at"), 0.0) / 1000.0     # bridge 里是毫秒
        ts_new = _num(m.get("created_at"), 0.0) / 1000.0
        imp = max(0.0, min(1.0, _num(m.get("importance"), 0.6)))
        status = str(m.get("status") or "active")
        pinned = bool(m.get("pinned"))
        strength = 1.0 if pinned else _strength_now(
            {"strength": 1.0, "importance": imp,
             "updated_at": ts_up or ts_new or now_ts}, now_ts)
        if not pinned and status == "faded":
            strength *= 0.15          # 忘掉的球压暗，但仍然留在图里
        elif not pinned and status == "superseded":
            strength *= 0.55
        subject = (m.get("subject") or "").strip()
        first = body.split("\n", 1)[0].strip()
        if first.startswith("用户："):
            first = first[3:].strip()
        label = subject or first or "…"
        sid = f"mem:s:{m['id']}"
        spk = _speaker_of(conv_key)
        c_reason = _core_reason(body, spk, conv_key, admins, admin_nicks)
        c_core = bool(c_reason)
        m_ring = "core" if c_core else _ring_of(spk, admins, known)
        add({"id": sid, "type": "memory", "store": "s", "label": label[:28],
             "text": body, "qa": "\n" in body, "fact": subject,
             "conv": conv_key,
             "importance": imp, "confidence": m.get("confidence"),
             "speaker": spk, "kind": "qa", "source": "共用记忆",
             "origin": "qq", "status": status, "pinned": pinned,
             "core_related": c_core, "core_reason": c_reason,
             "known": spk in known, "ring": m_ring, "ring_label": RING_LABEL[m_ring],
             "superseded_by": m.get("superseded_by"),
             "created": ts_new or None, "updated": ts_up or ts_new or None,
             "access": None, "strength": round(strength, 3),
             "color": PIN_COLOR if pinned else None,
             "size": NODE_TYPES["memory"]["size"] + 4 * imp + (1.5 if pinned else 0)})
        if conv_key in conv_set:
            links.append({"source": sid, "target": f"conv:{conv_key}", "kind": "about"})
        elif spk and f"persona:{spk}" in seen:
            # 这个人的会话没进 history/memory（比如只聊过几句），挂到人格球上
            links.append({"source": sid, "target": f"persona:{spk}", "kind": "about"})
        nb = m.get("superseded_by")
        if nb:
            try:
                sup_pairs.append((m["id"], int(nb), "s"))
            except (TypeError, ValueError):
                pass

    for m in mems:
        mid = f"mem:b:{m['id']}"
        _pinned = bool(m.get("pinned"))
        strength = 1.0 if _pinned else _strength_now(m, now_ts)
        _body = (m["content"] or "").strip()
        # 成对记忆是「问：…\n答：…\n记：…」，球上优先显示提炼出的事实，详情里看完整问答
        _lines = _body.split("\n")
        _fact = next((ln[2:].strip() for ln in _lines if ln.startswith("记：")), "")
        _first = _fact or (_lines[0] if _lines else "")
        _imp = _num(m.get("importance"), 0.0)
        _spk = str(m["speaker_id"])
        _conv = next((key for key in conv_keys
                      if key.endswith(":" + _spk) or key == f"private:{_spk}"), "")
        _c_reason = _core_reason(_body, _spk, _conv, admins, admin_nicks)
        _c_core = bool(_c_reason)
        _ring = "core" if _c_core else _ring_of(_spk, admins, known)
        add({"id": mid, "type": "memory", "store": "b", "label": _first[:28],
             "text": _body, "qa": len(_lines) > 1, "fact": _fact,
             "importance": _imp,
             "confidence": m["confidence"], "speaker": m["speaker_id"],
             "kind": m.get("kind") or "fact", "source": "本地伴生",
             "origin": m.get("source") or "",
             "core_related": _c_core, "core_reason": _c_reason,
             "known": _spk in known, "ring": _ring, "ring_label": RING_LABEL[_ring],
             "status": m.get("status") or "active", "pinned": _pinned,
             "superseded_by": m.get("superseded_by"),
             "created": m.get("created_at"), "updated": m["updated_at"],
             "access": m["access_count"], "strength": round(strength, 3),
             "color": PIN_COLOR if _pinned else None,
             "size": NODE_TYPES["memory"]["size"] + 4 * _imp + (1.5 if _pinned else 0)})
        spk = str(m["speaker_id"])
        for key in conv_keys:
            if key.endswith(":" + spk) or key == f"private:{spk}":
                links.append({"source": mid, "target": f"conv:{key}", "kind": "about"})
                break
        # 被新记忆覆盖的，先记下来，等所有节点建完再连线
        nb = m.get("superseded_by")
        if nb:
            try:
                sup_pairs.append((m["id"], int(nb), "b"))
            except (TypeError, ValueError):
                pass

    for old, new, store in sup_pairs:
        old_id, new_id = f"mem:{store}:{old}", f"mem:{store}:{new}"
        if old_id in seen and new_id in seen:
            links.append({"source": old_id, "target": new_id, "kind": "superseded"})

    # 5) 语义边：日摘要与记忆之间，意思相近就连一条
    sem: list[dict] = []
    vecmap: dict[str, list[float]] = {}
    try:
        import settings_store
        sem_on = settings_store.on("viewer.semantic_edges", True)
    except Exception:
        sem_on = True
    if sem_on:
        pairs = [(n["id"], n.get("text") or "") for n in nodes if n["type"] == "summary"]
        if pairs:
            try:
                vecmap.update(_summary_vectors(pairs))
            except Exception as e:  # 向量后端不可用就只画结构边
                print(f"  语义边跳过：{type(e).__name__}", file=sys.stderr)
        for mid, v in vecs.items():
            if f"mem:b:{mid}" in seen:
                vecmap[f"mem:b:{mid}"] = v
        try:
            sem = _semantic_links(vecmap)
        except Exception:
            sem = []
    links.extend(sem)
    meta_extra = {"semantic_edges": len(sem), "vectors": len(vecmap)}

    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {},
        "sources": {
            "会话": len(conv_keys), "消息": sum(len(hist.get(k) or []) for k in conv_keys),
            "有摘要的天": sum(len((mem_cfg.get(k) or {}).get("days") or {}) for k in conv_keys),
            "人格": len(nicks), "记忆": len(shared) + len(mems),
            "共用记忆": len(shared), "本地记忆": len(mems), "带向量": len(vecs),
        },
        "moods": moods,
        "types": {k: {"color": v["color"], "label": v["label"]} for k, v in NODE_TYPES.items()},
        "core": {
            "id": CORE_ID, "label": core_label, "admins": admins,
            "admin_labels": admin_labels, "backend": backend,
            "known": sorted(known), "known_labels": known_labels,
            "ring": "core",
        },
    }
    for n in nodes:
        meta["counts"][n["type"]] = meta["counts"].get(n["type"], 0) + 1
    core_mems = sum(1 for n in nodes
                    if n.get("type") == "memory" and n.get("core_related"))
    core_nodes = sum(1 for n in nodes if n.get("core_related"))
    known_mems = sum(1 for n in nodes
                     if n.get("type") == "memory" and n.get("ring") == "inner")
    stranger_mems = sum(1 for n in nodes
                        if n.get("type") == "memory" and n.get("ring") == "outer")
    meta["core"]["memories"] = core_mems
    meta["core"]["nodes"] = core_nodes
    meta["sources"]["核心相关"] = core_mems
    meta["sources"]["熟识记忆"] = known_mems
    meta["sources"]["陌生记忆"] = stranger_mems
    meta["sources"]["外围记忆"] = sum(
        1 for n in nodes if n.get("type") == "memory" and not n.get("core_related"))
    meta["rings"] = {
        r: sum(1 for n in nodes if n.get("ring") == r)
        for r in ("core", "inner", "outer")
    }
    # 统计口径跟节点对齐（内容完全相同的消息会合并成一个球）
    meta["sources"]["消息"] = meta["counts"].get("msg", 0)
    meta["sources"]["摘要天"] = meta["counts"].get("summary", 0)
    meta["sources"]["语义边"] = meta_extra["semantic_edges"]
    meta["sources"]["向量"] = meta_extra["vectors"]
    meta["sources"]["永久"] = sum(1 for n in nodes if n.get("pinned"))

    # 「只看某个人」用的名单：QQ 号 + 昵称 + 各类数量
    people: dict[str, dict] = {}

    def _person(qq: Any, nick: str = "") -> dict | None:
        qq = str(qq or "").strip()
        if not qq or qq == "0":
            return None
        p = people.get(qq)
        if p is None:
            name = nick or nicks.get(qq, "")
            p = people[qq] = {"qq": qq, "nick": nicks.get(qq, ""),
                              "label": name or f"QQ {qq}",
                              "known": qq in known,
                              "ring": _ring_of(qq, admins, known),
                              "memories": 0, "msgs": 0, "convs": 0}
        return p

    for n in nodes:
        if n["type"] == "persona":
            _person(n.get("qq"), n.get("nick") or "")
        elif n["type"] == "conv":
            p = _person(_speaker_of(n.get("raw") or ""))
            if p:
                p["convs"] += 1
                p["msgs"] += int(n.get("msgs") or 0)
        elif n["type"] == "memory":
            p = _person(n.get("speaker"))
            if p:
                p["memories"] += 1
    meta["people"] = sorted(people.values(), key=lambda p: (-p["memories"], p["qq"]))
    return {"nodes": nodes, "links": links, "meta": meta}


def _link_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def scope_graph(g: dict, qq: str) -> dict:
    """把整张图裁成某个 QQ 能看的部分。

    只给：他自己的人格球、和他的私聊、这个私聊里的消息/摘要/记忆，以及相关日期。
    群聊（``group:...``）一律不进——群里还有别人的话，不能因为账号是他的就整段给出去。
    本地伴生记忆按 ``speaker`` 归他；共用记忆按 ``conv == private:<qq>`` 归他。
    """
    qq = str(qq or "").strip()
    nodes = list(g.get("nodes") or [])
    links = list(g.get("links") or [])
    by_id = {n["id"]: n for n in nodes}
    conv_key = f"private:{qq}"
    conv_id = f"conv:{conv_key}"
    persona_id = f"persona:{qq}"
    core_meta = (g.get("meta") or {}).get("core") or {}
    core_id = str(core_meta.get("id") or CORE_ID)
    admins = [str(x) for x in (core_meta.get("admins") or [])]
    allowed: set[str] = set()
    if persona_id in by_id:
        allowed.add(persona_id)
    if conv_id in by_id:
        allowed.add(conv_id)
    # 核心只给管理员看；普通账号即使群会话里连到了核心，也不能拿走管理员信息
    if qq in admins and core_id in by_id:
        allowed.add(core_id)

    def one_hop(anchor: str) -> None:
        for l in links:
            s, t = _link_id(l.get("source")), _link_id(l.get("target"))
            if s == anchor:
                allowed.add(t)
            elif t == anchor:
                allowed.add(s)

    if conv_id in by_id:
        # 只从「这个私聊」走一跳，不从人格走：人格下还挂着群聊，不能整片带出来
        one_hop(conv_id)

    # 私聊里的共用记忆（没连上会话的也补上）
    for n in nodes:
        if (n.get("type") == "memory" and n.get("store") == "s"
                and str(n.get("conv") or "") == conv_key):
            allowed.add(n["id"])

    # 过滤记忆：共用只认这个私聊；本地只认说话人是他
    for nid in list(allowed):
        n = by_id.get(nid) or {}
        if n.get("type") != "memory":
            continue
        if n.get("store") == "b":
            if str(n.get("speaker") or "") != qq:
                allowed.discard(nid)
        else:
            c = str(n.get("conv") or "")
            # 老 graph.json 没 conv 字段：只要它是从「这个私聊」走到的就留下
            if c and c != conv_key:
                allowed.discard(nid)

    # 他的本地伴生记忆，即使没连到会话也给他
    for n in nodes:
        if (n.get("type") == "memory" and n.get("store") == "b"
                and str(n.get("speaker") or "") == qq):
            allowed.add(n["id"])

    # 日期球：只留和已允许摘要相连的（日期球本身还会连到别的会话，不能再从它扩散）
    for l in links:
        if l.get("kind") != "on":
            continue
        s, t = _link_id(l.get("source")), _link_id(l.get("target"))
        if s in allowed and (by_id.get(t) or {}).get("type") == "day":
            allowed.add(t)
        elif t in allowed and (by_id.get(s) or {}).get("type") == "day":
            allowed.add(s)

    if qq not in admins:
        allowed.discard(core_id)

    out_nodes = [n for n in nodes if n["id"] in allowed]
    out_links = [l for l in links
                 if _link_id(l.get("source")) in allowed
                 and _link_id(l.get("target")) in allowed]
    counts: dict[str, int] = {}
    for n in out_nodes:
        k = n.get("type") or "?"
        counts[k] = counts.get(k, 0) + 1
    shared = sum(1 for n in out_nodes if n.get("type") == "memory" and n.get("store") == "s")
    local = sum(1 for n in out_nodes if n.get("type") == "memory" and n.get("store") == "b")
    pinned = sum(1 for n in out_nodes if n.get("pinned"))
    sem = sum(1 for l in out_links if l.get("kind") == "semantic")
    meta = dict(g.get("meta") or {})
    meta["counts"] = counts
    meta["sources"] = {
        "会话": counts.get("conv", 0),
        "消息": counts.get("msg", 0),
        "摘要天": counts.get("summary", 0),
        "人格": counts.get("persona", 0),
        "记忆": counts.get("memory", 0),
        "共用记忆": shared,
        "本地记忆": local,
        "永久": pinned,
        "核心相关": sum(1 for n in out_nodes
                        if n.get("type") == "memory" and n.get("core_related")),
        "熟识记忆": sum(1 for n in out_nodes
                        if n.get("type") == "memory" and n.get("ring") == "inner"),
        "陌生记忆": sum(1 for n in out_nodes
                        if n.get("type") == "memory" and n.get("ring") == "outer"),
        "外围记忆": sum(1 for n in out_nodes
                        if n.get("type") == "memory" and not n.get("core_related")),
        "语义边": sem,
    }
    meta["rings"] = {
        r: sum(1 for n in out_nodes if n.get("ring") == r)
        for r in ("core", "inner", "outer")
    }
    if qq not in admins:
        meta["core"] = {}
    persona = by_id.get(persona_id) or {}
    nick = str(persona.get("nick") or "")
    meta["people"] = [{"qq": qq, "nick": nick,
                       "label": nick or str(persona.get("label") or qq),
                       "memories": counts.get("memory", 0)}]
    meta["scope"] = {"qq": qq, "label": nick or qq}
    return {"nodes": out_nodes, "links": out_links, "meta": meta}


_REDACT_LABELS = {
    "core": "系统核心",
    "persona": "别人的人格",
    "conv": "别人的会话",
    "msg": "别人的消息",
    "summary": "别人的摘要",
    "memory": "别人的记忆",
}


def overview_graph(g: dict, qq: str) -> dict:
    """整张图都能看，但别人的具体内容抹掉。

    普通账号看到的是完整的星云（所有球、人数、分类统计），点开别人的球只有
    「这是别人的数据」；``mine`` 标记前端哪些球能多选/编辑。写接口仍然逐条校验
    归属，导出也只导出自己的。
    """
    own = scope_graph(g, qq)
    own_ids = {n["id"] for n in own.get("nodes") or []}
    nodes = []
    for raw in g.get("nodes") or []:
        n = dict(raw)
        if n.get("id") in own_ids:
            n["mine"] = True
        else:
            n["mine"] = False
            if n.get("type") in _REDACT_LABELS:
                n["redacted"] = True
                # 正文、提炼出的事实、原始字段都不能留：它们在别的视图里就是内容
                for key in ("text", "fact", "body", "content", "subject",
                            "admins", "core_reason", "known_labels"):
                    n.pop(key, None)
                n["label"] = _REDACT_LABELS[n["type"]]
        nodes.append(n)
    meta = dict(g.get("meta") or {})
    meta["redacted"] = True
    # 普通账号/访客不需要管理员名单；核心球保留，但 meta.core 里的账号信息不返回。
    meta["core"] = {}
    # 「看谁」下拉只列自己：别人的昵称也属于个人信息，访客一个都不给。
    meta["people"] = [p for p in (meta.get("people") or [])
                      if qq and str(p.get("qq")) == str(qq)]
    meta["scope"] = dict((own.get("meta") or {}).get("scope") or {"qq": qq})
    return {"nodes": nodes, "links": list(g.get("links") or []), "meta": meta}


def guest_graph(g: dict) -> dict:
    """未登录视图：所有球体都能看，但所有人的具体内容都抹掉。

    和普通账号的 overview_graph 一样保留完整拓扑；区别是没有 ``mine``，
    所以前端不会出现任何可编辑入口。
    """
    out = overview_graph(g, "")
    meta = dict(out.get("meta") or {})
    meta["guest"] = True
    meta["redacted"] = True
    meta["scope"] = {"qq": "", "label": "未登录"}
    out["meta"] = meta
    return out


def write_graph(g: dict, out: str | None = None) -> str:
    """先写临时文件再原子替换：页面正好在重建时刷新也不会读到写了一半的 JSON。"""
    os.makedirs(WEB, exist_ok=True)
    out = out or os.path.join(WEB, "graph.json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False)
    os.replace(tmp, out)
    # 这张图含所有人的记忆正文，本机其他用户不该直接读；写完后收紧 ACL
    try:
        import accounts
        accounts._harden(out)
    except Exception:
        pass
    return out


def main() -> None:
    g = build()
    print(json.dumps(g["meta"], ensure_ascii=False, indent=2))
    if "--stats" in sys.argv:
        return
    out = write_graph(g)
    print(f"\n已写入 {out}  ({round(os.path.getsize(out)/1024,1)} KB)")


if __name__ == "__main__":
    main()