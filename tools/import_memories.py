# -*- coding: utf-8 -*-
"""把**任何软件**产生的记忆导入记忆库——不绑定 QQ，也不要求对方有 conversations 表。

支持三种输入：
    1. JSONL：每行一个对象，字段宽松
       {"text": "...", "at": "2026-11-01T09:00:00", "importance": 0.7,
        "conversation": "app:myapp", "speaker": "me"}
       text / content / message 任取其一当正文；时间可以是 ISO 字符串或毫秒时间戳。
    2. 纯文本：一行一条记忆。
    3. CSV：带表头，需有 text 列（其余同名字段可选）。

    python tools/import_memories.py D:\\my-memory --file notes.jsonl --conversation app:notes
    python tools/import_memories.py D:\\my-memory --file diary.txt --speaker me --importance 0.8

重复正文默认跳过（--allow-duplicates 可关掉）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TEXT_KEYS = ("text", "content", "message", "body", "memory")


def _ms(value, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num if num > 1e11 else num * 1000)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except Exception:
            continue
    return fallback


def _text_of(obj: dict) -> str:
    for key in TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def read_items(path: str) -> list[dict]:
    lower = path.lower()
    if lower.endswith((".jsonl", ".ndjson")):
        items = []
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    items.append({"text": line})
                    continue
                if isinstance(obj, str):
                    items.append({"text": obj})
                elif isinstance(obj, dict):
                    items.append(obj)
        return items
    if lower.endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return [row for row in csv.DictReader(f)]
    if lower.endswith(".json"):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x if isinstance(x, dict) else {"text": str(x)} for x in data]
        if isinstance(data, dict):
            for key in ("items", "memories", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [{"text": str(data)}]
        return []
    with open(path, encoding="utf-8-sig") as f:
        return [{"text": line.strip()} for line in f if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="把任意来源的记忆导入记忆库")
    ap.add_argument("store", help="记忆库目录（init_store.py 建出来的那个）")
    ap.add_argument("--file", required=True, help="输入的 jsonl / json / csv / txt")
    ap.add_argument("--conversation", default="import", help="会话键，默认 import；建议写成 app:备注")
    ap.add_argument("--speaker", default="", help="说话者标识，可留空")
    ap.add_argument("--importance", type=float, default=0.6, help="默认重要度 0..1")
    ap.add_argument("--allow-duplicates", action="store_true", help="不按正文去重")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    db = os.path.join(store, "bridge.db")
    if not os.path.exists(db):
        raise SystemExit(f"{store} 里没有 bridge.db；先跑 python tools/init_store.py {store}")

    source = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.exists(source):
        raise SystemExit(f"找不到输入文件：{source}")

    raw_items = read_items(source)
    now = int(time.time() * 1000)

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS memories ("
                "id INTEGER PRIMARY KEY, content TEXT, conversation_key TEXT,"
                "created_at INTEGER, updated_at INTEGER, subject TEXT, status TEXT,"
                "confidence REAL, importance REAL, superseded_by INTEGER, pinned INTEGER)")
    seen = set()
    if not args.allow_duplicates:
        for (content,) in con.execute("select content from memories"):
            if isinstance(content, str):
                seen.add(hashlib.sha1(content.encode("utf-8")).hexdigest())

    added = skipped = 0
    try:
        for obj in raw_items:
            text = _text_of(obj) if isinstance(obj, dict) else str(obj).strip()
            if not text:
                skipped += 1
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            conv = str(obj.get("conversation") or obj.get("conversation_key") or args.conversation)
            speaker = str(obj.get("speaker") or obj.get("sender") or args.speaker or "")
            importance = float(obj.get("importance") or args.importance)
            importance = min(max(importance, 0.0), 1.0)
            at = _ms(obj.get("at") or obj.get("created_at") or obj.get("time"), now)
            if not args.dry_run:
                con.execute(
                    "INSERT INTO memories (content, conversation_key, created_at, updated_at,"
                    " subject, status, confidence, importance, superseded_by, pinned)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (text, conv, at, at, speaker, "active", 0.8, importance, None, 0),
                )
            added += 1
        if not args.dry_run:
            con.commit()
    finally:
        con.close()

    verb = "可导入" if args.dry_run else "已导入"
    print(f"{verb} {added} 条，跳过 {skipped} 条（空或重复）→ {db}")
    if args.dry_run:
        return
    try:
        # 必须在 import memory_view 之前把路径钉死到这家库，否则会读到别处的默认库。
        os.environ["SHARED_DB"] = db
        os.environ["VIEW_DB"] = os.path.join(store, "brain.db")
        os.environ["NEBULA_DATA"] = os.path.join(store, "data")
        sys.path.insert(0, store)
        import memory_view as mv
        graph = mv.build()
        mv.write_graph(graph, os.path.join(store, "web", "graph.json"))
        counts = (graph.get("meta") or {}).get("counts") or {}
        print("星云图已更新：" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    except Exception as error:
        print(f"（图没重建：{error}；打开页面时运行中的 viewer 会自动重建）")


if __name__ == "__main__":
    main()