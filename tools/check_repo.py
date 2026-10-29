# -*- coding: utf-8 -*-
"""防悬空自检：文档里提到的文件/接口/脚本必须真的存在。

仓库最容易烂掉的地方不是代码，而是文档：写着 `python foo.py`，可 foo.py
早就改名了；写着 `GET /api/x`，路由却已经删了。这个脚本把这些引用全部
解析出来逐一核对，顺便拦住本机绝对路径、个人 QQ 号和密钥样式字符串。

用法：
    python tools/check_repo.py

退出码 0 = 全部通过；1 = 有悬空引用或敏感内容。
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXT = {".md", ".html", ".py", ".bat", ".json", ".css", ".txt", ".example"}
SKIP_DIRS = {".git", "vendor", "data", "personas", "docs/images", "__pycache__"}

# 运行期才生成的文件：文档可以提，但仓库里不该有
RUNTIME_PREFIXES = (
    "data/", "personas/", "web/graph.json", "graph.json", "accounts.json",
    "accounts.key", "config.json", "settings.json", "runtime/", "__pycache__/",
)
RUNTIME_SUFFIXES = (".db", ".db-wal", ".db-shm", ".log", ".pyc")

# 可选依赖：没有也能跑，代码里有 try/except
OPTIONAL_IMPORTS = {"companion_brain"}

# 文档里允许出现的“路由”，不要求是磁盘文件
ROUTES = {"/", "/login", "/logout", "/life", "/core.html", "/settings.html",
          "/favicon.ico"}

SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "疑似 API key"),
    (re.compile(r"656088512"), "个人 QQ 号"),
    (re.compile(r"大肥鱼"), "个人化命名"),
    (re.compile(r"tail22a805|\.ts\.net"), "个人 Tailscale 域名"),
]
ABS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\|/Users/|/home/")

ERRORS: list[str] = []


def fail(msg: str) -> None:
    ERRORS.append(msg)


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/")


def iter_text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if rel(os.path.join(base, d)) not in SKIP_DIRS
                   and d != ".git"]
        for name in files:
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                yield os.path.join(base, name)


def is_runtime(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./")
    return p.startswith(RUNTIME_PREFIXES) or p.endswith(RUNTIME_SUFFIXES)


def _basename_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "vendor", "__pycache__")]
        for name in files:
            index.setdefault(name, []).append(os.path.join(base, name))
    return index


BASE_INDEX: dict[str, list[str]] = {}


def resolve(token: str, md_dir: str) -> str | None:
    global BASE_INDEX
    token = token.strip().strip("`'\"()[]<>，。；：、")
    if not token or token.startswith(("-", ".", "/", "http", "#", "mailto:")):
        return None
    if any(ch in token for ch in "*?{}<>$%|()=:"):
        return None
    if token in ROUTES or token.startswith("/api/") or token.startswith("/vendor/"):
        return None
    if is_runtime(token):
        return None
    # 只核对「看起来像仓库内文件」的引用，纯概念词（text/fact/label）不算
    known_ext = re.search(r"\.(py|bat|ps1|md|html|css|json|txt|example|png)$", token)
    known_dir = re.match(r"(web|docs|tools|demo|tests|data|personas|vendor)/", token)
    if not known_ext and not known_dir:
        return None
    candidates = [os.path.join(ROOT, token), os.path.join(md_dir, token)]
    for c in candidates:
        if os.path.exists(c):
            return c
    # 文档里只写文件名时，允许按全仓库唯一同名文件解析
    if not BASE_INDEX:
        BASE_INDEX = _basename_index()
    hits = BASE_INDEX.get(os.path.basename(token), [])
    if len(hits) == 1:
        return hits[0]
    return ""


def check_markdown(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    md_dir = os.path.dirname(path)
    # 1) 行内代码里出现的文件名 / 相对路径
    for span in re.findall(r"`([^`\n]+)`", text):
        for token in span.split():
            if not (re.search(r"\.(py|bat|ps1|md|html|css|json|txt)$", token)
                    or "/" in token):
                continue
            got = resolve(token, md_dir)
            if got == "":
                fail(f"{rel(path)}: 引用了不存在的文件 `{token}`")
    # 2) 相对链接
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http", "#", "mailto:")):
            continue
        got = resolve(target.split("#")[0], md_dir)
        if got == "":
            fail(f"{rel(path)}: 链接指向不存在的文件 {target}")


def check_endpoints(path: str, server_src: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for ep in sorted(set(re.findall(r"/api/[A-Za-z0-9_\-/]+", text))):
        if ep.rstrip("/") not in server_src:
            fail(f"{rel(path)}: 文档里的接口 {ep} 在 viewer_server.py 里找不到")


def check_html(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    base = os.path.dirname(path)
    for value in re.findall(r'(?:src|href)="([^"]+)"', text):
        v = value.split("?")[0]
        if v.startswith(("http", "data:", "#", "mailto:", "/api/", "/login",
                         "/logout", "/life", "/core.html", "/settings.html")):
            continue
        if "." not in os.path.basename(v):
            continue          # 无扩展名 = 路由，不是静态文件
        v = v.lstrip("/")
        if os.path.exists(os.path.join(base, v)) or os.path.exists(os.path.join(ROOT, v)):
            continue
        fail(f"{rel(path)}: 页面引用了不存在的资源 {value}")


def check_python_imports() -> None:
    import importlib.util

    local = {os.path.splitext(n)[0] for n in os.listdir(ROOT)
             if n.endswith(".py")}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            src = open(path, encoding="utf-8").read()
            try:
                tree = ast.parse(src, feature_version=(3, 9))
            except SyntaxError as e:
                fail(f"{rel(path)}: 语法不符合 Python 3.9（第 {e.lineno} 行：{e.msg}）")
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module.split(".")[0]]
                for mod in names:
                    if mod in local or mod in OPTIONAL_IMPORTS:
                        continue
                    try:
                        found = importlib.util.find_spec(mod) is not None
                    except Exception:
                        found = False
                    if not found:
                        fail(f"{rel(path)}: 导入了仓库里没有的模块 `{mod}`")


def check_bat() -> None:
    for name in os.listdir(ROOT):
        if not name.endswith(".bat"):
            continue
        text = open(os.path.join(ROOT, name), encoding="utf-8", errors="replace").read()
        for ref in re.findall(r"[\w\-./\\]+\.(?:py|bat|ps1)", text):
            if not os.path.exists(os.path.join(ROOT, ref.replace("\\", "/"))):
                fail(f"{name}: 调用了不存在的脚本 {ref}")


def check_sensitive() -> None:
    for path in iter_text_files():
        name = rel(path)
        if name.startswith("docs/images/") or name == "tools/check_repo.py":
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        for rx, why in SECRET_PATTERNS:
            if rx.search(text):
                fail(f"{rel(path)}: 含有{why}，公开仓库前要删掉")
        if ABS_PATH.search(text):
            fail(f"{rel(path)}: 含有本机绝对路径（换机器就悬空），改成相对路径")
        if "TODO" in text and path.endswith(".md"):
            pass  # TODO 允许存在，不算悬空


def check_required() -> None:
    required = [
        "README.md", "LICENSE", ".gitignore", "requirements.txt",
        "viewer_server.py", "memory_view.py", "accounts.py", "settings_store.py",
        "core_api.py", "core_life.py",
        "web/index.html", "web/login.html", "web/settings.html", "web/core.html",
        "web/vendor/3d-force-graph.min.js", "web/vendor/3d-force-graph.min.js.gz",
        "demo/make_demo.py", "tools/check_repo.py", "tests/smoke_test.py",
        "docs/设计思路.md", "docs/接口.md", "docs/数据与部署.md",
        "docs/images/screenshot.png",
    ]
    for item in required:
        if not os.path.exists(os.path.join(ROOT, item)):
            fail(f"缺少必需文件 {item}")
    ignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    for pattern in ("accounts.json", "config.json", "*.db", "web/graph.json"):
        if pattern not in ignore:
            fail(f".gitignore 少了 `{pattern}`，运行数据可能被提交")


def main() -> int:
    if not os.path.isdir(os.path.join(ROOT, "web")):
        print(f"[X] 这里不是仓库根目录：{ROOT}")
        return 1
    server = os.path.join(ROOT, "viewer_server.py")
    server_src = open(server, encoding="utf-8").read()

    check_required()
    check_bat()
    check_python_imports()
    for path in iter_text_files():
        name = rel(path)
        if name.endswith(".md") and name != "web/vendor/README.md":
            check_markdown(path)
            check_endpoints(path, server_src)
        elif name.endswith(".html"):
            check_html(path)
    check_sensitive()

    if ERRORS:
        print(f"发现 {len(ERRORS)} 个问题：")
        for e in ERRORS:
            print("  [X] " + e)
        return 1
    print("通过：文档引用、接口、导入、脚本、敏感信息全部对得上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())