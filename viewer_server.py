# -*- coding: utf-8 -*-
"""记忆星云本地服务：静态页面 + 只对本机开放的读写接口。

记忆有两份，节点 id 用 ``mem:<库>:<id>`` 区分，写接口也按这个前缀路由：

    s = 共用记忆 ``bridge.db``   QQ 每轮真实问答都会写，页面里的主体；
                                改它会被另一个端同步走
    b = 本地伴生 ``brain.db``    伴生大脑提炼过的事实，带重要度/强度/向量

接口：
    POST /api/memory     {items:[{store,id}], op, text}  批量（可混库）
    POST /api/memory     {store,id,op,text}              单条（store 可省略）
    POST /api/memory     {ids:[...], store, op}          兼容旧批量写法
    POST /api/rebuild                                    立即重建 graph.json
    POST /api/relation   {qq, ring}                      改一个人的关系圈层（inner/outer/auto）
    GET  /api/export                                     导出全部记忆 JSON
    GET  /api/settings                                   读全局开关
    POST /api/settings   {任意子集}                       改全局开关

所有写操作完成后自动重建 graph.json。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote


class ViewerHTTPServer(ThreadingHTTPServer):
    """Threaded viewer server with a larger accept backlog.

    The default backlog is 5, which is enough for one browser but produces
    connection resets when several phones open the page at the same time.
    """
    daemon_threads = True
    request_queue_size = 256


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
WEB = os.path.join(HERE, "web")
DB = os.environ.get("VIEW_DB") or os.path.join(HERE, "brain.db")

import accounts as accounts_store  # noqa: E402
import memory_view as mv  # noqa: E402
import settings_store  # noqa: E402

STORE_LABEL = {"s": "共用记忆", "b": "本地伴生"}

# 只读模式（给别人看时用）：所有写接口一律 403，页面也会把编辑按钮藏起来
READONLY = False
HOST = "127.0.0.1"

# 账号模式：每个账号（QQ 号）只能看自己那一份图
ACCOUNTS = False
# 访客模式：不登录也能看所有球体，但所有人的具体内容都抹掉
GUEST = False
GRAPH_PATH = os.environ.get("NEBULA_GRAPH") or os.path.join(WEB, "graph.json")
# 7 天免登录凭证只存 SHA-256，服务重启后仍有效；文件里没有原始 Cookie。
SESSIONS: dict[str, dict] = {}
SESSIONS_PATH = (os.environ.get("NEBULA_SESSIONS")
                 or os.path.join(HERE, "runtime", "sessions.json"))
SESSION_TTL = 7 * 86400
_SESSIONS_LOCK = threading.RLock()
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_WINDOW = 600
_LOGIN_MAX = 8
_LOGIN_SEM = threading.BoundedSemaphore(8)   # 同时最多 8 个登录校验，防线程/PBKDF2 被刷爆

# 同一个进程里可能同时有「写入后重建」和页面点「同步」，串行化避免抢 graph.json.tmp
_REBUILD_LOCK = threading.Lock()
_MAX_BODY = 2 * 1024 * 1024      # 单个请求体上限，防止随便塞大 JSON 占内存
_MAX_ITEMS = 500                 # 一次最多批量改这么多条
_REBUILD_GAP = 5.0               # 同一个账号两次手动「同步」的最小间隔（秒）
_REBUILD_LAST: dict[str, float] = {}


def _shared_db_path() -> str:
    """共用记忆库位置：环境变量 SHARED_DB 优先，否则用仓库根目录的 bridge.db。"""
    env = os.environ.get("SHARED_DB")
    if env:
        return env
    return os.path.join(HERE, "bridge.db")


SHARED_DB = _shared_db_path()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_s() -> int:
    return int(time.time())


def _db_path(store: str) -> str:
    return SHARED_DB if store == "s" else DB


def _norm_store(value) -> str:
    s = str(value or "").strip().lower()
    if s in ("s", "shared", "bridge", "共用记忆", "共用"):
        return "s"
    if s in ("b", "brain", "local", "本地记忆", "本地伴生", "本地"):
        return "b"
    return ""


def _rebuild() -> dict:
    """把当前数据库重新导成 graph.json，返回统计给页面提示。"""
    try:
        import memory_view as mv
        with _REBUILD_LOCK:
            g = mv.build()
            mv.write_graph(g, GRAPH_PATH)
        counts = g.get("meta", {}).get("counts") or {}
        sources = g.get("meta", {}).get("sources") or {}
        return {"ok": True, "nodes": len(g["nodes"]),
                "memories": counts.get("memory", 0),
                "shared": sources.get("共用记忆", 0),
                "local": sources.get("本地记忆", 0),
                "pinned": sources.get("永久", 0)}
    except Exception as e:  # noqa: BLE001
        # 异常里可能带绝对路径，只写日志，不把细节发给任何登录用户
        print("重建失败:", type(e).__name__, e)
        return {"ok": False, "error": "重建失败，详情见服务端日志"}


def _connect(store: str) -> sqlite3.Connection:
    path = _db_path(store)
    if not path or not os.path.exists(path):
        raise LookupError(f"{STORE_LABEL.get(store, store)}库不存在")
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_shared_columns(conn: sqlite3.Connection) -> None:
    """共用记忆库原来没这几列：页面要调重要度/合并/永久，按需补上（只加列，不改旧数据）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    if "importance" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 0.6")
    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN superseded_by INTEGER")
    if "pinned" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0")
    conn.commit()


def _ensure_brain_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    if "pinned" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0")
    if "subject" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN subject TEXT DEFAULT ''")
    conn.commit()


def _one(conn: sqlite3.Connection, store: str, op: str, mid: int,
         value: float, text: str) -> None:
    row = conn.execute("SELECT id FROM memories WHERE id=?", (mid,)).fetchone()
    if row is None:
        raise LookupError(f"没有 id={mid} 这条记忆")

    if store == "s":
        now = _now_ms()
        if op == "forget":
            conn.execute("UPDATE memories SET status='faded', updated_at=? WHERE id=?",
                         (now, mid))
        elif op == "restore":
            conn.execute("UPDATE memories SET status='active', updated_at=? WHERE id=?",
                         (now, mid))
        elif op == "delete":
            conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        elif op == "important":
            conn.execute("UPDATE memories SET importance=MIN(1.0,COALESCE(importance,0.6)+?),"
                         " updated_at=? WHERE id=?", (value or 0.2, now, mid))
        elif op == "less":
            conn.execute("UPDATE memories SET importance=MAX(0.0,COALESCE(importance,0.6)-?)"
                         " WHERE id=?", (value or 0.2, mid))
        elif op == "pin":
            # 永久记忆：顺手从忘掉状态恢复，时间戳刷新，球永远满亮
            conn.execute("UPDATE memories SET pinned=1, status='active', updated_at=?"
                         " WHERE id=?", (now, mid))
        elif op == "unpin":
            conn.execute("UPDATE memories SET pinned=0, updated_at=? WHERE id=?", (now, mid))
        elif op == "edit":
            body = (text or "").strip()
            if not body:
                raise ValueError("正文不能为空")
            first = body.split("\n", 1)[0].strip()
            if first.startswith("用户："):
                first = first[3:].strip()
            conn.execute("UPDATE memories SET content=?, subject=?, updated_at=?"
                         " WHERE id=?", (body[:1200], first[:24], now, mid))
        else:
            raise ValueError(f"未知操作 {op}")
        return

    # 本地伴生：沿用原来的秒级时间戳和向量清理
    if op == "forget":
        conn.execute("UPDATE memories SET status='faded', strength=0.05 WHERE id=?", (mid,))
    elif op == "restore":
        conn.execute("UPDATE memories SET status='active', strength=1.0 WHERE id=?", (mid,))
    elif op == "delete":
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        try:
            conn.execute("DELETE FROM embeddings WHERE memory_id=?", (mid,))
        except sqlite3.Error:
            pass
    elif op == "important":
        conn.execute("UPDATE memories SET importance=MIN(1.0,importance+?), strength=1.0"
                     " WHERE id=?", (value or 0.2, mid))
    elif op == "less":
        conn.execute("UPDATE memories SET importance=MAX(0.0,importance-?) WHERE id=?",
                     (value or 0.2, mid))
    elif op == "pin":
        conn.execute("UPDATE memories SET pinned=1, status='active', strength=1.0,"
                     " updated_at=strftime('%s','now') WHERE id=?", (mid,))
    elif op == "unpin":
        conn.execute("UPDATE memories SET pinned=0, updated_at=strftime('%s','now')"
                     " WHERE id=?", (mid,))
    elif op == "edit":
        body = (text or "").strip()
        if not body:
            raise ValueError("正文不能为空")
        try:
            import companion_brain as _cb
            subj = _cb._fact_subject(_cb._fact_text(body))
        except Exception:
            subj = ""
        conn.execute("UPDATE memories SET content=?, subject=?,"
                     " updated_at=strftime('%s','now') WHERE id=?",
                     (body[:800], subj, mid))
        try:
            conn.execute("DELETE FROM embeddings WHERE memory_id=?", (mid,))
        except sqlite3.Error:
            pass
    else:
        raise ValueError(f"未知操作 {op}")


def _merge(conn: sqlite3.Connection, store: str, ids: list[int]) -> None:
    if len(ids) < 2:
        raise ValueError("合并至少要选两条")
    keep, *others = ids
    for o in others:
        if store == "s":
            # 共用记忆库同步只认 status='active'：标成 superseded 后下游也会撤掉旧的
            conn.execute("UPDATE memories SET status='superseded', superseded_by=?,"
                         " updated_at=? WHERE id=?", (keep, _now_ms(), o))
        else:
            conn.execute("UPDATE memories SET status='superseded', superseded_by=?"
                         " WHERE id=?", (keep, o))


def _guess_store(mid: int) -> str:
    """老接口只给 id：先看共用记忆，再看本地伴生。"""
    for store in ("s", "b"):
        path = _db_path(store)
        if not path or not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                if conn.execute("SELECT 1 FROM memories WHERE id=?", (mid,)).fetchone():
                    return store
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            continue
    return "s"


def _normalise_items(payload: dict) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    items = payload.get("items")
    if items:
        if len(items) > _MAX_ITEMS:
            raise ValueError(f"一次最多改 {_MAX_ITEMS} 条")
        for it in items:
            if not isinstance(it, dict) or it.get("id") is None:
                continue
            mid = int(it["id"])
            out.append((_norm_store(it.get("store")) or _guess_store(mid), mid))
        return out
    if payload.get("ids"):
        if len(payload["ids"]) > _MAX_ITEMS:
            raise ValueError(f"一次最多改 {_MAX_ITEMS} 条")
        store = _norm_store(payload.get("store"))
        for raw in payload["ids"]:
            mid = int(raw)
            out.append((store or _guess_store(mid), mid))
        return out
    if payload.get("id") is not None:
        mid = int(payload["id"])
        return [(_norm_store(payload.get("store")) or _guess_store(mid), mid)]
    return out


def _apply(payload: dict, owner: str | None = None) -> dict:
    op = str(payload.get("op") or "")
    try:
        items = _normalise_items(payload)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": str(e) or "id 必须是数字"}
    if not items:
        return {"ok": False, "error": "缺少 id"}
    try:
        value = float(payload.get("value") or 0.2)
    except (TypeError, ValueError):
        value = 0.2
    text = str(payload.get("text") or "")

    groups: dict[str, list[int]] = {}
    for store, mid in items:
        groups.setdefault(store, []).append(mid)

    if op == "merge" and any(len(ids) < 2 for ids in groups.values()):
        return {"ok": False, "error": "合并至少要选两条（同一个记忆库内）"}

    # 先把所有库、所有 id 都核对一遍，再动手：混库批量要么全成要么全不动
    for store, ids in groups.items():
        if store not in STORE_LABEL:
            return {"ok": False, "error": f"未知记忆库 {store}"}
        path = _db_path(store)
        if not path or not os.path.exists(path):
            return {"ok": False, "error": f"{STORE_LABEL[store]}库不存在"}
        try:
            probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            bad: list[int] = []
            try:
                missing = [mid for mid in ids
                           if probe.execute("SELECT 1 FROM memories WHERE id=?",
                                            (mid,)).fetchone() is None]
                if owner and not missing:
                    key_col = "conversation_key" if store == "s" else "speaker_id"
                    want = f"private:{owner}" if store == "s" else str(owner)
                    got = {r[0]: str(r[1] or "") for r in probe.execute(
                        f"SELECT id,{key_col} FROM memories WHERE id IN "
                        f"({','.join('?' * len(ids))})", ids)}
                    bad = [mid for mid in ids if got.get(mid) != want]
            finally:
                probe.close()
        except sqlite3.Error as e:
            return {"ok": False, "error": f"读{STORE_LABEL[store]}库失败：{e}"}
        if missing:
            return {"ok": False,
                    "error": "没有这些记忆：" + ", ".join(str(m) for m in missing[:5])}
        if owner and bad:
            return {"ok": False, "forbidden": True,
                    "error": "普通账号只能改自己的记忆：" + ", ".join(str(m) for m in bad[:5])}

    for store, ids in groups.items():
        conn = _connect(store)
        try:
            if store == "s":
                _ensure_shared_columns(conn)
            else:
                _ensure_brain_columns(conn)
            if op == "merge":
                _merge(conn, store, ids)
            else:
                for mid in ids:
                    _one(conn, store, op, mid, value, text)
            conn.commit()
        except (LookupError, ValueError) as e:
            conn.rollback()
            return {"ok": False, "error": str(e)}
        except sqlite3.Error as e:
            conn.rollback()
            return {"ok": False, "error": f"数据库错误：{e}"}
        finally:
            conn.close()

    return {"ok": True, "op": op,
            "changed": {store: len(ids) for store, ids in groups.items()}}


def _export() -> dict:
    memories: list[dict] = []
    found = False
    for store in ("s", "b"):
        path = _db_path(store)
        if not path or not os.path.exists(path):
            continue
        found = True
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                for r in conn.execute("SELECT * FROM memories ORDER BY id"):
                    item = dict(r)
                    item["store"] = store
                    item["store_name"] = STORE_LABEL[store]
                    memories.append(item)
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    if not found:
        return {"ok": False, "error": "没有可导出的记忆库"}
    return {"ok": True, "exported": len(memories),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "memories": memories}


def _session_key(token: str) -> str:
    """Cookie 里的原始凭证不落盘，只存摘要。"""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _pwtag(rec: dict) -> str:
    """密码指纹：改密后旧会话即使在别的进程里也会立刻失效。"""
    raw = f"{rec.get('salt') or ''}:{rec.get('hash') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_sessions_file() -> dict[str, dict]:
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    items = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    for key, rec in items.items():
        if not isinstance(rec, dict):
            continue
        try:
            exp = float(rec.get("exp") or 0)
        except (TypeError, ValueError):
            continue
        qq = str(rec.get("qq") or "").strip()
        if key and qq and exp > now:
            out[str(key)] = {"qq": qq, "exp": exp,
                             "pwtag": str(rec.get("pwtag") or ""),
                             "remember": True}
    return out


def _save_sessions() -> None:
    """原子写回；权限收紧到当前用户，文件里只有 qq/过期时间/token 摘要。"""
    with _SESSIONS_LOCK:
        persist = {k: dict(v) for k, v in SESSIONS.items()
                   if v.get("remember", True)}
        data = {"version": 1, "sessions": persist}
        try:
            os.makedirs(os.path.dirname(SESSIONS_PATH) or ".", exist_ok=True)
            tmp = SESSIONS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, SESSIONS_PATH)
            try:
                accounts_store._harden(SESSIONS_PATH)
            except Exception:
                pass
        except OSError:
            # 磁盘不可写时退化为内存会话，不影响本次登录。
            pass


def _drop_sessions(qq: str) -> None:
    """改密/删号/改角色后，让这个 QQ 的旧会话立刻失效。"""
    changed = False
    with _SESSIONS_LOCK:
        for tok, rec in list(SESSIONS.items()):
            if str(rec.get("qq") or "") == str(qq):
                SESSIONS.pop(tok, None)
                changed = True
        if changed:
            _save_sessions()


# 启动时恢复上次的 7 天凭证；必须放在 _load_sessions_file 定义之后。
SESSIONS.update(_load_sessions_file())


def _brain_conn() -> sqlite3.Connection | None:
    if not DB or not os.path.exists(DB):
        return None
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _recall_payload(owner: str | None = None, limit: int = 50) -> list[dict]:
    """召回日志：普通账号只看自己的 speaker_id，管理员看全部。"""
    conn = _brain_conn()
    if conn is None:
        return []
    try:
        limit = max(1, min(200, int(limit or 50)))
        sql = ("SELECT id,speaker_id,audience,query,query_hash,candidate_count,"
               "selected_count,selected,injected_chars,created_at FROM recall_log")
        args: list = []
        if owner:
            sql += " WHERE speaker_id=?"
            args.append(owner)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(sql, args):
            row = dict(r)
            try:
                row["selected"] = json.loads(row.get("selected") or "[]")
            except Exception:
                row["selected"] = []
            out.append(row)
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _core_payload(owner: str | None = None, limit: int = 50) -> list[dict]:
    """待审核心候选；只读打开 brain.db，不抢 bot 的写锁。"""
    conn = _brain_conn()
    if conn is None:
        return []
    try:
        limit = max(1, min(200, int(limit or 50)))
        sql = ("SELECT q.id AS qid,q.memory_id,q.score,q.reason,q.created_at,"
               "m.content,m.speaker_id,m.audience,m.importance,m.access_count"
               " FROM core_queue q JOIN memories m ON m.id=q.memory_id"
               " WHERE q.status='pending' AND m.status='active'")
        args: list = []
        if owner:
            sql += " AND m.speaker_id=?"
            args.append(owner)
        sql += " ORDER BY q.score DESC,q.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _conflicts_payload(owner: str | None = None, limit: int = 50) -> list[dict]:
    conn = _brain_conn()
    if conn is None:
        return []
    try:
        limit = max(1, min(200, int(limit or 50)))
        sql = ("SELECT c.id,c.speaker_id,c.audience,c.reason,c.created_at,"
               "o.content AS old_content,n.content AS new_content"
               " FROM conflicts c JOIN memories o ON o.id=c.old_id"
               " JOIN memories n ON n.id=c.new_id WHERE c.status='pending'")
        args: list = []
        if owner:
            sql += " AND c.speaker_id=?"
            args.append(owner)
        sql += " ORDER BY c.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _conflict_action(conflict_id: int, op: str, owner: str | None = None) -> dict:
    """人工选边：keep_old 保旧值，keep_new 让新值生效。"""
    if op not in ("keep_old", "keep_new"):
        return {"ok": False, "error": "未知操作"}
    if not DB or not os.path.exists(DB):
        return {"ok": False, "error": "brain.db 不存在"}
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM conflicts WHERE id=? AND status='pending'",
            (int(conflict_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "没有这个冲突"}
        if owner and str(row["speaker_id"] or "") != str(owner):
            return {"ok": False, "forbidden": True, "error": "只能处理自己的记忆"}
        old_id, new_id = int(row["old_id"]), int(row["new_id"])
        now = _now_s()
        if op == "keep_old":
            conn.execute("UPDATE memories SET status='superseded',superseded_by=?,"
                         "updated_at=? WHERE id=?", (old_id, now, new_id))
        else:
            conn.execute("UPDATE memories SET status='superseded',superseded_by=?,"
                         "pinned=0,updated_at=? WHERE id=?", (new_id, now, old_id))
            conn.execute("UPDATE memories SET status='active',superseded_by=NULL,"
                         "updated_at=? WHERE id=?", (now, new_id))
        conn.execute("UPDATE conflicts SET status=?,resolved_at=? WHERE id=?",
                     (op, now, int(conflict_id)))
        conn.commit()
        return {"ok": True, "id": int(conflict_id), "op": op}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"数据库错误：{type(e).__name__}"}
    finally:
        conn.close()


def _core_action(queue_id: int, op: str, owner: str | None = None) -> dict:
    """批准=设为永久；拒绝=只关队列项，不动记忆。"""
    if op not in ("approve", "reject"):
        return {"ok": False, "error": "未知操作"}
    if not DB or not os.path.exists(DB):
        return {"ok": False, "error": "brain.db 不存在"}
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT q.*,m.speaker_id,m.status AS mem_status FROM core_queue q"
            " JOIN memories m ON m.id=q.memory_id WHERE q.id=?", (int(queue_id),)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "没有这个候选"}
        if owner and str(row["speaker_id"] or "") != str(owner):
            return {"ok": False, "forbidden": True, "error": "只能处理自己的记忆"}
        if str(row["status"]) != "pending":
            return {"ok": False, "error": "这个候选已经处理过了"}
        if op == "approve":
            _ensure_brain_columns(conn)
            conn.execute("UPDATE core_queue SET status='approved',reviewed_at=? WHERE id=?",
                         (_now_s(), int(queue_id)))
            conn.execute("UPDATE memories SET pinned=1,status='active',strength=1.0,"
                         "updated_at=? WHERE id=?", (_now_s(), int(row["memory_id"])))
        else:
            conn.execute("UPDATE core_queue SET status='rejected',reviewed_at=? WHERE id=?",
                         (_now_s(), int(queue_id)))
        conn.commit()
        return {"ok": True, "id": int(queue_id), "op": op}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"数据库错误：{type(e).__name__}"}
    finally:
        conn.close()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        # 不主动暴露 Python/版本号，少给扫描器一点信息
        return "Nebula"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def handle_one_request(self) -> None:
        self._cache_set = False
        super().handle_one_request()

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _drain_body(self) -> None:
        """拒绝写请求时把请求体读掉，避免 keep-alive 连接留下残包。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(min(n, _MAX_BODY))
        except (TypeError, ValueError):
            pass

    def _read_json(self) -> dict | None:
        """只收 JSON 对象，并限制请求体大小；顺便挡掉表单式 CSRF。"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"ok": False, "error": "请用 application/json 提交"})
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"ok": False, "error": "请求长度不对"})
            return None
        if n <= 0:
            self._json(400, {"ok": False, "error": "请求为空"})
            return None
        if n > _MAX_BODY:
            self._json(413, {"ok": False, "error": "请求太大"})
            return None
        try:
            obj = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json(400, {"ok": False, "error": "JSON 格式不对"})
            return None
        if not isinstance(obj, dict):
            self._json(400, {"ok": False, "error": "JSON 必须是对象"})
            return None
        return obj

    def _send(self, code: int, data: bytes, ctype: str, cache: str,
              cookie: str | None = None) -> None:
        if len(data) > 1024 and self._accepts_gzip():
            data = gzip.compress(data, 6)
            self.send_response(code)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        else:
            self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self._cache_set = True
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict, cookie: str | None = None) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8", "no-store", cookie)

    def _html(self, name: str) -> None:
        path = os.path.join(WEB, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, {"ok": False, "error": "页面不存在"})
        self._send(200, data, "text/html; charset=utf-8", "no-store")

    def send_head(self):
        # 第三方大库（three.js/3d-force-graph）预压缩并按文件名长期缓存；
        # 页面里用 ?v=N 改 URL 来强制刷新。其他文件继续走原逻辑。
        p = self.path.split("?", 1)[0].split("#", 1)[0]
        if p.startswith("/vendor/") and self._accepts_gzip():
            fs = self.translate_path(p)
            gz = fs + ".gz"
            if os.path.isfile(fs) and os.path.isfile(gz):
                try:
                    with open(gz, "rb") as f:
                        data = f.read()
                except OSError:
                    data = b""
                if data:
                    self.send_response(200)
                    self.send_header("Content-Type", self.guess_type(fs))
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    self._cache_set = True
                    self.end_headers()
                    return io.BytesIO(data)
        return super().send_head()

    def end_headers(self) -> None:
        # 基础安全头（明确不嗅探 MIME、不许被嵌 iframe、不外带 Referer）
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy",
                         "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        # 页面目前是单文件内联脚本/样式；CSP 至少封死外链脚本、对象、iframe 和
        # 内联事件属性（script-src-attr），XSS 即使漏进 HTML 也不能靠 onerror 执行。
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "script-src-attr 'none'; connect-src 'self'; font-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; worker-src 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        # 页面/脚本每次都取最新的，改完刷新立刻生效（避免用旧缓存）
        p = self.path.split("?", 1)[0]
        if not getattr(self, "_cache_set", False) and (
                p.endswith((".html", ".js", ".css", ".json")) or p.endswith("/")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _redirect(self, to: str, cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nebula_sid":
                return v
        return ""

    def _qq(self) -> str:
        """当前登录的 QQ；没登录或过期返回空串。凭证摘要持久化，重启后仍有效。"""
        tok = self._token()
        if not tok:
            return ""
        key = _session_key(tok)
        with _SESSIONS_LOCK:
            rec = SESSIONS.get(key)
            if not rec:
                return ""
            if float(rec.get("exp") or 0) < time.time():
                SESSIONS.pop(key, None)
                _save_sessions()
                return ""
            qq = str(rec.get("qq") or "")
            acc = accounts_store.load().get(qq)
            # 密码改过 / 账号被删：不再信任旧 Cookie，跨进程也立即失效。
            if not acc or str(rec.get("pwtag") or "") != _pwtag(acc):
                SESSIONS.pop(key, None)
                _save_sessions()
                return ""
            return qq

    def _need_login(self) -> str:
        qq = self._qq()
        if not qq:
            self._json(401, {"ok": False, "error": "请先登录"})
        return qq

    def _cookie(self, token: str, max_age: int | None) -> str:
        # 公网 HTTPS（Funnel 会带 X-Forwarded-Proto: https）加 Secure；
        # 本机 http://127.0.0.1 不加，否则浏览器不会存 cookie，自己反而登不上。
        # max_age=None 表示浏览器会话 Cookie（关闭浏览器即掉线），不写 Max-Age。
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        age = "" if max_age is None else f"; Max-Age={max_age}"
        return (f"nebula_sid={token}; Path=/; HttpOnly; SameSite=Lax{secure}{age}")

    def _graph(self) -> dict | None:
        try:
            with open(GRAPH_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _login(self) -> None:
        body = self._read_json()
        if body is None:
            return
        # 公网登录口不能让人开几千个线程一起跑 PBKDF2；超出并发直接 429。
        if not _LOGIN_SEM.acquire(blocking=False):
            return self._json(429, {"ok": False, "error": "登录太频繁，稍后再试"})
        try:
            return self._login_body(body)
        finally:
            _LOGIN_SEM.release()

    def _login_body(self, body: dict) -> None:
        qq = str(body.get("qq") or "").strip()[:32]
        pw = str(body.get("password") or "")[:200]
        remember = body.get("remember", True)
        if isinstance(remember, str):
            remember = remember.strip().lower() not in ("0", "false", "off", "no", "")
        remember = bool(remember)
        if not qq or not pw:
            return self._json(400, {"ok": False, "error": "QQ 号和密码不能为空"})
        now = time.time()
        # 清掉过期记录；攻击者可以随便编 QQ 号，字典要设上限
        for k in [k for k, v in _LOGIN_FAILS.items()
                  if not any(now - t < _LOGIN_WINDOW for t in v)]:
            _LOGIN_FAILS.pop(k, None)
        if len(_LOGIN_FAILS) > 5000:
            _LOGIN_FAILS.clear()
        # 按账号限流：Funnel 后面所有请求的 client_address 都是 127.0.0.1，
        # 再按 IP 限流会变成“一个人试错，所有人被锁”。按 QQ 限流也挡分布式撞库。
        key = qq
        fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW]
        if len(fails) >= _LOGIN_MAX:
            left = max(1, int(_LOGIN_WINDOW - (now - fails[0])) // 60 + 1)
            return self._json(429, {"ok": False,
                                    "error": f"试错太多次，{left} 分钟后再来"})
        rec = accounts_store.load().get(qq)
        if not rec:
            # 不存在的账号也做一次同成本校验，避免靠响应时间枚举 QQ 号
            accounts_store.verify(pw, {"salt": "00" * 16, "hash": "00" * 32,
                                       "iter": 200_000})
        if not rec or not accounts_store.verify(pw, rec):
            fails.append(now)
            _LOGIN_FAILS[key] = fails
            time.sleep(0.25)
            return self._json(401, {"ok": False, "error": "QQ 号或密码不对"})
        _LOGIN_FAILS.pop(key, None)
        # 顺手清掉过期会话，别让内存和文件一直涨
        token = secrets.token_urlsafe(32)
        with _SESSIONS_LOCK:
            for key in [k for k, r in SESSIONS.items()
                        if float(r.get("exp") or 0) < now]:
                SESSIONS.pop(key, None)
            SESSIONS[_session_key(token)] = {"qq": qq, "exp": now + SESSION_TTL,
                                             "pwtag": _pwtag(rec),
                                             "remember": remember}
            _save_sessions()
        nick = str(rec.get("nick") or "")
        return self._json(200, {"ok": True, "qq": qq, "nick": nick,
                                "admin": bool(rec.get("admin")),
                                "remember": remember},
                          cookie=self._cookie(token, SESSION_TTL if remember else None))

    def _admin(self, qq: str) -> bool:
        """管理员能看所有人、能改；角色每次现读 accounts.json，改了立刻生效。"""
        return bool((accounts_store.load().get(qq) or {}).get("admin"))

    def _accounts_post(self, actor: str) -> None:
        """管理员增删账号 / 改密码 / 改角色；不能删自己或改自己的角色。"""
        body = self._read_json()
        if body is None:
            return
        action = str(body.get("action") or "")
        qq = str(body.get("qq") or "").strip()[:32]
        try:
            if action == "add":
                pw = str(body.get("password") or "").strip()[:200] or None
                qq, pw = accounts_store.add(qq, pw, admin=bool(body.get("admin")))
                return self._json(200, {"ok": True, "qq": qq, "password": pw,
                                        "accounts": accounts_store.list_accounts()})
            if action == "passwd":
                pw = str(body.get("password") or "").strip()[:200] or None
                qq, pw = accounts_store.set_password(qq, pw)
                _drop_sessions(qq)
                return self._json(200, {"ok": True, "qq": qq, "password": pw,
                                        "accounts": accounts_store.list_accounts()})
            if action == "remove":
                if qq == actor:
                    return self._json(400, {"ok": False, "error": "不能删除当前登录的账号"})
                if not accounts_store.remove(qq):
                    return self._json(404, {"ok": False, "error": "没有这个账号"})
                _drop_sessions(qq)
                return self._json(200, {"ok": True,
                                        "accounts": accounts_store.list_accounts()})
            if action == "role":
                if qq == actor:
                    return self._json(400, {"ok": False, "error": "不能改自己的角色"})
                if not accounts_store.set_admin(qq, bool(body.get("admin"))):
                    return self._json(404, {"ok": False, "error": "没有这个账号"})
                _drop_sessions(qq)
                return self._json(200, {"ok": True,
                                        "accounts": accounts_store.list_accounts()})
            return self._json(400, {"ok": False, "error": "未知操作"})
        except (ValueError, KeyError) as e:
            return self._json(400, {"ok": False, "error": str(e)})

    def _accounts_get(self, path: str) -> None:
        if path in ("/login", "/login.html"):
            if self._qq():
                return self._redirect("/")
            return self._html("login.html")
        if path == "/logout":
            tok = self._token()
            if tok:
                with _SESSIONS_LOCK:
                    SESSIONS.pop(_session_key(tok), None)
                    _save_sessions()
            return self._redirect("/login", cookie=self._cookie("", 0))
        if path == "/api/login":
            return self._json(405, {"ok": False, "error": "登录请用 POST"})
        qq = self._qq()
        if path in ("/life", "/life.html", "/core.html"):
            if not qq:
                return self._redirect("/login")
            # 数字生命页是管理员后台：情绪、执念、疤痕、梦境和迁移包不是普通账号的数据。
            if not self._admin(qq):
                return self._redirect("/")
            return self._html("core.html")
        if path in ("/api/life/state", "/api/core/state"):
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            if not self._admin(qq):
                return self._json(403, {"ok": False, "error": "数字生命页仅管理员可用"})
            import core_api
            admin = self._admin(qq)
            st = core_api.state(admin=admin)
            rec = accounts_store.load().get(qq) or {}
            st["me"] = {"qq": qq, "label": rec.get("nick") or qq,
                        "admin": admin}
            return self._json(200, st)
        if path.startswith("/life/vault/") or path.startswith("/api/life/vault/"):
            if not qq:
                return self._redirect("/login")
            if not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能下载迁移包"})
            vid = unquote(path.rsplit("/", 1)[-1].strip())
            try:
                import core_life as CL
                fp = CL.vault_zip(vid)
            except Exception:
                return self._json(404, {"ok": False, "error": "没有这个迁移包"})
            data = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            fname = os.path.basename(fp)
            self.send_header("Content-Disposition",
                             "attachment; filename=\"vault.zip\"; filename*=UTF-8''"
                             + quote(fname, safe=""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/mode":
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            rec = accounts_store.load().get(qq) or {}
            admin = self._admin(qq)
            return self._json(200, {"ok": True, "readonly": False, "accounts": True,
                                    "host": HOST, "port": self.server.server_port,
                                    "me": {"qq": qq, "label": rec.get("nick") or qq,
                                           "admin": admin}})
        if path in ("/", "/index.html"):
            if not qq:
                return self._redirect("/login")
            return self._html("index.html")
        if path == "/api/graph":
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            g = self._graph()
            if g is None:
                return self._json(503, {"ok": False, "error": "graph.json 还没生成"})
            return self._json(200, g if self._admin(qq) else mv.overview_graph(g, qq))
        if path == "/api/export":
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            data = _export()
            g = self._graph()
            if g is not None and data.get("ok") and not self._admin(qq):
                allowed = {n["id"] for n in mv.scope_graph(g, qq)["nodes"]}
                data["memories"] = [
                    m for m in data.get("memories") or []
                    if f"mem:{m.get('store')}:{m.get('id')}" in allowed]
                data["exported"] = len(data["memories"])
            return self._json(200, data)
        if path == "/api/settings":
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            if not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能看开关"})
            return self._json(200, {"ok": True, "settings": settings_store.get()})
        if path in ("/api/recall-log", "/api/core", "/api/conflicts"):
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            owner = None if self._admin(qq) else qq
            if path == "/api/recall-log":
                return self._json(200, {"ok": True,
                                        "items": _recall_payload(owner, limit)})
            if path == "/api/core":
                return self._json(200, {"ok": True,
                                        "items": _core_payload(owner, limit)})
            return self._json(200, {"ok": True,
                                    "items": _conflicts_payload(owner, limit)})
        if path == "/api/accounts":
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            if not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能看账号"})
            return self._json(200, {"ok": True, "accounts": accounts_store.list_accounts()})
        if path == "/settings.html":
            if not qq:
                return self._redirect("/login")
            if not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能改开关"})
            return super().do_GET()
        if path in ("/vendor", "/vendor/"):
            return self._json(404, {"ok": False, "error": "没有这个页面"})
        if path.startswith("/vendor/"):
            return super().do_GET()
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not qq:
            return self._redirect("/login")
        # 账号模式下 graph.json 不直接给：它包含所有人的数据，只能走 /api/graph 裁剪
        return self._json(404, {"ok": False, "error": "没有这个页面"})

    def do_HEAD(self):  # noqa: N802
        # 账号模式下 HEAD 会绕过 do_GET 的登录检查直接走静态文件处理，
        # 至少会泄露文件是否存在/大小；直接不允许。
        if ACCOUNTS:
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/vendor", "/vendor/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if GUEST and path in ("/graph.json", "/settings.html", "/README.md",
                              "/life", "/life.html", "/core.html"):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_HEAD()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if ACCOUNTS:
            return self._accounts_get(path)
        if path == "/api/graph":
            g = self._graph()
            if g is None:
                return self._json(503, {"ok": False, "error": "graph.json 还没生成"})
            return self._json(200, mv.guest_graph(g) if GUEST else g)
        if path == "/api/mode":
            return self._json(200, {"ok": True, "readonly": READONLY, "accounts": False,
                                    "guest": GUEST,
                                    "host": HOST, "port": self.server.server_port})
        if GUEST and path in ("/api/export", "/api/settings", "/api/recall-log",
                              "/api/core", "/api/conflicts"):
            return self._json(403, {"ok": False, "error": "请登录后查看"})
        if path == "/api/export":
            return self._json(200, _export())
        if path == "/api/settings":
            return self._json(200, {"ok": True, "settings": settings_store.get()})
        if path in ("/api/recall-log", "/api/core", "/api/conflicts"):
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            if path == "/api/recall-log":
                return self._json(200, {"ok": True,
                                        "items": _recall_payload(None, limit)})
            if path == "/api/core":
                return self._json(200, {"ok": True,
                                        "items": _core_payload(None, limit)})
            return self._json(200, {"ok": True,
                                    "items": _conflicts_payload(None, limit)})
        if GUEST and path in ("/life", "/life.html", "/core.html"):
            # 访客站不暴露数字生命后台；本地主人（非 --guest）仍可看完整状态。
            return self._redirect("/")
        if path in ("/life", "/life.html", "/core.html"):
            return self._html("core.html")
        if path in ("/api/life/state", "/api/core/state"):
            if GUEST:
                return self._json(403, {"ok": False, "error": "请登录后查看数字生命"})
            import core_api
            return self._json(200, core_api.state(admin=True))
        if path in ("/vendor", "/vendor/"):
            return self._json(404, {"ok": False, "error": "没有这个页面"})
        if GUEST and path in ("/graph.json", "/settings.html", "/README.md"):
            return self._json(403, {"ok": False, "error": "请登录后查看"})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        # 登录接口不要求这个头；其余写接口统一要求 X-Viewer。
        # 跨站表单/简单请求带不了自定义头，跨域请求又会先 OPTIONS（本服务不返回 CORS），
        # 因此这比只靠 SameSite=Lax 更能挡住同站子域/代理场景下的 CSRF。
        if path != "/api/login" and self.headers.get("X-Viewer") != "1":
            self._drain_body()
            return self._json(403, {"ok": False, "error": "缺少本地请求标记"})
        if ACCOUNTS:
            if path == "/api/login":
                return self._login()
            if path == "/api/accounts":
                qq = self._qq()
                if not qq:
                    return self._json(401, {"ok": False, "error": "请先登录"})
                if not self._admin(qq):
                    return self._json(403, {"ok": False, "error": "只有管理员能管账号"})
                return self._accounts_post(qq)
            if path not in ("/api/memory", "/api/settings", "/api/rebuild", "/api/core",
                        "/api/conflicts", "/api/relation", "/api/life/action"):
                return self._json(404, {"ok": False, "error": "没有这个接口"})
            qq = self._qq()
            if not qq:
                return self._json(401, {"ok": False, "error": "请先登录"})
            if path == "/api/life/action":
                body = self._read_json()
                if body is None:
                    return
                if not self._admin(qq):
                    return self._json(403, {"ok": False, "error": "只有管理员能改"})
                import core_api
                return self._json(200, core_api.action_admin(body))
            if path == "/api/settings" and not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能改开关"})
            if path == "/api/relation" and not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能改关系"})
            # 普通账号也能改，但只能改自己的记忆（下面按 owner 校验）；管理员不限制
            self._owner = None if self._admin(qq) else qq
        else:
            if path == "/api/life/action":
                # 只读站点：把请求体读掉再拒，避免 keep-alive 连接里留下残包
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 0:
                        self.rfile.read(min(n, _MAX_BODY))
                except ValueError:
                    pass
                return self._json(403, {"ok": False, "error": "只读模式：这里只能看，不能改"})
            if path not in ("/api/memory", "/api/settings", "/api/rebuild", "/api/core",
                        "/api/conflicts", "/api/relation"):
                return self._json(404, {"ok": False, "error": "没有这个接口"})
            if READONLY:
                return self._json(403, {"ok": False, "error": "只读模式：这里只能看，不能改"})
            if self.headers.get("X-Viewer") != "1":
                return self._json(403, {"ok": False, "error": "缺少本地标记"})
            self._owner = None

        if path == "/api/conflicts":
            body = self._read_json()
            if body is None:
                return
            try:
                cid = int(body.get("id") or 0)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "id 不合法"})
            res = _conflict_action(cid, str(body.get("op") or ""), self._owner)
            if res.get("ok"):
                res["rebuild"] = _rebuild()
            code = 200 if res.get("ok") else (403 if res.get("forbidden") else 404)
            return self._json(code, res)

        if path == "/api/core":
            body = self._read_json()
            if body is None:
                return
            try:
                qid = int(body.get("id") or 0)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "id 不合法"})
            res = _core_action(qid, str(body.get("op") or ""), self._owner)
            if res.get("ok"):
                res["rebuild"] = _rebuild()
            code = 200 if res.get("ok") else (403 if res.get("forbidden") else 404)
            return self._json(code, res)

        if path == "/api/rebuild":
            # 重建全站图是管理员操作；普通账号只能改自己的记忆，不能操作全局。
            if ACCOUNTS and not self._admin(qq):
                return self._json(403, {"ok": False, "error": "只有管理员能同步全站图"})
            # 不用等 30 分钟的计划任务：页面上点「同步」直接重建
            # 这个接口没有 JSON 体；把可能带来的请求体读掉，保持 keep-alive 干净
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 0:
                    self.rfile.read(min(n, _MAX_BODY))
            except ValueError:
                pass
            actor = qq if ACCOUNTS else self.client_address[0]
            now = time.time()
            last = _REBUILD_LAST.get(actor, 0.0)
            if now - last < _REBUILD_GAP:
                return self._json(429, {"ok": False,
                                        "error": f"刚同步过，{int(_REBUILD_GAP - (now - last)) + 1} 秒后再试"})
            _REBUILD_LAST[actor] = now
            if len(_REBUILD_LAST) > 5000:
                _REBUILD_LAST.clear()
            res = _rebuild()
            return self._json(200 if res.get("ok") else 500, res)

        body = self._read_json()
        if body is None:
            return

        if path == "/api/relation":
            try:
                res = mv.set_relation_override(body.get("qq"), str(body.get("ring") or ""))
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            rebuilt = _rebuild()
            res["rebuild"] = rebuilt
            res["nodes"] = rebuilt.get("nodes", 0)
            return self._json(200 if rebuilt.get("ok") else 500, res)

        if path == "/api/settings":
            try:
                saved = settings_store.save(body)
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"ok": False, "error": str(e)})
            return self._json(200, {"ok": True, "settings": saved})

        res = _apply(body, self._owner)
        if res.get("ok"):
            rebuilt = _rebuild()
            res["rebuild"] = rebuilt
            res["nodes"] = rebuilt.get("nodes", 0)
        code = 200 if res.get("ok") else (403 if res.get("forbidden") else 404)
        return self._json(code, res)

    def log_message(self, *_a):
        pass


def main() -> None:
    global READONLY, HOST, ACCOUNTS, GUEST
    settings_store.ensure_file()
    argv = sys.argv[1:]
    port = 8765
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            HOST = argv[i + 1]
        elif a.isdigit():
            port = int(a)
    ACCOUNTS = "--accounts" in argv or os.environ.get("VIEWER_ACCOUNTS") == "1"
    # 没有账号时，非账号入口一律按访客处理：能看所有球体，看不到具体内容。
    GUEST = ("--guest" in argv or "--readonly" in argv
             or os.environ.get("VIEWER_GUEST") == "1"
             or os.environ.get("VIEWER_READONLY") == "1")
    READONLY = GUEST or ACCOUNTS or "--readonly" in argv or os.environ.get("VIEWER_READONLY") == "1"
    if ACCOUNTS and not accounts_store.load():
        print(f"提示：{accounts_store.path_of()} 里还没有账号，"
              f"先跑 python accounts.py add <QQ号>")
    srv = ViewerHTTPServer((HOST, port), Handler)
    mode = ("（账号模式）" if ACCOUNTS else
            ("（访客模式，只能看球体概况）" if GUEST else ("（只读）" if READONLY else "")))
    print(f"记忆星云{mode}: http://{HOST}:{port}/   本地={DB}  共用={SHARED_DB}")
    if ACCOUNTS:
        print(f"账号文件={accounts_store.path_of()}  数据={GRAPH_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()