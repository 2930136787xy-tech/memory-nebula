# -*- coding: utf-8 -*-
"""数字生命 P0：管理员权限、审计、迁移舱（导出 / 导入 / 回滚 / 对比）。

只做三件事：
1. `/生命`：开关、状态、审计
2. `/迁移`：导出、列表、对比、导入（先预演再确认）、回滚
3. 给星云网页留地基：data/core/*.json 数据契约（本模块只读写，不渲染）

原则
- 只有 owner（config.core.owner，缺省取 admins[0]）在私聊能用；群里一律拒绝。
- 所有写操作追加 data/core/audit.log。
- 任何异常都在 bot 侧兜底，不影响聊天主流程。
- 迁移包不含任何密钥；导入前自动备份当前状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CORE = os.path.join(DATA, "core")
VAULT = os.path.join(DATA, "vault")
PERSONAS = os.path.join(HERE, "personas")
CONFIG = os.path.join(HERE, "config.json")
BRAIN = os.path.join(HERE, "brain.db")
AUDIT = os.path.join(CORE, "audit.log")

SCHEMA = 1
SECRET_KEYS = {"api_key", "key", "token", "password", "secret", "cloud_key",
               "access_token", "refresh_token", "app_secret"}
CORE_FILES = ("growth.json", "hormones.json", "obsessions.json", "scars.json", "emotions.json")

_pending: dict[str, dict] = {}


# ------------------------------------------------------------------ 基础

def _load(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _dump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _audit(cmd: str, arg: str, result: str) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmd": cmd,
                       "arg": str(arg)[:120], "result": str(result)[:120]},
                      ensure_ascii=False)
    try:
        os.makedirs(CORE, exist_ok=True)
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _cfg() -> dict:
    return _load(CONFIG, {}) or {}


def _owner() -> str:
    core = _cfg().get("core") or {}
    admins = _cfg().get("admins") or []
    return str(core.get("owner") or (admins[0] if admins else ""))


def _enabled() -> bool:
    return bool((_cfg().get("core") or {}).get("enable", True))


def _memories() -> list[dict]:
    """只读导出 brain.db 的记忆；库不在或结构不对就返回空。"""
    if not os.path.exists(BRAIN):
        return []
    out: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{BRAIN}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.execute(
                "SELECT scope, speaker_id, kind, content, importance, confidence,"
                " status, source, subject, pinned, audience, created_at, updated_at"
                " FROM memories")
            for r in cur.fetchall():
                out.append({"scope": str(r[0] or "default"), "speaker": str(r[1] or ""),
                            "kind": str(r[2] or "fact"), "content": str(r[3] or ""),
                            "importance": float(r[4] or 0.5), "confidence": float(r[5] or 0.8),
                            "status": str(r[6] or "active"), "source": str(r[7] or ""),
                            "subject": str(r[8] or ""), "pinned": int(r[9] or 0),
                            "audience": str(r[10] or "global"),
                            "created_at": float(r[11] or 0.0),
                            "updated_at": float(r[12] or r[11] or 0.0)})
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return out


# ------------------------------------------------------------------ 迁移舱

def list_vaults() -> list[dict]:
    if not os.path.isdir(VAULT):
        return []
    out = []
    for name in sorted(os.listdir(VAULT), reverse=True):
        man = _load(os.path.join(VAULT, name, "manifest.json"))
        if isinstance(man, dict) and man.get("id"):
            out.append(man)
    return out


def _vault_dir(vid: str) -> str:
    vid = os.path.basename(str(vid).strip())
    root = os.path.join(VAULT, vid)
    if not vid or not os.path.isdir(root):
        raise FileNotFoundError(vid)
    return root


def vault_zip(vid: str) -> str:
    root = _vault_dir(vid)
    path = os.path.join(VAULT, os.path.basename(root) + ".zip")
    if not os.path.exists(path):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _, names in os.walk(root):
                for n in names:
                    p = os.path.join(dirpath, n)
                    z.write(p, os.path.relpath(p, root))
    return path


def export(remark: str = "") -> dict:
    os.makedirs(VAULT, exist_ok=True)
    vid = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in str(remark) if ch.isalnum() or ch in "-_")[:20]
    if safe:
        vid += "-" + safe
    root = os.path.join(VAULT, vid)
    if os.path.isdir(root):
        root = os.path.join(VAULT, vid + "-" + str(int(time.time() % 1000)))
        vid = os.path.basename(root)
    os.makedirs(root)
    counts = {"personas": 0, "memories": 0, "core": 0}

    if os.path.isdir(PERSONAS):
        shutil.copytree(PERSONAS, os.path.join(root, "personas"))
        counts["personas"] = sum(len(fs) for _, _, fs in os.walk(os.path.join(root, "personas")))

    core_dir = os.path.join(root, "core")
    os.makedirs(core_dir)
    for name in CORE_FILES:
        src = os.path.join(CORE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(core_dir, name))
            counts["core"] += 1

    mems = _memories()
    with open(os.path.join(root, "memories.jsonl"), "w", encoding="utf-8") as f:
        for m in mems:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    counts["memories"] = len(mems)

    cfg = _cfg()

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()
                    if not any(s in str(k).lower() for s in ("key", "token", "secret", "password"))}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o

    _dump(os.path.join(root, "config.subset.json"), strip(cfg))

    files = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if rel == "manifest.json":
                continue
            files[rel] = {"sha256": _sha(p), "size": os.path.getsize(p)}

    man = {"schema": SCHEMA, "id": vid, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "remark": str(remark or ""), "files": files, "counts": counts,
           "bot": {"model": str(cfg.get("model") or "")}}
    _dump(os.path.join(root, "manifest.json"), man)
    _audit("迁移导出", vid, f"人格{counts['personas']} 记忆{counts['memories']} core{counts['core']}")
    return man


def _verify(root: str, man: dict) -> list[str]:
    bad = []
    for rel, meta in (man.get("files") or {}).items():
        p = os.path.join(root, rel.replace("/", os.sep))
        try:
            if not os.path.exists(p) or _sha(p) != meta.get("sha256"):
                bad.append(rel)
        except OSError:
            bad.append(rel)
    return bad


def preview(vid: str) -> tuple[str, bool]:
    root = _vault_dir(vid)
    man = _load(os.path.join(root, "manifest.json")) or {}
    bad = _verify(root, man)
    mems = 0
    mp = os.path.join(root, "memories.jsonl")
    if os.path.exists(mp):
        with open(mp, "r", encoding="utf-8") as f:
            mems = sum(1 for line in f if line.strip())
    cur_p = sum(len(fs) for _, _, fs in os.walk(PERSONAS)) if os.path.isdir(PERSONAS) else 0
    lines = [f"【迁移导入预演】{vid}",
             f"创建 {man.get('created_at', '?')} · 备注 {man.get('remark') or '无'}",
             f"人格文件 {man.get('counts', {}).get('personas', 0)} 个（当前 {cur_p} 个，同名覆盖）",
             f"记忆 {mems} 条（当前 {len(_memories())} 条，按 内容+说话人 去重后追加）",
             f"core 数据 {man.get('counts', {}).get('core', 0)} 个文件（覆盖当前）"]
    if bad:
        lines.append(f"!! 校验失败 {len(bad)} 个：{', '.join(bad[:3])}")
        lines.append("已拒绝，未改动任何文件。")
        return "\n".join(lines), False
    lines.append(f"确认后 60 秒内发：/迁移 确认 {vid}")
    return "\n".join(lines), True


def apply(vid: str, mode: str = "replace") -> int:
    root = _vault_dir(vid)
    man = _load(os.path.join(root, "manifest.json")) or {}
    bad = _verify(root, man)
    if bad:
        raise ValueError("校验失败：" + ", ".join(bad[:3]))
    export("pre-import")          # 导入前自动备份当前状态
    if os.path.isdir(os.path.join(root, "personas")):
        os.makedirs(PERSONAS, exist_ok=True)
        shutil.copytree(os.path.join(root, "personas"), PERSONAS, dirs_exist_ok=True)
    for name in CORE_FILES:
        src = os.path.join(root, "core", name)
        if os.path.exists(src):
            os.makedirs(CORE, exist_ok=True)
            shutil.copy2(src, os.path.join(CORE, name))

    added = 0
    mp = os.path.join(root, "memories.jsonl")
    if os.path.exists(mp) and os.path.exists(BRAIN):
        existing = {(m["scope"], m["speaker"], m["content"]) for m in _memories()}
        con = sqlite3.connect(BRAIN, timeout=10)
        try:
            with open(mp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = json.loads(line)
                    key = (m.get("scope", "default"), m.get("speaker", ""), m.get("content", ""))
                    if key in existing:
                        continue
                    con.execute(
                        "INSERT INTO memories(scope,speaker_id,kind,content,importance,confidence,"
                        "status,source,subject,pinned,audience,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m.get("scope", "default"), m.get("speaker", ""), m.get("kind", "fact"),
                         m.get("content", ""), float(m.get("importance", 0.5)),
                         float(m.get("confidence", 0.8)), m.get("status", "active"),
                         m.get("source", ""), m.get("subject", ""), int(m.get("pinned", 0)),
                         m.get("audience", "global"), float(m.get("created_at", 0)),
                         float(m.get("updated_at", 0))))
                    existing.add(key)
                    added += 1
            con.commit()
        finally:
            con.close()
    _audit("迁移导入", vid, f"人格+core 已覆盖，记忆 +{added}")
    return added


def compare(vid: str) -> str:
    man = _load(os.path.join(_vault_dir(vid), "manifest.json")) or {}
    cur_p = sum(len(fs) for _, _, fs in os.walk(PERSONAS)) if os.path.isdir(PERSONAS) else 0
    return "\n".join([f"【迁移对比】{vid}",
                       f"人格文件：包 {man.get('counts', {}).get('personas', 0)} / 现在 {cur_p}",
                       f"记忆：包 {man.get('counts', {}).get('memories', 0)} / 现在 {len(_memories())}",
                       f"模型：包 {man.get('bot', {}).get('model', '?')} / 现在 {_cfg().get('model', '?')}"])


# ------------------------------------------------------------------ P2 执念 / 疤痕 / 梦境

OBS = os.path.join(CORE, "obsessions.json")
SCARS = os.path.join(CORE, "scars.json")
DREAMS = os.path.join(CORE, "dreams")


def _items(path: str) -> list[dict]:
    data = _load(path, [])
    return data if isinstance(data, list) else []


def _save_items(path: str, items: list[dict]) -> None:
    _dump(path, items)


def _next_id(items: list[dict]) -> int:
    return max([int(i.get("id") or 0) for i in items] or [0]) + 1


def _list_obs() -> str:
    items = [o for o in _items(OBS) if o.get("status", "open") == "open"]
    if not items:
        return "执念池是空的。"
    lines = ["【执念池】"]
    for o in sorted(items, key=lambda x: -float(x.get("weight") or 0)):
        lines.append(f"#{o.get('id')} {o.get('text','')}（权重 {float(o.get('weight') or 0):.1f}）")
    return "\n".join(lines)


def _list_scars() -> str:
    items = _items(SCARS)
    if not items:
        return "身上还没有疤。"
    lines = ["【疤痕】"]
    for s in items:
        lines.append(f"#{s.get('id')} {s.get('text','')}（锁定）")
    return "\n".join(lines)


def _set_flag(key: str, on: bool) -> str:
    cfg = _cfg()
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    inj = core.get("inject") if isinstance(core.get("inject"), dict) else {}
    inj[key] = bool(on)
    core["inject"] = inj
    cfg["core"] = core
    _dump(CONFIG, cfg)
    _audit("注入开关", f"{key}={'on' if on else 'off'}", "ok")
    return f"{key} 注入已{'开启' if on else '关闭'}。"


def _obs_cmd(rest: str) -> str:
    parts = rest.split()
    sub = parts[0] if parts else ""
    if sub in ("", "列表"):
        return _list_obs()
    if sub == "加" and len(parts) >= 2:
        text = " ".join(parts[1:]).strip()
        w = 0.6
        mm = text.rsplit(" ", 1)
        if len(mm) == 2:
            try:
                w = float(mm[1]); text = mm[0]
            except ValueError:
                pass
        items = _items(OBS)
        items.append({"id": _next_id(items), "text": text[:80], "weight": round(max(0.1, min(1.0, w)), 2),
                      "status": "open", "created_at": int(time.time()), "due_at": 0})
        _save_items(OBS, items)
        _audit("执念加", text[:40], "ok")
        return f"已放进执念池 #{items[-1]['id']}。"
    if sub == "删" and len(parts) >= 2 and parts[1].isdigit():
        items = _items(OBS)
        left = [o for o in items if int(o.get("id") or 0) != int(parts[1])]
        if len(left) == len(items):
            return "没有这个执念。"
        _save_items(OBS, left)
        _audit("执念删", parts[1], "ok")
        return f"已删执念 #{parts[1]}。"
    if sub == "注入" and len(parts) >= 2:
        return _set_flag("obsessions", parts[1] in ("on", "开", "1"))
    return "用法：/生命 执念 [加 文本 权重|删 id|注入 on|off]"


def _scar_cmd(rest: str) -> str:
    parts = rest.split()
    sub = parts[0] if parts else ""
    if sub in ("", "列表"):
        return _list_scars()
    if sub == "加" and len(parts) >= 2:
        text = " ".join(parts[1:]).strip()
        items = _items(SCARS)
        items.append({"id": _next_id(items), "text": text[:80], "weight": 1.0, "locked": True,
                      "created_at": int(time.time()), "source": "admin"})
        _save_items(SCARS, items)
        _audit("疤痕加", text[:40], "ok")
        return f"已固化疤痕 #{items[-1]['id']}（锁定，夜间不会清）。"
    if sub == "解除" and len(parts) >= 2 and parts[1].isdigit():
        items = _items(SCARS)
        left = [s for s in items if int(s.get("id") or 0) != int(parts[1])]
        if len(left) == len(items):
            return "没有这条疤。"
        _save_items(SCARS, left)
        _audit("疤痕解除", parts[1], "ok")
        return f"已解除疤痕 #{parts[1]}。"
    if sub == "注入" and len(parts) >= 2:
        return _set_flag("scars", parts[1] in ("on", "开", "1"))
    return "用法：/生命 疤痕 [加 文本|解除 id|注入 on|off]"


def _dream_append(text: str) -> None:
    day = time.strftime("%Y-%m-%d")
    os.makedirs(DREAMS, exist_ok=True)
    path = os.path.join(DREAMS, day + ".json")
    data = _load(path, {"date": day, "items": []}) or {"date": day, "items": []}
    data.setdefault("items", []).append({"ts": int(time.time()), "text": text})
    _dump(path, data)


def _dreams_text(n: int = 2) -> str:
    if not os.path.isdir(DREAMS):
        return "还没有梦。发 /生命 梦 现在 做一个。"
    out = []
    for name in sorted(os.listdir(DREAMS), reverse=True)[:n]:
        if not name.endswith(".json"):
            continue
        data = _load(os.path.join(DREAMS, name), {}) or {}
        for item in (data.get("items") or [])[-2:]:
            out.append(f"{str(item.get('ts',''))} {item.get('text','')}")
    return "【最近的梦】\n" + "\n".join(out) if out else "还没有梦。"


def dream_now(bot) -> str:
    if bot is None or not hasattr(bot, "chat"):
        return "机器人实例不在，这次做不了梦。"
    mems = [m.get("content", "") for m in _memories()][-8:]
    obs = [o.get("text", "") for o in _items(OBS) if o.get("status", "open") == "open"][-3:]
    sc = [s.get("text", "") for s in _items(SCARS)][-3:]
    ctx = "；".join([x for x in (obs + sc + mems) if x]) or "今天没什么特别的"
    msgs = [
        {"role": "system", "content": "你是一个住在 QQ 里的数字生命。用第一人称写一个 50~100 字的梦，"
                                       "像真的做梦，隐喻下面的记忆碎片；不要解释、不要列表、不要用「梦到」开头。"},
        {"role": "user", "content": "今天的碎片：" + ctx[:600]},
    ]
    try:
        text = (bot.chat(msgs) or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"做梦失败：{type(e).__name__}: {str(e)[:60]}"
    if not text:
        return "模型没返回，这次没做成梦。"
    _dream_append(text)
    _audit("做梦", text[:40], "ok")
    return "【梦】" + text


def _dreams_count() -> int:
    if not os.path.isdir(DREAMS):
        return 0
    n = 0
    for name in os.listdir(DREAMS):
        if name.endswith(".json"):
            n += len((_load(os.path.join(DREAMS, name), {}) or {}).get("items") or [])
    return n


def _dream_cmd(rest: str, bot=None) -> str:
    sub = rest.strip()
    if sub in ("", "最近"):
        return _dreams_text()
    if sub == "现在":
        return dream_now(bot)
    return "用法：/生命 梦 [最近|现在]"


# ------------------------------------------------------------------ P1 全局激素

HORM = os.path.join(CORE, "hormones.json")
HORM_BASE = 50.0
HORM_HALF_LIFE_H = 6.0
_PRAISE = ("好棒", "厉害", "喜欢", "爱你", "谢谢", "可爱", "靠谱", "真牛")
_INSULT = ("滚", "傻", "笨", "烦", "讨厌", "闭嘴", "废物", "恶心")


def _hormones() -> dict:
    h = _load(HORM, {}) or {}
    return {"dopamine": float(h.get("dopamine", HORM_BASE)),
            "cortisol": float(h.get("cortisol", HORM_BASE)),
            "baseline": float(h.get("baseline", HORM_BASE)),
            "enabled": bool(h.get("enabled", False)),
            "updated_at": float(h.get("updated_at", 0) or 0)}


def _hormones_save(h: dict) -> None:
    h = dict(h)
    h["updated_at"] = time.time()
    _dump(HORM, h)


def _decay(h: dict) -> dict:
    now = time.time()
    dt_h = max(0.0, (now - float(h.get("updated_at") or now)) / 3600.0)
    if dt_h > 0 and h.get("enabled"):
        f = 0.5 ** (dt_h / HORM_HALF_LIFE_H)
        base = float(h.get("baseline", HORM_BASE))
        for k in ("dopamine", "cortisol"):
            h[k] = base + (float(h[k]) - base) * f
    h["updated_at"] = now
    return h


def hormones() -> dict:
    h = _decay(_hormones())
    _hormones_save(h)
    return h


def hormone_event(kind: str, text: str = "") -> None:
    """interaction / praise / insult；没开内分泌就只更新时间。"""
    try:
        h = _decay(_hormones())
        if not h.get("enabled"):
            _hormones_save(h)
            return
        t = str(text or "")
        if kind == "interaction":
            if any(w in t for w in _PRAISE):
                kind = "praise"
            elif any(w in t for w in _INSULT):
                kind = "insult"
        dd, dc = {"interaction": (1.5, -0.5), "praise": (6.0, -2.0),
                  "insult": (-4.0, 7.0)}.get(kind, (0.0, 0.0))
        h["dopamine"] = max(0.0, min(100.0, float(h["dopamine"]) + dd))
        h["cortisol"] = max(0.0, min(100.0, float(h["cortisol"]) + dc))
        _hormones_save(h)
    except Exception:
        pass


def temperature_delta() -> float:
    try:
        h = hormones()
        if not h.get("enabled"):
            return 0.0
        d = (h["dopamine"] - 50.0) / 50.0 * 0.25 - (h["cortisol"] - 50.0) / 50.0 * 0.20
        return max(-0.20, min(0.25, d))
    except Exception:
        return 0.0


def _hormone_view() -> str:
    h = hormones()
    return (f"【激素】{'开' if h['enabled'] else '关'} · 多巴胺 {h['dopamine']:.0f} · "
            f"皮质醇 {h['cortisol']:.0f} · 基线 {h['baseline']:.0f} · "
            f"温度偏移 {temperature_delta():+.2f}")


def _hormone_cmd(rest: str) -> str:
    parts = rest.split()
    sub = parts[0] if parts else ""
    h = hormones()
    if sub in ("", "查看"):
        return _hormone_view()
    if sub == "重置":
        h["dopamine"] = h["cortisol"] = HORM_BASE
        _hormones_save(h)
        _audit("激素重置", "", "ok")
        return "激素已回到基线 50/50。"
    if sub == "设" and len(parts) >= 3:
        key = {"多巴胺": "dopamine", "皮质醇": "cortisol"}.get(parts[1])
        try:
            v = max(0.0, min(100.0, float(parts[2])))
        except ValueError:
            return "数值要 0~100。"
        if not key:
            return "只能设 多巴胺 或 皮质醇。"
        h[key] = v
        _hormones_save(h)
        _audit("激素设", f"{parts[1]}={v:.0f}", "ok")
        return _hormone_view()
    return "用法：/生命 激素 [查看|重置|设 多巴胺 70]"


# ------------------------------------------------------------------ P3 生长层

GROWTH = os.path.join(CORE, "growth.json")


def _growth() -> list:
    items = _load(GROWTH, [])
    return items if isinstance(items, list) else []


def _growth_save(items) -> None:
    _dump(GROWTH, list(items)[-500:])


def _growth_add(text, kind="habit", status="proposed", source="") -> dict:
    items = _growth()
    it = {"id": max([int(i.get("id") or 0) for i in items] + [0]) + 1,
          "text": str(text).strip()[:80], "kind": kind, "status": status,
          "weight": 0.6, "created_at": int(time.time()), "source": str(source)[:40]}
    items.append(it)
    _growth_save(items)
    return it


def growth_approved() -> list:
    return [i for i in _growth() if i.get("status") == "approved"][:3]


def _growth_mark(gid: int, status: str) -> str:
    items = _growth()
    for it in items:
        if int(it.get("id") or 0) == gid:
            it["status"] = status
            it["updated_at"] = int(time.time())
            _growth_save(items)
            _audit("生长" + ("采纳" if status == "approved" else "驳回"), f"#{gid}", "ok")
            return f"已{'采纳' if status == 'approved' else '驳回'} #{gid}"
    return "没有这条。"


def _growth_del(gid: int) -> str:
    items = _growth()
    left = [i for i in items if int(i.get("id") or 0) != gid]
    if len(left) == len(items):
        return "没有这条。"
    _growth_save(left)
    _audit("生长删除", f"#{gid}", "ok")
    return f"已删 #{gid}"


def _growth_cmd(rest: str, bot=None) -> str:
    parts = rest.split()
    sub = parts[0] if parts else ""
    if sub in ("", "列表"):
        items = _growth()
        if not items:
            return "还没有长出什么。"
        lines = ["【生长层】"]
        for it in items[-12:]:
            mark = {"approved": "✓", "proposed": "?", "rejected": "×"}.get(str(it.get("status")), "·")
            lines.append(f"{mark} #{it.get('id')} {str(it.get('text', ''))[:40]}")
        return "\n".join(lines)
    if sub in ("过", "批准") and len(parts) >= 2:
        try:
            return _growth_mark(int(parts[1].lstrip("#")), "approved")
        except ValueError:
            return "要 id，比如 /生命 生长 过 3"
    if sub in ("拒", "驳回") and len(parts) >= 2:
        try:
            return _growth_mark(int(parts[1].lstrip("#")), "rejected")
        except ValueError:
            return "要 id，比如 /生命 生长 拒 3"
    if sub == "删" and len(parts) >= 2:
        try:
            return _growth_del(int(parts[1].lstrip("#")))
        except ValueError:
            return "要 id。"
    if sub == "注入" and len(parts) >= 2:
        cfg = _cfg()
        core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
        inj = core.get("inject") if isinstance(core.get("inject"), dict) else {}
        inj["growth"] = parts[1] in ("on", "开", "1")
        core["inject"] = inj
        cfg["core"] = core
        _dump(CONFIG, cfg)
        _audit("生长注入", parts[1], "ok")
        return "生长注入已" + ("开" if inj["growth"] else "关") + "。"
    if sub == "提炼":
        return _growth_propose(bot)
    return "用法：/生命 生长 [列表|过 3|拒 3|删 3|提炼|注入 on|off]"


def _growth_propose(bot=None) -> str:
    if bot is None:
        return "提炼要在机器人进程里跑（私聊我）。"
    try:
        mems = [m for m in _memories() if m.get("status") == "active"]
        mems.sort(key=lambda m: float(m.get("updated_at") or 0), reverse=True)
        ctx = "\n".join(f"- {str(m.get('content', ''))[:120]}" for m in mems[:12])
        cur = "\n".join(f"- {i.get('text')}" for i in _growth() if i.get("status") == "approved")
        msgs = [
            {"role": "system",
             "content": "你在帮一只住在 QQ 里的数字生命总结她慢慢长出来的习惯、口头禅或偏好。"
                        "只写有证据的，不要编。输出 JSON 数组，每项 {\"text\":\"不超过30字\","
                        "\"kind\":\"habit|phrase|prefer\"}，最多 3 条，不要解释。"},
            {"role": "user",
             "content": "最近被记住的事：\n" + (ctx or "（没有）")
                        + "\n\n已经长出来的：\n" + (cur or "（没有）")},
        ]
        raw = str(bot.chat(msgs) or "")
        i, j = raw.find("["), raw.rfind("]")
        arr = json.loads(raw[i:j + 1]) if 0 <= i < j else []
        added = []
        have = {str(x.get("text", "")) for x in _growth()}
        for row in arr[:3]:
            text = str((row or {}).get("text") if isinstance(row, dict) else row).strip()
            if text and text not in have:
                _growth_add(text, str(row.get("kind") or "habit") if isinstance(row, dict) else "habit",
                            "proposed", "提炼")
                added.append(text)
        if not added:
            return "这次没提炼出新的。"
        return "提了 %d 条，等管理员过：\n" % len(added) + "\n".join("· " + t for t in added)
    except Exception as e:  # noqa: BLE001
        return f"提炼失败：{type(e).__name__}"


# ------------------------------------------------------------------ 夜间自动

SCHED = os.path.join(CORE, "schedule.json")


def _sched() -> dict:
    d = _load(SCHED, {})
    return d if isinstance(d, dict) else {}


def _sched_save(d) -> None:
    _dump(SCHED, d)


def nightly(bot=None) -> None:
    """夜里 2~6 点各做一次：自动做梦、自动提炼生长（每天最多一次）。"""
    hour = time.localtime().tm_hour
    if bot is None or not (2 <= hour < 6):
        return
    cfg = _cfg()
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    if not core.get("enable", True):
        return
    today = time.strftime("%Y-%m-%d")
    st = _sched()
    dream_cfg = core.get("dream") if isinstance(core.get("dream"), dict) else {}
    if dream_cfg.get("auto", True) and st.get("dream") != today:
        try:
            dream_now(bot)
            st["dream"] = today
            _sched_save(st)
            _audit("自动做梦", today, "ok")
        except Exception:  # noqa: BLE001
            pass
    growth_cfg = core.get("growth") if isinstance(core.get("growth"), dict) else {}
    if growth_cfg.get("auto", True) and st.get("growth") != today:
        try:
            _growth_propose(bot)
            st["growth"] = today
            _sched_save(st)
            _audit("自动提炼生长", today, "ok")
        except Exception:  # noqa: BLE001
            pass
    if 5 <= hour < 6 and st.get("backup") != today:
        try:
            backup_now()
            maintenance()
            st["backup"] = today
            _sched_save(st)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ P4 器官（只提案，不执行）

ORGANS = os.path.join(CORE, "organs.json")


def _organs() -> list:
    items = _load(ORGANS, [])
    return items if isinstance(items, list) else []


def _organs_save(items) -> None:
    _dump(ORGANS, list(items)[-200:])


def _organ_add(title, why="", status="proposed", source="") -> dict:
    items = _organs()
    it = {"id": max([int(i.get("id") or 0) for i in items] + [0]) + 1,
          "title": str(title).strip()[:40], "why": str(why).strip()[:200],
          "status": status, "created_at": int(time.time()), "source": str(source)[:20]}
    items.append(it)
    _organs_save(items)
    return it


def _organ_mark(oid: int, status: str) -> str:
    items = _organs()
    for it in items:
        if int(it.get("id") or 0) == oid:
            it["status"] = status
            it["updated_at"] = int(time.time())
            _organs_save(items)
            _audit("器官" + status, f"#{oid}", "ok")
            return f"器官 #{oid} 已{ {'enabled':'启用','disabled':'停用'}.get(status, status) }"
    return "没有这个器官。"


def _organ_del(oid: int) -> str:
    items = _organs()
    left = [i for i in items if int(i.get("id") or 0) != oid]
    if len(left) == len(items):
        return "没有这个器官。"
    _organs_save(left)
    return f"已删器官 #{oid}"


def _organ_cmd(rest: str, bot=None) -> str:
    parts = rest.split()
    sub = parts[0] if parts else ""
    if sub in ("", "列表"):
        items = _organs()
        if not items:
            return "还没有器官提案。发 /生命 器官 想 让她想一个。"
        lines = ["【器官】"]
        for it in items[-10:]:
            mark = {"enabled": "✓", "disabled": "×"}.get(str(it.get("status")), "?")
            lines.append(f"{mark} #{it.get('id')} {it.get('title', '')}")
        return "\n".join(lines)
    if sub == "想":
        return _organ_propose(bot)
    if sub in ("启用", "停用", "删") and len(parts) >= 2:
        try:
            oid = int(parts[1].lstrip("#"))
        except ValueError:
            return "要 id，比如 /生命 器官 启用 1"
        if sub == "删":
            return _organ_del(oid)
        return _organ_mark(oid, "enabled" if sub == "启用" else "disabled")
    return "用法：/生命 器官 [列表|想|启用 1|停用 1|删 1]"


def _organ_propose(bot=None) -> str:
    if bot is None:
        return "想器官要在机器人进程里跑（私聊我）。"
    cur = "\n".join(f"- {i.get('title')}" for i in _organs()) or "（没有）"
    msgs = [
        {"role": "system", "content": "你是一个住在 QQ 里的数字生命。"
            "提一个你想长出来的小能力（比如“能记住天气”“会画简笔画”），只提案，不会自动执行。"
            "输出一行 JSON：{\"title\":\"不超过16字\",\"why\":\"一句话为什么\"}，不要解释。"},
        {"role": "user", "content": "已有的器官提案：\n" + cur},
    ]
    try:
        raw = str(bot.chat(msgs) or "")
        i, j = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[i:j + 1]) if 0 <= i < j else {}
        title = str(obj.get("title") or "").strip()
        if not title:
            return "这次没想出新的。"
        it = _organ_add(title, str(obj.get("why") or ""), "proposed", "想")
        _audit("器官提案", title[:30], "ok")
        return f"提了一个器官 #{it['id']}：{title}\n{obj.get('why', '')}"
    except Exception as e:  # noqa: BLE001
        return f"想器官失败：{type(e).__name__}"


# ------------------------------------------------------------------ 管理视图：情绪 / 审计 / 备份

def moods(limit: int = 60) -> list:
    """按人情绪（brain.db 的 emotion_state），只读。"""
    if not os.path.exists(BRAIN):
        return []
    try:
        conn = sqlite3.connect(f"file:{BRAIN}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute(
                "SELECT speaker_id,valence,arousal,affection,updated_at"
                " FROM emotion_state ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [{"qq": str(r[0] or ""), "valence": round(float(r[1] or 0), 2),
             "arousal": round(float(r[2] or 0), 2), "affection": round(float(r[3] or 0), 2),
             "updated_at": int(r[4] or 0)} for r in rows]


def audit_lines(n: int = 40) -> list:
    if not os.path.exists(AUDIT):
        return []
    try:
        with open(AUDIT, "r", encoding="utf-8") as f:
            rows = f.readlines()[-max(1, min(int(n), 200)):]
    except OSError:
        return []
    out = []
    for ln in rows:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


BACKUPS = os.path.join(DATA, "backups")


def backup_now() -> str:
    os.makedirs(BACKUPS, exist_ok=True)
    path = os.path.join(BACKUPS, "auto-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(CORE):
            p = os.path.join(CORE, name)
            if os.path.isfile(p):
                z.write(p, "core/" + name)
        if os.path.exists(BRAIN):
            z.write(BRAIN, "brain.db")
        cfg = _cfg()
        cfg = {k: v for k, v in cfg.items()
               if not any(s in str(k).lower() for s in ("key", "token", "secret", "password"))}
        z.writestr("config.subset.json", json.dumps(cfg, ensure_ascii=False, indent=1))
    keep = 7
    files = sorted(f for f in os.listdir(BACKUPS) if f.startswith("auto-") and f.endswith(".zip"))
    for old in files[:-keep]:
        try:
            os.remove(os.path.join(BACKUPS, old))
        except OSError:
            pass
    _audit("自动备份", os.path.basename(path), "ok")
    return path


def maintenance() -> dict:
    removed = {"dreams": 0, "growth": 0, "vault": 0}
    cutoff = time.time() - 180 * 86400
    if os.path.isdir(DREAMS):
        for name in os.listdir(DREAMS):
            if not name.endswith(".json"):
                continue
            try:
                day = time.mktime(time.strptime(name[:-5], "%Y-%m-%d"))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(os.path.join(DREAMS, name))
                    removed["dreams"] += 1
                except OSError:
                    pass
    items = _growth()
    keep = [i for i in items if not (i.get("status") == "rejected"
                                     and float(i.get("created_at") or 0) < time.time() - 30 * 86400)]
    if len(keep) != len(items):
        removed["growth"] = len(items) - len(keep)
        _growth_save(keep)
    if os.path.isdir(VAULT):
        dirs = sorted(d for d in os.listdir(VAULT) if os.path.isdir(os.path.join(VAULT, d)))
        for old in dirs[:-20]:
            shutil.rmtree(os.path.join(VAULT, old), ignore_errors=True)
            removed["vault"] += 1
    if any(removed.values()):
        _audit("清理", json.dumps(removed, ensure_ascii=False), "ok")
    return removed


def prompt_block() -> str:
    """给 system_prompt 用：只有管理员打开注入、且 core 开着时才追加。"""
    cfg = _cfg()
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    if not core.get("enable", True):
        return ""
    flags = core.get("inject") if isinstance(core.get("inject"), dict) else {}
    parts = []
    if flags.get("obsessions"):
        items = sorted([o for o in _items(OBS) if o.get("status", "open") == "open"],
                       key=lambda x: -float(x.get("weight") or 0))[:2]
        if items:
            parts.append("【你最近惦记着】" + "；".join(str(o.get("text", ""))[:40] for o in items))
    if flags.get("scars"):
        items = sorted(_items(SCARS), key=lambda x: -float(x.get("weight") or 0))[:2]
        if items:
            parts.append("【你身上的疤】" + "；".join(str(s.get("text", ""))[:40] for s in items)
                         + "（已经过去了，别反复提）")
    if flags.get("growth"):
        g = growth_approved()
        if g:
            parts.append("【你慢慢长出来的】" + "；".join(str(i.get("text", ""))[:30] for i in g))
    try:
        h = hormones()
        if h.get("enabled"):
            tone = "偏兴奋" if h["dopamine"] > 60 else ("偏低落" if h["dopamine"] < 40 else "平稳")
            parts.append(f"【身体状态】多巴胺 {h['dopamine']:.0f}，皮质醇 {h['cortisol']:.0f}，"
                         f"整体{tone}；别报具体数字。")
    except Exception:
        pass
    return "\n".join(parts)


# ------------------------------------------------------------------ 状态与命令

def status() -> str:
    core = _cfg().get("core") or {}
    vs = list_vaults()
    lines = ["【数字生命 · core】",
             f"开关 {'开' if core.get('enable', True) else '关'} · 仅私聊 · owner {_owner()}",
             f"迁移舱 {len(vs)} 个包" + (f" · 最近 {vs[0]['id']}" if vs else " · 还没有包"),
             f"执念 {len([o for o in _items(OBS) if o.get('status', 'open') == 'open'])} 条 · "
             f"疤痕 {len(_items(SCARS))} 条 · 梦 {_dreams_count()} 条 · 生长 {len([g for g in _growth() if g.get('status') == 'approved'])} 条",
             _hormone_view(),
             "数据：data/core（激素/生长/执念/疤痕）、data/vault（存档）"]
    if os.path.exists(AUDIT):
        tail = []
        try:
            with open(AUDIT, "r", encoding="utf-8") as f:
                for ln in f.readlines()[-3:]:
                    d = json.loads(ln)
                    tail.append(f"{d['ts'][11:16]} {d['cmd']} {d['result']}")
        except (OSError, ValueError):
            tail = []
        if tail:
            lines.append("最近：" + "；".join(tail))
    return "\n".join(lines)


def _set_enable(on: bool) -> str:
    cfg = _cfg()
    core = cfg.get("core")
    if not isinstance(core, dict):
        core = {}
    core["enable"] = bool(on)
    cfg["core"] = core
    _dump(CONFIG, cfg)
    _audit("生命开关", "开" if on else "关", "ok")
    return f"core 已{'开启' if on else '关闭'}。"


def _audit_text(n: int = 5) -> str:
    if not os.path.exists(AUDIT):
        return "还没有审计记录。"
    try:
        with open(AUDIT, "r", encoding="utf-8") as f:
            rows = f.readlines()[-max(1, min(n, 20)):]
    except OSError:
        return "审计日志读不出来。"
    out = []
    for ln in rows:
        try:
            d = json.loads(ln)
            out.append(f"{d['ts'][5:16]} {d['cmd']} {d['arg']} → {d['result']}")
        except ValueError:
            continue
    return "【审计】\n" + "\n".join(out)


def _vault_list_text() -> str:
    vs = list_vaults()
    if not vs:
        return "迁移舱里还没有包。发 /迁移 导出 生成一个。"
    lines = [f"【迁移舱】{len(vs)} 个包"]
    for man in vs[:10]:
        lines.append(f"{man['id']} · 人格{man.get('counts', {}).get('personas', 0)}"
                     f" 记忆{man.get('counts', {}).get('memories', 0)}"
                     f" · {str(man.get('created_at', ''))[5:16]}")
    if len(vs) > 10:
        lines.append(f"……还有 {len(vs) - 10} 个")
    return "\n".join(lines)


def handle(body: str, user_id, msg_type: str, admin: bool = False, bot=None) -> str | None:
    """bot 侧唯一入口：返回要回复的文本；返回 None 表示不回复。"""
    body = (body or "").strip()
    if not _enabled():
        return "core 未开启。"
    owner = _owner()
    if not owner or str(user_id) != owner:
        return None
    if msg_type != "private":
        return "数字生命模块只在私聊里能用。"

    try:
        if body.startswith("生命"):
            rest = body[len("生命"):].strip()
            if rest in ("", "状态"):
                return status()
            if rest in ("开", "启用"):
                return _set_enable(True)
            if rest in ("关", "关闭", "全关"):
                return _set_enable(False)
            if rest.startswith("审计"):
                arg = rest[len("审计"):].strip()
                return _audit_text(int(arg) if arg.isdigit() else 5)
            if rest in ("帮助", "help", "?"):
                return ("【数字生命】\n/生命 状态|开|关|全关\n/生命 审计 [n]\n"
                        "/生命 执念 [加 文本 权重|删 id|注入 on|off]\n"
                        "/生命 疤痕 [加 文本|解除 id|注入 on|off]\n"
                        "/生命 梦 [最近|现在]\n"
                        "/生命 生长 [列表|过 3|拒 3|提炼|注入 on]\n"
                        "/生命 器官 [列表|想|启用 1|停用 1|删 1]\n"
                        "/迁移 导出 [备注]|列表|对比 <id>|导入 <id>|确认 <id>|取消|回滚 <id>")
            if rest.startswith("内分泌"):
                sub = rest[len("内分泌"):].strip()
                h = hormones()
                h["enabled"] = sub in ("on", "开", "1")
                _hormones_save(h)
                _audit("内分泌", sub, "ok")
                return _hormone_view()
            if rest.startswith("激素"):
                return _hormone_cmd(rest[len("激素"):].strip())
            if rest.startswith("器官"):
                return _organ_cmd(rest[len("器官"):].strip(), bot)
            if rest.startswith("生长"):
                return _growth_cmd(rest[len("生长"):].strip(), bot)
            if rest.startswith("执念"):
                return _obs_cmd(rest[len("执念"):].strip())
            if rest.startswith("疤痕"):
                return _scar_cmd(rest[len("疤痕"):].strip())
            if rest.startswith("梦"):
                return _dream_cmd(rest[len("梦"):].strip(), bot)
            return "不认识的子命令，发 /生命 帮助。"

        if body.startswith("迁移"):
            rest = body[len("迁移"):].strip()
            parts = rest.split()
            sub = parts[0] if parts else ""
            if sub == "导出":
                man = export(" ".join(parts[1:]))
                return (f"已导出 {man['id']}\n人格 {man['counts']['personas']} 个文件 · "
                        f"记忆 {man['counts']['memories']} 条 · core {man['counts']['core']} 个\n"
                        "包在 data/vault/ 下，不含密钥。")
            if sub == "列表":
                return _vault_list_text()
            if sub == "对比" and len(parts) >= 2:
                return compare(parts[1])
            if sub == "导入" and len(parts) >= 2:
                vid = parts[1]
                text, ok = preview(vid)
                if ok:
                    _pending[str(user_id)] = {"vid": vid, "exp": time.time() + 60}
                return text
            if sub == "确认" and len(parts) >= 2:
                p = _pending.get(str(user_id)) or {}
                if not p or p.get("vid") != parts[1] or time.time() > float(p.get("exp", 0)):
                    return "没有待确认的导入，或已超时。重新发 /迁移 导入 <id>。"
                _pending.pop(str(user_id), None)
                added = apply(parts[1])
                return f"导入完成：{parts[1]}（记忆新增 {added} 条；导入前已自动备份）。"
            if sub == "取消":
                _pending.pop(str(user_id), None)
                return "已取消待确认的导入。"
            if sub == "回滚" and len(parts) >= 2:
                vid = parts[1]
                text, ok = preview(vid)
                if not ok:
                    return text
                _pending[str(user_id)] = {"vid": vid, "exp": time.time() + 60}
                return text.replace("【迁移导入预演】", "【回滚预演】")
            return "不认识的子命令，发 /迁移 列表 或 /生命 帮助。"
    except FileNotFoundError:
        return "找不到这个迁移包，先发 /迁移 列表 看 id。"
    except Exception as e:  # noqa: BLE001
        return f"迁移舱出错：{type(e).__name__}: {str(e)[:80]}"
    return None


# ------------------------------------------------------------------ 自检

def selftest() -> bool:
    ok: list[tuple[str, bool]] = []
    made = []
    try:
        man = export("selftest")
        made.append(man["id"])
        root = os.path.join(VAULT, man["id"])
        ok.append(("导出生成 manifest", os.path.exists(os.path.join(root, "manifest.json"))))
        ok.append(("全部文件校验和一致", not _verify(root, man)))
        cfgsub = _load(os.path.join(root, "config.subset.json"), {})
        blob = json.dumps(cfgsub, ensure_ascii=False).lower()
        ok.append(("导出不含密钥字段", '"api_key"' not in blob and '"token"' not in blob))
        text, good = preview(man["id"])
        ok.append(("导入预演通过", good and "确认" in text))
        ok.append(("非管理员静默忽略", handle("迁移 列表", "10001", "private") is None))
        ok.append(("群聊被拒绝", "私聊" in (handle("迁移 列表", _owner(), "group") or "")))
        ok.append(("状态可读", "数字生命" in (handle("生命", _owner(), "private") or "")))
        ok.append(("审计有记录", os.path.exists(AUDIT)))
    finally:
        for vid in made:
            shutil.rmtree(os.path.join(VAULT, vid), ignore_errors=True)
    for name, passed in ok:
        print(("PASS " if passed else "FAIL ") + name)
    print("RESULT", "PASS" if all(p for _, p in ok) else "FAIL")
    return all(p for _, p in ok)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    print(status())