# -*- coding: utf-8 -*-
"""冒烟测试：真的把服务起起来，走一遍页面和几个接口。

前提：已经跑过 `python demo/make_demo.py`（会生成 web/graph.json）。
测试用 8811 端口，不碰 8765/8766/8767。

    python tests/smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("SMOKE_PORT", "8811"))
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(path: str, payload: dict, marker: bool = True) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if marker:
        headers["X-Viewer"] = "1"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    graph = os.path.join(ROOT, "web", "graph.json")
    if not os.path.exists(graph):
        print("还没生成图：先跑 `python demo/make_demo.py` 或把你自己的数据放好。")
        return 2

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "viewer_server.py", str(PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    checks: list[tuple[str, bool, str]] = []
    try:
        for _ in range(50):
            try:
                code, _ = get("/api/mode")
                if code == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print("服务没起来：\n" + out)
                return 1
            time.sleep(0.2)
        else:
            print("服务 10 秒内没有响应")
            return 1

        code, body = get("/")
        checks.append(("首页 200", code == 200, str(code)))
        checks.append(("首页标题", "记忆星云" in body.decode("utf-8", "ignore"), ""))

        code, body = get("/api/graph")
        data = json.loads(body or b"{}")
        checks.append(("graph 接口", code == 200 and data.get("nodes"),
                       f"{code} nodes={len(data.get('nodes') or [])}"))
        checks.append(("图里有边", bool(data.get("links")), str(len(data.get("links") or []))))

        code, body = get("/api/mode")
        mode = json.loads(body or b"{}")
        checks.append(("mode 接口", code == 200 and mode.get("ok") is True, str(code)))

        code, body = get("/vendor/pico.min.css")
        checks.append(("静态资源", code == 200 and len(body) > 1000, str(len(body))))

        code, body = get("/settings.html")
        checks.append(("开关页", code == 200, str(code)))

        code, body = post("/api/settings", {"viewer": {"semantic_edges": True}})
        checks.append(("写接口 + 本地标记", code == 200, str(code)))

        code, body = post("/api/settings", {}, marker=False)
        checks.append(("无标记应拒绝", code in (403, 400), str(code)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    bad = [c for c in checks if not c[1]]
    for name, ok, extra in checks:
        print(("  [OK] " if ok else "  [X]  ") + name + (f"  ({extra})" if extra else ""))
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())