# -*- coding: utf-8 -*-
"""数字生命 · 网站接入层（P1/P2 数据只读 + 管理员写）。

给未来的“单开一个界面”用：
- GET  /api/core/state        任何人可读（不含密钥、不含聊天原文）
- POST /api/core/action       只有管理员可写，需请求头 X-Core-Token
- 管理员令牌在 config.json 的 core.web_token，第一次用自动生成

独立测试：python core_api.py --serve 6198
正式接入：在 viewer_server 的路由里转调 state() / action(body, token) 即可。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core_life as CL

try:
    import affect_core as AC
except Exception:  # noqa: BLE001
    AC = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
GROWTH = os.path.join(HERE, "data", "core", "growth.json")


def _cfg() -> dict:
    try:
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cfg(cfg: dict) -> None:
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    # 替换会继承目录 ACL；config 里可能有 web_token / 画图 token，写回后重新收紧。
    try:
        import accounts
        accounts._harden(CONFIG)
    except Exception:
        pass


def web_token() -> str:
    cfg = _cfg()
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    tok = str(core.get("web_token") or "")
    if not tok:
        tok = secrets.token_urlsafe(18)
        core["web_token"] = tok
        cfg["core"] = core
        _save_cfg(cfg)
    return tok


def state(admin: bool = True, qq: str = "") -> dict:
    cfg = _cfg()
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    h = CL.hormones()
    # 情绪层是激素的唯一真源：内分泌环和温度偏移都从 affect 取，避免同一张卡两套数
    aff = None
    try:
        aff = AC.state_view(admin=admin) if AC else None
    except Exception:
        aff = None
    if aff and aff.get("enabled"):
        ah, ab = aff["hormones"], aff["hormone_base"]
        h = {"dopamine": round(ah["da"] * 100, 2),
             "cortisol": round(ah["cor"] * 100, 2),
             "baseline": round(ab["da"] * 100, 2),
             "enabled": bool(h.get("enabled", True)),
             "source": "affect"}
        tdelta = round((ah["da"] - ah["cor"]) * 0.5, 3)
    else:
        tdelta = round(CL.temperature_delta(), 3)
    dreams = []
    if os.path.isdir(CL.DREAMS):
        for name in sorted(os.listdir(CL.DREAMS), reverse=True)[:7]:
            if name.endswith(".json"):
                d = CL._load(os.path.join(CL.DREAMS, name), {}) or {}
                items = d.get("items") or []
                if items:
                    dreams.append({"date": d.get("date", name[:-5]), "count": len(items),
                                   "last": items[-1].get("text", "")})
    out = {
        "ok": True,
        "ts": int(time.time()),
        "core": {"enable": bool(core.get("enable", True)),
                 "inject": core.get("inject") or {},
                 "owner": str(core.get("owner") or "")},
        "hormones": h,
        "temperature_delta": tdelta,
        "obsessions": [o for o in CL._items(CL.OBS) if o.get("status", "open") == "open"],
        "scars": CL._items(CL.SCARS),
        "growth": CL._growth(),
        "organs": CL._organs(),
        "dreams": dreams,
        "vault": [{"id": m.get("id"), "created_at": m.get("created_at")}
                  for m in CL.list_vaults()[:10]],
    }
    try:
        out["affect"] = aff if aff else {"enabled": False}
    except Exception as e:  # noqa: BLE001
        out["affect"] = {"enabled": False, "error": f"{type(e).__name__}: {e}"}
    if not admin:
        # 普通账号只看得到自己：别人的关系数值、梦、执念、伤痕、成长记录都不下发
        me = str(qq or "").strip()
        if aff and aff.get("enabled"):
            aff["relations"] = [r for r in aff.get("relations") or []
                                if str(r.get("id")) == me]
        out["obsessions"] = []
        out["scars"] = []
        out["growth"] = []
        out["organs"] = {}
        out["dreams"] = []
        out["vault"] = []
    if admin:
        # 情绪层开着时，「每个人的情绪」也从它推：心情是全局的，亲密度按人取。
        if aff and aff.get("enabled"):
            _d, _der = aff["dims"], aff["derived"]
            _v = round((_d["joy"] + _d["content"]) / 2.0 - _d["sad"], 2)
            _a = round(1.0 - float(_der.get("fatigue") or 0.0), 2)
            out["moods"] = [{"qq": r["id"], "valence": _v, "arousal": _a,
                             "affection": r["attachment"],
                             "updated_at": int(aff.get("ts") or time.time())}
                            for r in aff.get("relations") or []]
        else:
            out["moods"] = CL.moods(80)
        out["audit"] = CL.audit_lines(40)
    return out


def action_admin(payload: dict) -> dict:
    """站点已登录管理员调用：跳过令牌，只做业务。"""
    return _apply(payload)


def action(payload: dict, token: str) -> dict:
    if not token or token != web_token():
        return {"ok": False, "error": "forbidden"}
    return _apply(payload)


def _apply(payload: dict) -> dict:
    op = str(payload.get("op") or "")
    try:
        if op == "ping":
            return {"ok": True, "ts": int(time.time())}
        if op == "hormone_set":
            h = CL.hormones()
            key = {"dopamine": "dopamine", "cortisol": "cortisol",
                   "多巴胺": "dopamine", "皮质醇": "cortisol"}.get(str(payload.get("key")))
            if not key:
                return {"ok": False, "error": "bad key"}
            h[key] = max(0.0, min(100.0, float(payload.get("value", 50))))
            CL._hormones_save(h)
            return {"ok": True, "hormones": CL.hormones()}
        if op == "hormone_enable":
            h = CL.hormones()
            h["enabled"] = bool(payload.get("value"))
            CL._hormones_save(h)
            return {"ok": True, "hormones": CL.hormones()}
        if op == "hormone_reset":
            h = CL.hormones()
            h["dopamine"] = h["cortisol"] = CL.HORM_BASE
            CL._hormones_save(h)
            return {"ok": True, "hormones": CL.hormones()}
        if op == "inject":
            key = str(payload.get("key") or "")
            if key not in ("obsessions", "scars", "growth"):
                return {"ok": False, "error": "bad key"}
            cfg = _cfg()
            core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
            inj = core.get("inject") if isinstance(core.get("inject"), dict) else {}
            inj[key] = bool(payload.get("value"))
            core["inject"] = inj
            cfg["core"] = core
            _save_cfg(cfg)
            return {"ok": True, "inject": inj}
        if op == "obsession_add":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty"}
            CL._obs_cmd(f"加 {text} {float(payload.get('weight', 0.6))}")
            return {"ok": True, "obsessions": CL._items(CL.OBS)}
        if op == "obsession_del":
            CL._obs_cmd(f"删 {int(payload.get('id', 0))}")
            return {"ok": True, "obsessions": CL._items(CL.OBS)}
        if op == "scar_add":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty"}
            CL._scar_cmd(f"加 {text}")
            return {"ok": True, "scars": CL._items(CL.SCARS)}
        if op == "scar_del":
            CL._scar_cmd(f"解除 {int(payload.get('id', 0))}")
            return {"ok": True, "scars": CL._items(CL.SCARS)}
        if op == "growth_add":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty"}
            CL._growth_add(text, str(payload.get("kind") or "habit"), "approved", "web")
            return {"ok": True, "growth": CL._growth()}
        if op == "growth_mark":
            status = "approved" if str(payload.get("status")) == "approved" else "rejected"
            msg = CL._growth_mark(int(payload.get("id", 0)), status)
            return {"ok": True, "growth": CL._growth(), "msg": msg}
        if op == "growth_del":
            msg = CL._growth_del(int(payload.get("id", 0)))
            return {"ok": True, "growth": CL._growth(), "msg": msg}
        if op == "organ_mark":
            status = "enabled" if str(payload.get("status")) == "enabled" else "disabled"
            msg = CL._organ_mark(int(payload.get("id", 0)), status)
            return {"ok": True, "organs": CL._organs(), "msg": msg}
        if op == "organ_del":
            msg = CL._organ_del(int(payload.get("id", 0)))
            return {"ok": True, "organs": CL._organs(), "msg": msg}
        if op == "vault_export":
            man = CL.export("网页")
            return {"ok": True, "vault": CL.list_vaults()[:10], "id": man.get("id")}
        if op == "affect_tick":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            AC.tick()
            return {"ok": True, "affect": AC.state_view(admin=True)}
        if op == "affect_reset":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            res = AC.reset()
            res["affect"] = AC.state_view(admin=True)
            return res
        if op == "affect_set":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            res = AC.set_value(str(payload.get("group") or ""),
                               str(payload.get("key") or ""),
                               float(payload.get("value") or 0))
            res["affect"] = AC.state_view(admin=True)
            return res
        if op == "affect_rel_del":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            res = AC.clear_relation(str(payload.get("contact") or ""))
            res["affect"] = AC.state_view(admin=True)
            return res
        if op == "affect_event":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            res = AC.inject_event(str(payload.get("kind") or ""))
            res["affect"] = AC.state_view(admin=True)
            return res
        if op == "affect_conf":
            if not AC:
                return {"ok": False, "error": "affect module missing"}
            key = str(payload.get("key") or "")
            val = payload.get("value")
            if key == "enable":
                val = bool(val)
            elif key == "appraisal":
                val = str(val)
                if val not in ("auto", "keywords", "model"):
                    return {"ok": False, "error": "appraisal 只能是 auto / keywords / model"}
            else:
                return {"ok": False, "error": "只能改 enable / appraisal"}
            res = AC.set_conf(key, val)
            res["affect"] = AC.state_view(admin=True)
            return res
        return {"ok": False, "error": "unknown op"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


class Handler(BaseHTTPRequestHandler):
    # 不暴露 Python 版本；这是本机 127.0.0.1 服务，但不是给任意网页跨域读的。
    server_version = "NebulaCore"
    sys_version = ""

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 不再返回 ACAO；浏览器跨域预检失败，避免任意网站读取本机状态。
        self.send_response(204)
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/core", "/core.html", "/life", "/life.html"):
            self._file(os.path.join(HERE, "web", "core.html"), "text/html; charset=utf-8")
        elif path == "/vendor/pico.min.css":
            self._file(os.path.join(HERE, "web", "vendor", "pico.min.css"), "text/css; charset=utf-8")
        elif path in ("/api/core/state", "/api/core/state/", "/api/life/state", "/api/life/state/"):
            self._send(200, state())
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            self._send(404, {"ok": False, "error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/api/core/action", "/api/core/action/",
                                           "/api/life/action", "/api/life/action/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "bad json"})
            return
        tok = self.headers.get("X-Core-Token", "")
        res = action(payload, tok)
        self._send(200 if res.get("ok") else 403, res)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    port = 6198
    if "--serve" in sys.argv:
        i = sys.argv.index("--serve")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            port = int(sys.argv[i + 1])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"core api on http://127.0.0.1:{port}  token={web_token()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()