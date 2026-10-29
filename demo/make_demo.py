# -*- coding: utf-8 -*-
"""生成一套可公开演示的虚构数据，然后重建 web/graph.json。

仓库里不放任何真实聊天记录。这个脚本用固定随机种子造出四个虚构人物、
两份记忆库和若干天的摘要，跑完就能直接打开页面看效果：

    python demo/make_demo.py
    python viewer_server.py 8765

生成物都在 .gitignore 里（data/、personas/、*.db、config.json），不会进仓库。
想回到自己的真实数据：删掉这些生成物，把 config.json 换成自己的即可。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from array import array

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")
PERSONAS = os.path.join(ROOT, "personas")
BRAIN_DB = os.path.join(ROOT, "brain.db")
SHARED_DB = os.path.join(ROOT, "bridge.db")
VEC_CACHE = os.path.join(DATA, "vectors-cache.json")

DIM = 24

PEOPLE = [
    {"qq": "10001", "nick": "阿岚", "admin": True, "ring": "core"},
    {"qq": "10002", "nick": "小满", "admin": False, "ring": "inner"},
    {"qq": "10003", "nick": "老周", "admin": False, "ring": "inner"},
    {"qq": "10004", "nick": "阿七", "admin": False, "ring": "outer"},
]

TOPICS = [
    ("饮食", [
        "周五晚上去吃了巷口的牛肉面",
        "在便利店买了热美式",
        "照着视频学会了番茄牛腩",
        "夜宵吃了烤冷面",
    ]),
    ("运动", [
        "约了周六早上去打羽毛球",
        "夜跑五公里，配速比上周快",
        "游泳课换到了周三晚上",
        "开始练靠墙静蹲",
    ]),
    ("工作", [
        "项目这周要交初版",
        "开会开到晚上八点",
        "换了一个矮轴键盘",
        "把周报模板改短了",
    ]),
    ("学习", [
        "在准备资格证考试",
        "背了五十个单词",
        "看完一本讲信息设计的书",
        "把笔记搬进了新软件",
    ]),
    ("情绪", [
        "这两天有点低落",
        "被夸了，开心了很久",
        "有点焦虑，晚上睡不着",
        "今天状态意外地好",
    ]),
    ("宠物", [
        "猫又把杯子推下桌",
        "给狗换了新的粮",
        "带猫去打疫苗",
        "狗学会了握手",
    ]),
    ("旅行", [
        "计划下个月去海边",
        "在整理上次旅行的照片",
        "订好了往返的车票",
        "想去山里住两天",
    ]),
    ("音乐", [
        "循环一首很多年前的老歌",
        "买了一张现场演出的票",
        "在学一首简单的钢琴曲",
        "耳机坏了一只",
    ]),
    ("代码", [
        "在查一个内存泄漏",
        "把构建脚本改快了一倍",
        "给页面补了键盘快捷键",
        "重写了一版筛选逻辑",
    ]),
    ("游戏", [
        "新版本上线，打了一晚上",
        "和朋友联机到半夜",
        "卡在一个 boss 三天了",
        "把存档备份了一份",
    ]),
]

REPLIES = [
    "收到，我记下了。",
    "嗯，这个可以留着以后提醒你。",
    "听起来不错，需要的时候我再提。",
    "好，我把这件事放进记忆里。",
    "明白，下次聊到相关的事我会想起来。",
    "记好了。",
]


def topic_vector(idx: int, jitter: float = 0.05) -> list[float]:
    """同主题向量高度相似、不同主题接近正交，让演示里能看到语义边。"""
    rnd = random.Random(1000 + idx)
    v = [rnd.uniform(-jitter, jitter) for _ in range(DIM)]
    v[idx % DIM] += 1.0
    v[(idx + 7) % DIM] += 0.18
    v[(idx + 13) % DIM] += 0.08
    return v


def pack(v: list[float]) -> str:
    return base64.b64encode(array("f", v).tobytes()).decode("ascii")


def raw(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def reset(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            os.remove(p)


def make_brain() -> tuple[list[tuple], list[tuple], dict]:
    """返回 (memories 行, embeddings 行, 情绪)。"""
    rnd = random.Random(7)
    now = int(time.time())
    rows: list[tuple] = []
    vecs: list[tuple] = []
    moods: dict[str, dict] = {}
    n = 0
    for topic_i, (topic, texts) in enumerate(TOPICS):
        for text in texts[:2]:
            n += 1
            qq = rnd.choice(["10002", "10003", "10004"])
            imp = round(rnd.uniform(0.15, 0.95), 2)
            pinned = 1 if (qq == "10002" and n % 9 == 0) else 0
            created = now - rnd.randint(1, 12) * 86400
            body = f"问：{text}？\n答：{rnd.choice(REPLIES)}\n记：{text}"
            rows.append((n, qq, rnd.choice(["fact", "preference", "event"]), body,
                         imp, round(rnd.uniform(0.6, 0.95), 2),
                         1.0 if pinned else round(rnd.uniform(0.35, 1.0), 2),
                         created, created + rnd.randint(0, 3600),
                         "active", "demo", None, rnd.randint(0, 8), pinned))
            vecs.append((n, raw(topic_vector(topic_i)), DIM))
    for qq, base_v, base_a in (("10001", 62, 41), ("10002", 71, 35),
                               ("10003", 55, 58), ("10004", 48, 63)):
        moods[qq] = {"v": base_v, "a": base_a, "f": rnd.randint(30, 80)}
    return rows, vecs, moods


def make_shared() -> tuple[list[tuple], dict]:
    """共用记忆：QQ 每轮对话写进去的那份。"""
    rnd = random.Random(11)
    now = int(time.time() * 1000)
    rows: list[tuple] = []
    summaries: dict[str, dict] = {}
    n = 0
    convs = [
        ("private:10001", "10001", 6),
        ("private:10002", "10002", 8),
        ("private:10003", "10003", 5),
        ("group:555001:10003", "10003", 4),
        ("private:10004", "10004", 3),
    ]
    for key, qq, days in convs:
        summaries[key] = {"days": {}}
        for d in range(days):
            topic, texts = TOPICS[(d + len(key)) % len(TOPICS)]
            date = time.strftime("%Y-%m-%d", time.localtime(now / 1000 - (days - d) * 86400))
            fact = texts[d % len(texts)]
            summaries[key]["days"][date] = f"{topic}：{fact}。"
            for k in range(2):
                n += 1
                body = f"用户：{fact}\n机器人：{rnd.choice(REPLIES)}"
                status = "active"
                if n % 17 == 0 and qq == "10004":
                    status = "faded"
                pinned = 1 if (n % 23 == 0) else 0
                rows.append((
                    n, body, key, now - (days - d) * 86400 * 1000 - k * 60000,
                    now - (days - d) * 86400 * 1000 + k * 1000,
                    fact, status, round(rnd.uniform(0.4, 0.95), 2),
                    round(rnd.uniform(0.3, 1.0), 2), None, pinned,
                ))
    return rows, summaries


def make_history(summaries: dict) -> dict:
    rnd = random.Random(23)
    hist: dict[str, list] = {}
    for key, info in summaries.items():
        msgs = []
        for date in sorted(info["days"]):
            for _ in range(2):
                topic_i = rnd.randrange(len(TOPICS))
                topic, texts = TOPICS[topic_i]
                msgs.append({"role": "user", "content": rnd.choice(texts)})
                msgs.append({"role": "assistant", "content": rnd.choice(REPLIES)})
        hist[key] = msgs
    return hist


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(PERSONAS, exist_ok=True)
    os.makedirs(os.path.join(DATA, "core"), exist_ok=True)

    for p in PEOPLE:
        write_json(os.path.join(PERSONAS, p["qq"], "meta.json"),
                   {"qq": p["qq"], "nick": p["nick"], "created": int(time.time())})

    write_json(os.path.join(ROOT, "config.json"), {
        "admins": ["10001"],
        "backend": "local",
        "known_users": ["10002", "10003"],
        "stranger_users": ["10004"],
        "note": "演示配置；真实部署时按自己的账号改。",
    })

    shared_rows, summaries = make_shared()
    write_json(os.path.join(DATA, "memory.json"), summaries)
    write_json(os.path.join(DATA, "history.json"), make_history(summaries))

    cpu = time.time()
    reset(SHARED_DB)
    conn = sqlite3.connect(SHARED_DB)
    conn.execute("""CREATE TABLE memories (
        id INTEGER PRIMARY KEY, content TEXT, conversation_key TEXT,
        created_at INTEGER, updated_at INTEGER, subject TEXT, status TEXT,
        confidence REAL, importance REAL, superseded_by INTEGER, pinned INTEGER)""")
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", shared_rows)
    conn.commit()
    conn.close()

    reset(BRAIN_DB)
    brain_rows, vecs, moods = make_brain()
    conn = sqlite3.connect(BRAIN_DB)
    conn.execute("""CREATE TABLE memories (
        id INTEGER PRIMARY KEY, speaker_id TEXT, kind TEXT, content TEXT,
        importance REAL, confidence REAL, strength REAL, created_at INTEGER,
        updated_at INTEGER, status TEXT, source TEXT, superseded_by INTEGER,
        access_count INTEGER, pinned INTEGER)""")
    conn.execute("CREATE TABLE embeddings (memory_id INTEGER, vec BLOB, dim INTEGER)")
    conn.execute("CREATE TABLE emotion_state (speaker_id TEXT, valence REAL,"
                 " arousal REAL, affection REAL)")
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", brain_rows)
    conn.executemany("INSERT INTO embeddings VALUES (?,?,?)", vecs)
    conn.executemany("INSERT INTO emotion_state VALUES (?,?,?,?)",
                     [(qq, m["v"], m["a"], m["f"]) for qq, m in moods.items()])
    conn.commit()
    conn.close()

    # 摘要向量写进缓存，没有向量后端也能画出语义边
    cache: dict[str, str] = {}
    for i, (key, info) in enumerate(sorted(summaries.items())):
        for j, text in enumerate(info["days"].values()):
            v = topic_vector((i + j) % len(TOPICS))
            cache[hashlib.md5(text.encode("utf-8")).hexdigest()] = pack(v)
    write_json(VEC_CACHE, cache)

    import memory_view as mv
    graph = mv.build()
    out = mv.write_graph(graph)
    counts = graph["meta"]["counts"]
    print(f"演示数据已生成（{time.time() - cpu:.2f}s）：")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"  节点 {len(graph['nodes'])}  边 {len(graph['links'])}  →  {out}")
    print("下一步：python viewer_server.py 8765   然后打开 http://127.0.0.1:8765/")


if __name__ == "__main__":
    main()