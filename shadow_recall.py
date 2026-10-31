"""影子召回：并行跑新算法，只记录、不改回复。

调用点在 companion_brain.Brain.recall() 的 return 之前：
此时老算法的最终结果已经定型，我们把「老算法选了哪几条」和
「新算法（WhaleGirl 引擎）会选哪几条」一起写进 data/shadow_recall.jsonl，
方便一段时间后对比，再决定要不要真的切换。

开关：环境变量 SHADOW_RECALL=1，或 settings.json 里 recall.shadow=true。
默认关闭——没开就是零开销（只多一次 os.environ 判断）。

手动看效果：
    python shadow_recall.py --replay "今天好累啊" --speaker 10001
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "data", "shadow_recall.jsonl")
WHALEGIRL_HOME = os.environ.get(
    "WHALEGIRL_HOME", r"<工作区>"
)
CORE_DIR = os.path.join(WHALEGIRL_HOME, "memory-nebula", "data", "core")
STATE_DIR = os.path.join(HERE, "data", "mind-state")

_MIND = None          # 进程内复用，避免每条消息都重新加载 ONNX
_MIND_DB = ""


def enabled(settings_value: bool = False) -> bool:
    if os.environ.get("SHADOW_RECALL") == "1":
        return True
    if os.environ.get("SHADOW_RECALL") == "0":
        return False
    return bool(settings_value)


def _get_mind(db_path: str):
    """按库路径复用引擎实例；只读打开，绝不写线上库。"""
    global _MIND, _MIND_DB
    if _MIND is not None and _MIND_DB == db_path:
        return _MIND
    if WHALEGIRL_HOME not in sys.path:
        sys.path.insert(0, WHALEGIRL_HOME)
    from whalegirl.embedding import load_provider
    from whalegirl.nebula import NebulaMind

    prov = load_provider()
    _MIND = NebulaMind(db_path, CORE_DIR, STATE_DIR, writable=False,
                       provider_name=getattr(prov, "name", "heuristic"))
    _MIND_DB = db_path
    return _MIND


def _write(rec: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# 后台线程：影子计算绝不等在回复路径上。
# 首次调用要加载向量模型（约 0.5 秒），放同步会拖慢第一条回复，所以一律入队后立刻返回。
_QUEUE: "queue.Queue" = None
_WORKER = None
_QLOCK = threading.Lock()


def _worker_loop(q) -> None:
    while True:
        item = q.get()
        if item is None:
            return
        try:
            _run(*item)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.task_done()


def _ensure_worker() -> "queue.Queue":
    global _QUEUE, _WORKER
    with _QLOCK:
        if _QUEUE is None:
            _QUEUE = queue.Queue(maxsize=512)
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, args=(_QUEUE,),
                                       name="shadow-recall", daemon=True)
            _WORKER.start()
        return _QUEUE


def record(db_path: str, scope: str, speaker: str, audience: str, query: str,
           old_picked: Sequence[Dict[str, Any]], k: int = 5) -> None:
    """把一次影子比对排进后台队列，立即返回（不阻塞回复）。任何异常都不外抛。"""
    try:
        q = _ensure_worker()
        q.put_nowait((db_path, scope, speaker, audience, query, list(old_picked), k))
    except Exception:  # noqa: BLE001  队列满或线程起不来都直接放弃这次记录
        pass


def _run(db_path: str, scope: str, speaker: str, audience: str, query: str,
         old_picked: Sequence[Dict[str, Any]], k: int = 5) -> Optional[Dict[str, Any]]:
    """真正干活：跑一次新算法并与老结果比对，写一行 jsonl。"""
    t0 = time.time()
    try:
        mind = _get_mind(db_path)
        new_hits = mind.recall(query, scope, speaker, k=k, log=False)
    except Exception as e:  # noqa: BLE001
        _write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": f"{type(e).__name__}: {e}",
                "query": (query or "")[:200], "speaker": speaker})
        return None

    old_ids = [int(m["id"]) for m in old_picked]
    new_ids = [int(m["id"]) for m in new_hits]
    inter = set(old_ids) & set(new_ids)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "speaker": speaker,
        "audience": audience,
        "query": (query or "")[:300],
        "old": [{"id": m["id"], "score": m.get("score"), "via": m.get("via")} for m in old_picked],
        "new": [{"id": m["id"], "prob": m.get("prob"), "score": m.get("score"),
                 "layer": m.get("layer")} for m in new_hits],
        "overlap": len(inter),
        "jaccard": round(len(inter) / max(1, len(set(old_ids) | set(new_ids))), 3),
        "ms": int((time.time() - t0) * 1000),
    }
    _write(rec)
    return rec


def _load_mind_provider():
    from whalegirl.embedding import load_provider

    return load_provider()


def replay(query: str, speaker: str = "10001", db_path: str = "",
           scope: str = "dafeiyu", k: int = 5) -> None:
    """命令行自测：不经过 QQ，直接看两套算法各挑了什么。"""
    sys.path.insert(0, HERE)
    import companion_brain as CB

    db_path = db_path or os.path.join(HERE, "brain.db")
    brain = CB.Brain(db_path, scope=scope)
    old = brain.recall(speaker, query, limit=k, log=False)
    print(f"老算法（词面+联想）挑出 {len(old)} 条：")
    for m in old:
        print(f"  #{m['id']:>3} score={m['score']:.3f} via={m['via']}  {m['content'][:36]}")

    rec = _run(db_path, scope, speaker, f"private:{speaker}", query, old, k=k)
    if not rec:
        print("新算法跑失败，看 data/shadow_recall.jsonl 里的 error 行")
        return
    print(f"\n新算法（概率竞争+扩散）挑出 {len(rec['new'])} 条，"
          f"重合 {rec['overlap']} 条，Jaccard={rec['jaccard']}，耗时 {rec['ms']}ms：")
    mind = _get_mind(db_path)
    for m in rec["new"]:
        row = mind.brain.get(m["id"]) or {}
        print(f"  #{m['id']:>3} p={m['prob']:.3f} {m['layer']:<9} {(row.get('content') or '')[:36]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--replay" in args:
        i = args.index("--replay")
        q = args[i + 1] if i + 1 < len(args) else ""
        sp = args[args.index("--speaker") + 1] if "--speaker" in args else "10001"
        replay(q, sp)
    else:
        print("用法: python shadow_recall.py --replay \"用户原话\" [--speaker QQ]")