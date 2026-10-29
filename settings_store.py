# -*- coding: utf-8 -*-
"""全局开关：一个 settings.json 管所有能力，改完立即生效（按 mtime 热加载）。

所有模块共用这一份，不限于某个对话：
    companion_brain   情绪/记忆/召回/遗忘/向量
    persona_index     人格结构化
    memory_view       语义边/图
    viewer_server    设置接口

读：settings_store.get() 返回 dict
写：settings_store.save(d)  只允许改 KNOWN 里列出的键
"""
from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "settings.json")

DEFAULTS: dict = {
    "recall": {"enabled": True, "semantic": True, "synonym": True, "max_items": 5,
               "mmr": True, "mmr_lambda": 0.72, "log": True, "digest": True},
    "memory": {"write_gate": True, "consolidate": True, "abstract": True,
               "core_queue": True, "core_context": True},
    "emotion": {"enabled": True, "grudge": True},
    "forget": {"enabled": True, "half_life_days": 90.0},
    "persona": {"structured": True},
    "viewer": {"semantic_edges": True, "auto_rebuild": True},
    "embed": {"provider": "cloud"},          # cloud / off
}

_lock = threading.Lock()
_cache: dict = {"mtime": 0.0, "data": None}


def _merge(base: dict, over: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def get() -> dict:
    """热加载：文件没变就用缓存，变了立刻重读。"""
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return json.loads(json.dumps(DEFAULTS))
    with _lock:
        if _cache["data"] is not None and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            with open(PATH, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        data = _merge(DEFAULTS, raw)
        _cache["mtime"] = mtime
        _cache["data"] = data
        return data


def on(path: str, default: bool = True) -> bool:
    """on("recall.enabled") 这样取布尔开关。"""
    cur: object = get()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return bool(cur) if isinstance(cur, bool) else default


def save(data: dict) -> dict:
    merged = _merge(get(), data)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PATH)
    with _lock:
        _cache["mtime"] = 0.0
        _cache["data"] = None
    return merged


def ensure_file() -> None:
    if not os.path.exists(PATH):
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    ensure_file()
    print(json.dumps(get(), ensure_ascii=False, indent=2))