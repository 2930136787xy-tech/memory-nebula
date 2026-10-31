# -*- coding: utf-8 -*-
"""全量数据备份 + 网盘上传。

一次备份 = 一个 zip：

    nebula-20261030-214000-daily.zip
      manifest.json          清单（每个文件的路径/大小/sha256）
      files/config.json …    配置、账号、状态、人格、日记、画、迁移包
      db/brain.db            伴生记忆（用 sqlite backup API 取的副本，不会半截）
      db/bridge.db           共用记忆（QQ → 抖音那座桥）

不打包的东西：日志、TTS 语音、浏览器配置、以及 backups/ 自己（避免套娃）。

网盘走 WebDAV（坚果云 / Nextcloud / 群晖 / alist 都支持），也能填一条自定义
上传命令（rclone、bypy 之类）作为兜底。

命令行::

    python data_backup.py make          # 只做本地包
    python data_backup.py run           # 做包 + 上传
    python data_backup.py upload <zip>  # 传一个已有的包
    python data_backup.py test          # 测网盘连通性
    python data_backup.py list          # 看最近的包
    python data_backup.py scrub <目录或zip> # 清理历史包里的明文凭证
    python data_backup.py restore <zip> # 解到 restore-<时间>/ 供核对

凭证会脱敏：config.json / data/state.json / data/facts.json 里的 api_key、token、
password 等字段在包里被替换成 ***已脱敏***，原值另存在本机 backups/secrets/ 下
（不进网盘目录）。从备份恢复后，需要照 secrets 文件把密钥填回去，否则模型连不上。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

DEFAULTS = {
    "enable": True,
    "daily_at": "03:40",
    "keep": 20,
    "dir": os.path.join("backups", "full"),
    "include_big": False,
    "webdav": {"enable": False, "url": "", "user": "", "pass": "", "dir": "nebula"},
    "upload_cmd": "",
}

# 单文件和目录（相对 HERE）
FILES = [
    "config.json", "accounts.json", "accounts.key", "settings.json",
    "gsv.json", "rvc.json", "netease.json", "quark-sync.json",
    "companion.config.json", "web/graph.json", "runtime/sessions.json",
    "data/state.json", "data/memory.json", "data/facts.json",
    "data/rings.json", "data/history.json", "data/allow.json",
    "data/vectors-cache.json",
]
DIRS = ["personas", "data/core", "data/diary", "data/draw", "data/vault", "data/archive"]
BIG_DIRS = ["data/voice", "data/edge-profile"]


def _read_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cfg() -> dict:
    raw = dict(DEFAULTS)
    raw["webdav"] = dict(DEFAULTS["webdav"])
    got = _read_config().get("backup")
    if isinstance(got, dict):
        for k, v in got.items():
            if k == "webdav" and isinstance(v, dict):
                raw["webdav"].update(v)
            else:
                raw[k] = v
    return raw


def backup_dir(c: dict | None = None) -> str:
    c = c or cfg()
    d = str(c.get("dir") or DEFAULTS["dir"])
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def _shared_db() -> str:
    c = cfg()
    explicit = str(c.get("shared_db") or "").strip()
    if explicit:
        return explicit
    try:
        import shared_memory
        return str(shared_memory.DEFAULTS.get("db") or "")
    except Exception:  # noqa: BLE001
        return ""


def _dbs() -> list[tuple[str, str]]:
    out = []
    brain = os.path.join(HERE, "brain.db")
    if os.path.exists(brain):
        out.append((brain, "db/brain.db"))
    shared = _shared_db()
    if shared and os.path.exists(shared):
        out.append((shared, "db/bridge.db"))
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 密钥脱敏
# 备份目录直接就是网盘同步目录（D:\夸克网盘\...），包里带明文 Key 等于上传到云。
# 所以打包时把凭证类字段换成占位符，原值另外写在本机 backups/secrets/ 下。
# 按「文件名」匹配，不按完整路径：data/core/backup-*/state.json 这类历史副本
# 也带着同一批明文 Key，只匹配固定路径会漏（2026-10-31 实测漏了一个）。
REDACT_TARGETS = {"config.json", "state.json", "facts.json"}
SECRET_FIELDS = {
    "apikey", "key", "secret", "secretkey", "token", "accesstoken",
    "password", "passwd", "pass", "cookie", "authorization",
    "accesskey", "cloudkey", "appid", "appsecret",
}
MASK = "***已脱敏***"


def _is_secret_field(name: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", str(name).lower()) in SECRET_FIELDS


def _redact(obj, path: str, found: dict) -> object:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and v.strip() and _is_secret_field(k):
                out[k] = MASK
                found[p] = v
            else:
                out[k] = _redact(v, p, found)
        return out
    if isinstance(obj, list):
        return [_redact(v, f"{path}[{i}]", found) for i, v in enumerate(obj)]
    return obj


def _redact_json(data: bytes) -> tuple[bytes, dict]:
    """把 JSON 里的凭证字段换成占位符；解析失败就原样返回。"""
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return data, {}
    found: dict = {}
    cleaned = _redact(parsed, "", found)
    if not found:
        return data, {}
    return json.dumps(cleaned, ensure_ascii=False, indent=1).encode("utf-8"), found


def _write_local_secrets(stamp: str, found: dict[str, dict]) -> str:
    """把被脱敏掉的原值写在本机（不进网盘目录），恢复时照着填回去。"""
    if not found:
        return ""
    d = os.path.join(HERE, "backups", "secrets")
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f"secrets-{stamp}.json")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "note": "备份包里的凭证已被替换为 " + MASK + "；恢复后请从这里取回原值。",
                       "files": found}, f, ensure_ascii=False, indent=1)
        return dst
    except OSError:
        return ""


def _db_copy(path: str, tmpdir: str) -> str:
    dst = os.path.join(tmpdir, "db-" + os.path.basename(path))
    src = sqlite3.connect(path, timeout=5)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def _walk(root: str, rel: str) -> list[tuple[str, str]]:
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            arc = os.path.join(rel, os.path.relpath(full, root)).replace("\\", "/")
            out.append((full, arc))
    return out


def make(reason: str = "manual", c: dict | None = None) -> dict:
    """打一个全量包，返回 {ok, path, files, size, sha256}。"""
    c = c or cfg()
    out_dir = backup_dir(c)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in str(reason) if ch.isalnum() or ch in "-_") or "manual"
    dst = os.path.join(out_dir, f"nebula-{stamp}-{safe}.zip")
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(out_dir, f"nebula-{stamp}-{safe}-{i}.zip")
        i += 1

    tmpdir = tempfile.mkdtemp(prefix="nebula-bak-")
    entries: list[tuple[str, str]] = []   # (源文件, 包内路径)
    try:
        for rel in FILES:
            p = os.path.join(HERE, rel.replace("/", os.sep))
            if os.path.isfile(p):
                entries.append((p, f"files/{rel}"))
        for rel in DIRS + (BIG_DIRS if c.get("include_big") else []):
            root = os.path.join(HERE, rel.replace("/", os.sep))
            if os.path.isdir(root):
                entries.extend(_walk(root, f"files/{rel}"))
        for src, arc in _dbs():
            entries.append((_db_copy(src, tmpdir), arc))

        manifest = {"version": 2, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": safe, "files": [], "dbs": [], "total": 0,
                    "redacted": []}
        total = 0
        secrets_found: dict[str, dict] = {}
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for src, arc in entries:
                with open(src, "rb") as f:
                    data = f.read()
                rel = arc[len("files/"):] if arc.startswith("files/") else ""
                if os.path.basename(rel) in REDACT_TARGETS:
                    data, found = _redact_json(data)
                    if found:
                        secrets_found[rel] = found
                        manifest["redacted"].append(rel)
                z.writestr(arc, data)
                item = {"name": arc, "size": len(data), "sha256": _sha(data)}
                (manifest["dbs"] if arc.startswith("db/") else manifest["files"]).append(item)
                total += len(data)
            manifest["total"] = total
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    secrets_path = _write_local_secrets(stamp, secrets_found)
    size = os.path.getsize(dst)
    digest = _sha(open(dst, "rb").read())
    _prune(c)
    return {"ok": True, "path": dst, "name": os.path.basename(dst), "bytes": size,
            "sha256": digest, "entries": len(entries) + 1,
            "redacted": manifest["redacted"], "secrets_local": secrets_path}


def _prune_secrets(keep: int = 20) -> int:
    """backups/secrets/ 里的密钥文件跟着备份一起清，只留最近 keep 个。

    这些文件不在网盘目录里，但一天一份会无限长；删掉旧的不会影响已有 zip
    （zip 里本来就是脱敏占位符）。
    """
    d = os.path.join(HERE, "backups", "secrets")
    try:
        files = [os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith(".json")]
        files.sort(key=os.path.getmtime)          # 按时间留最新的，不信文件名
    except OSError:
        return 0
    names = files
    removed = 0
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(name)
            removed += 1
        except OSError:
            pass
    return removed


def _prune(c: dict | None = None) -> None:
    c = c or cfg()
    try:
        keep = max(1, int(c.get("keep") or 20))
        d = backup_dir(c)
        names = sorted(n for n in os.listdir(d) if n.startswith("nebula-") and n.endswith(".zip"))
    except OSError:
        return
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    _prune_secrets(keep)


# -- 网盘 -------------------------------------------------------------------

def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")


def _opener(url: str):
    """本机地址不走系统代理（很多代理只认 CONNECT，PUT 会 405）。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _request(method: str, url: str, data: bytes | None = None,
             headers: dict | None = None, auth: str = "", timeout: int = 90):
    """返回 (状态码, 响应体, 头部)；连不上时状态码 0。"""
    req = urllib.request.Request(url, data=data, method=method)
    if auth:
        req.add_header("Authorization", auth)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, body, dict(e.headers or {})
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, str(e).encode("utf-8", "replace"), {}


def _mkcol(url: str, auth: str) -> int:
    code, _body, _hd = _request("MKCOL", url, auth=auth)
    return code


def _ensure_remote_dir(base: str, folder: str, auth: str, timeout: int = 90) -> None:
    """逐级建目录；多数服务器对已存在的目录返回 405。"""
    root = base.rstrip("/")
    for part in [p for p in folder.replace("\\", "/").split("/") if p]:
        root = root + "/" + urllib.parse.quote(part)
        code = _mkcol(root + "/", auth)
        if code not in (200, 201, 204, 301, 405):
            raise RuntimeError(f"建目录失败 {part}: HTTP {code}")


def webdav_put(webdav: dict, path: str) -> dict:
    base = str(webdav.get("url") or "").strip()
    if not base:
        return {"ok": False, "error": "没填 WebDAV 地址"}
    auth = _basic(str(webdav.get("user") or ""), str(webdav.get("pass") or ""))
    folder = str(webdav.get("dir") or "nebula").strip("/")
    name = os.path.basename(path)
    try:
        _ensure_remote_dir(base, folder, auth)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    url = base.rstrip("/") + "/" + urllib.parse.quote(folder) + "/" + urllib.parse.quote(name)
    data = open(path, "rb").read()
    code, body, _hd = _request("PUT", url, data=data,
                          headers={"Content-Type": "application/zip"}, auth=auth)
    if code not in (200, 201, 204):
        return {"ok": False, "error": f"上传失败 HTTP {code}: "
                                      f"{body[:200].decode('utf-8', 'replace')}"}
    # 尽量核对大小；有的网盘不支持 HEAD，就只认 2xx
    hcode, _hb, hd = _request("HEAD", url, auth=auth)
    remote = hd.get("Content-Length") if hcode == 200 else None
    ok = remote is None or int(remote) == len(data)
    return {"ok": ok, "url": url, "bytes": len(data),
            "remote_bytes": remote, "verified": remote is not None}


def upload(path: str, c: dict | None = None) -> dict:
    c = c or cfg()
    wd = c.get("webdav") or {}
    if wd.get("enable") and wd.get("url"):
        res = webdav_put(wd, path)
        res["via"] = "webdav"
        return res
    cmd = str(c.get("upload_cmd") or "").strip()
    if cmd:
        line = cmd.replace("{file}", path).replace("{name}", os.path.basename(path))
        try:
            p = subprocess.run(line, shell=True, capture_output=True, text=True, timeout=1800)
            return {"ok": p.returncode == 0, "via": "cmd", "cmd": line,
                    "out": (p.stdout or "")[-800:], "err": (p.stderr or "")[-800:]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "via": "cmd", "error": f"{type(e).__name__}: {e}"}
    return {"ok": False, "skipped": "没配置网盘（backup.webdav 或 backup.upload_cmd）"}


def test_webdav(c: dict | None = None) -> dict:
    c = c or cfg()
    wd = c.get("webdav") or {}
    if not wd.get("url"):
        return {"ok": False, "error": "没填 WebDAV 地址"}
    auth = _basic(str(wd.get("user") or ""), str(wd.get("pass") or ""))
    folder = str(wd.get("dir") or "nebula").strip("/")
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
    tmp.write(b"nebula webdav probe " + time.strftime("%Y-%m-%d %H:%M:%S").encode())
    tmp.close()
    try:
        res = webdav_put(wd, tmp.name)
        if res.get("ok"):
            url = res.get("url") or ""
            code, _b, _h = _request("DELETE", url, auth=auth)
            res["deleted"] = code in (200, 204, 404)
        return res
    finally:
        os.unlink(tmp.name)


def run(reason: str = "daily", c: dict | None = None) -> dict:
    c = c or cfg()
    made = make(reason, c)
    up = upload(made["path"], c)
    return {"ok": bool(made.get("ok")) and (up.get("ok") or bool(up.get("skipped"))),
            "made": made, "upload": up}


def recent(limit: int = 10, c: dict | None = None) -> list[dict]:
    c = c or cfg()
    d = backup_dir(c)
    try:
        names = sorted((n for n in os.listdir(d) if n.endswith(".zip")), reverse=True)
    except OSError:
        return []
    out = []
    for n in names[:limit]:
        p = os.path.join(d, n)
        st = os.stat(p)
        out.append({"name": n, "path": p, "kb": round(st.st_size / 1024, 1),
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def restore(zip_path: str, to: str = "") -> str:
    """解到独立目录（默认 restore-<时间>/），不直接覆盖现用数据。"""
    dst = to or os.path.join(HERE, "restore-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dst)
    return dst


def scrub_zip(zip_path: str) -> dict:
    """把现有备份包里的凭证就地换成占位符（用于清理历史包）。

    隐私清单里的 sha256 会跟着重算，manifest 标记改过哪些文件；
    包里原本就没有凭证的，直接跳过不改。
    """
    tmp = zip_path + ".scrub-tmp"
    changed: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zin:
            names = zin.namelist()
            if "manifest.json" not in names:
                return {"ok": False, "reason": "不是本系统的备份包"}
            manifest = json.loads(zin.read("manifest.json").decode("utf-8"))
            data = {n: zin.read(n) for n in names}
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        return {"ok": False, "reason": f"读不动：{e}"}

    for name in list(data):
        rel = name[len("files/"):] if name.startswith("files/") else ""
        if os.path.basename(rel) not in REDACT_TARGETS:
            continue
        cleaned, found = _redact_json(data[name])
        if found:
            data[name] = cleaned
            changed.append(rel)

    if not changed:
        return {"ok": True, "changed": [], "reason": "本来就没有明文凭证"}

    # 重算清单里的 sha256 / 大小
    for item in manifest.get("files", []) + manifest.get("dbs", []):
        name = item.get("name")
        if name in data:
            item["sha256"] = _sha(data[name])
            item["size"] = len(data[name])
    manifest["redacted"] = sorted(set(manifest.get("redacted", []) + changed))
    manifest["scrubbed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")

    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for name, blob in data.items():
                z.writestr(name, blob)
        os.replace(tmp, zip_path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "reason": f"写不动：{e}"}
    return {"ok": True, "changed": changed}


def _main(argv: list[str]) -> int:
    c = cfg()
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        rows = recent(20, c)
        if not rows:
            print("还没有全量备份")
        for r in rows:
            print(f"{r['at']}  {r['kb']:>9} KB  {r['name']}")
        return 0
    if cmd == "make":
        reason = argv[2] if len(argv) > 2 else "manual"
        res = make(reason, c)
        print(f"已生成 {res['path']}  {res['bytes']/1024:.0f} KB  {res['entries']} 项")
        print(f"sha256 {res['sha256']}")
        return 0
    if cmd == "run":
        res = run(argv[2] if len(argv) > 2 else "manual", c)
        made, up = res["made"], res["upload"]
        print(f"本地：{made['path']}  {made['bytes']/1024:.0f} KB")
        print("网盘：" + json.dumps(up, ensure_ascii=False))
        return 0 if res["ok"] else 1
    if cmd == "upload":
        if len(argv) < 3:
            print("用法：python data_backup.py upload <zip>")
            return 2
        print(json.dumps(upload(argv[2], c), ensure_ascii=False))
        return 0
    if cmd == "test":
        print(json.dumps(test_webdav(c), ensure_ascii=False))
        return 0
    if cmd == "restore":
        if len(argv) < 3:
            print("用法：python data_backup.py restore <zip> [目标目录]")
            return 2
        print("已解开到：" + restore(argv[2], argv[3] if len(argv) > 3 else ""))
        print("核对无误后，停 bot 再把文件拷回去。")
        return 0
    if cmd == "scrub":
        target = argv[2] if len(argv) > 2 else backup_dir(c)
        zips = ([target] if target.lower().endswith(".zip")
                else [os.path.join(target, n) for n in sorted(os.listdir(target))
                      if n.lower().endswith(".zip")]) if os.path.isdir(target) or target.lower().endswith(".zip") else []
        if not zips:
            print("没有找到 zip 备份。")
            return 2
        bad = 0
        for p in zips:
            res = scrub_zip(p)
            mark = "已清理" if res.get("changed") else "无需处理"
            if not res.get("ok"):
                mark = "失败"
                bad += 1
            print(f"{mark}  {os.path.basename(p)}  {res.get('changed') or res.get('reason', '')}")
        return 0 if bad == 0 else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))