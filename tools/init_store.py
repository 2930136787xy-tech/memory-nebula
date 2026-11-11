# -*- coding: utf-8 -*-
"""从零创建一个属于你自己的「记忆星云」记忆库——不依赖任何聊天软件，也不带任何演示数据。

    python tools/init_store.py D:\\my-memory --persona 10001=我自己 --persona 20002=朋友

它会做三件事：
    1. 把框架代码（viewer_server.py、web/ 等）复制到目标目录，成为一个独立可运行的项目；
    2. 建两个空库：bridge.db（共用记忆）、brain.db（本地记忆 + 向量 + 情绪）；
    3. 按 --persona 建人格，生成 data/ 与 config.json。

跑完直接：
    cd D:\\my-memory
    python viewer_server.py 8765
再打开 http://127.0.0.1:8765/，然后用 tools/import_memories.py 把任何来源的记忆灌进来。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SHARED_SCHEMA = """CREATE TABLE memories (
    id INTEGER PRIMARY KEY, content TEXT, conversation_key TEXT,
    created_at INTEGER, updated_at INTEGER, subject TEXT, status TEXT,
    confidence REAL, importance REAL, superseded_by INTEGER, pinned INTEGER)"""

BRAIN_SCHEMA = [
    """CREATE TABLE memories (
        id INTEGER PRIMARY KEY, speaker_id TEXT, kind TEXT, content TEXT,
        importance REAL, confidence REAL, strength REAL, created_at INTEGER,
        updated_at INTEGER, status TEXT, source TEXT, superseded_by INTEGER,
        access_count INTEGER, pinned INTEGER)""",
    "CREATE TABLE embeddings (memory_id INTEGER, vec BLOB, dim INTEGER)",
    "CREATE TABLE emotion_state (speaker_id TEXT, valence REAL, arousal REAL, affection REAL)",
]

# 复制到新库的框架文件（运行必需的，不含 demo/docs/tests）
COPY_FILES = [
    "viewer_server.py", "memory_view.py", "accounts.py", "core_api.py", "core_life.py",
    "settings_store.py", "memory_backup.py", "companion_brain.py", "shadow_recall.py", "data_backup.py",
    "requirements.txt", "LICENSE", "config.example.json", ".gitignore",
    "start-viewer.bat", "start-viewer-readonly.bat", "start-viewer-accounts.bat", "account.bat",
]
COPY_DIRS = ["web", "tools"]


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def existing_memories(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return int(con.execute("select count(*) from memories").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0


def parse_personas(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if "=" in text:
            ident, nick = text.split("=", 1)
        else:
            ident, nick = text, text
        ident = ident.strip()
        if not ident:
            raise SystemExit(f"--persona 写法应为 id=昵称，收到：{text}")
        out[ident] = nick.strip() or ident
    return out


def copy_framework(target: str) -> list[str]:
    copied = []
    for name in COPY_FILES:
        src = os.path.join(ROOT, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(target, name)
        if os.path.exists(dst) and os.path.samefile(src, dst):
            continue
        shutil.copy2(src, dst)
        copied.append(name)
    for name in COPY_DIRS:
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(target, name)
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            rel = os.path.relpath(base, src)
            out_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(out_dir, exist_ok=True)
            for f in files:
                if f == "graph.json":
                    continue
                s = os.path.join(base, f)
                d = os.path.join(out_dir, f)
                if os.path.exists(d) and os.path.samefile(s, d):
                    continue
                shutil.copy2(s, d)
        copied.append(name + "/")
    return copied


def main() -> None:
    ap = argparse.ArgumentParser(description="创建一份全新的记忆库（含框架代码）")
    ap.add_argument("target", help="记忆库目录（不存在会创建）")
    ap.add_argument("--persona", action="append", metavar="ID=昵称",
                    help="建一个人格，可重复；比如 --persona 10001=我自己")
    ap.add_argument("--admin", action="append", metavar="ID", help="设为核心关系（admins），可重复")
    ap.add_argument("--force", action="store_true", help="目标已有记忆时也重建（会清空原有记忆）")
    ap.add_argument("--no-copy", action="store_true", help="只建数据，不复制框架代码")
    ap.add_argument("--no-graph", action="store_true", help="不预生成 graph.json")
    args = ap.parse_args()

    target = os.path.abspath(os.path.expanduser(args.target))
    shared_db = os.path.join(target, "bridge.db")
    brain_db = os.path.join(target, "brain.db")

    if os.path.exists(target) and not os.path.isdir(target):
        raise SystemExit(f"目标不是目录：{target}")

    have = existing_memories(shared_db) + existing_memories(brain_db)
    if have and not args.force:
        raise SystemExit(f"{target} 里已经有 {have} 条记忆，为避免误删已停止。\n"
                         f"确定要清空重建，请加 --force。")

    os.makedirs(target, exist_ok=True)
    copied = [] if args.no_copy else copy_framework(target)

    if not os.path.exists(os.path.join(target, "viewer_server.py")):
        raise SystemExit("目标目录里没有 viewer_server.py；去掉 --no-copy 重跑，或手动把框架复制进去。")

    personas = parse_personas(args.persona)
    admins = [str(x).strip() for x in (args.admin or []) if str(x).strip()]
    for ident in admins:
        personas.setdefault(ident, ident)

    os.makedirs(os.path.join(target, "data", "core"), exist_ok=True)
    os.makedirs(os.path.join(target, "personas"), exist_ok=True)

    for ident, nick in personas.items():
        write_json(os.path.join(target, "personas", ident, "meta.json"),
                   {"id": ident, "qq": ident, "nick": nick, "created": int(time.time())})

    config_path = os.path.join(target, "config.json")
    if not os.path.exists(config_path) or args.force:
        write_json(config_path, {
            "admins": admins,
            "backend": "local",
            "known_users": [],
            "stranger_users": [],
            "note": "这是你自己的记忆库；关系分层按需要改。",
        })

    write_json(os.path.join(target, "data", "memory.json"), {})
    write_json(os.path.join(target, "data", "history.json"), {})

    for db_path, schema in ((shared_db, [SHARED_SCHEMA]), (brain_db, BRAIN_SCHEMA)):
        for suffix in ("", "-wal", "-shm"):
            stale = db_path + suffix
            if os.path.exists(stale):
                os.remove(stale)
        con = sqlite3.connect(db_path)
        try:
            for ddl in schema:
                con.execute(ddl)
            con.commit()
        finally:
            con.close()

    print(f"记忆库已创建：{target}")
    if copied:
        print(f"  复制框架：{'、'.join(copied)}")
    print(f"  人格 {len(personas)} 个" + (f"（{', '.join(personas)}）" if personas else "（还没有，加 --persona 再建）"))
    print("  bridge.db / brain.db 为空库，data/ 已初始化")

    if not args.no_graph:
        sys.path.insert(0, target)
        os.environ["SHARED_DB"] = shared_db
        os.environ["VIEW_DB"] = brain_db
        os.environ["NEBULA_DATA"] = os.path.join(target, "data")
        try:
            import memory_view as mv
            graph = mv.build()
            mv.write_graph(graph, os.path.join(target, "web", "graph.json"))
            print(f"  空星云图已生成（节点 {len(graph.get('nodes') or [])}）")
        except Exception as error:
            print(f"  跳过预生成 graph.json（{error}）")

    print("")
    print("下一步：")
    print(f"  cd {target}")
    print("  python viewer_server.py 8765    然后打开 http://127.0.0.1:8765/")
    print("  python tools/import_memories.py . --file 你的记忆.jsonl --conversation app:备注")


if __name__ == "__main__":
    main()