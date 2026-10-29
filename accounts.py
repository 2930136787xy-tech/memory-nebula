# -*- coding: utf-8 -*-
"""记忆星云的账号文件：QQ 号当账号。

密码同时存两份：PBKDF2 哈希用来登录校验；一份用本机密钥加密的副本只给管理员查看
（``accounts.key`` 和 ``accounts.json`` 放在一起，别单独拷走 json）。

用法（在仓库根目录下）::

    python accounts.py add 10001                  # 生成一个 12 位密码并打印一次
    python accounts.py add 10001 --password 自己定的密码
    python accounts.py add 10001 --password xxx --admin   # 管理员
    python accounts.py admin 10001                 # 事后设为管理员（--off 取消）
    python accounts.py passwd 10001               # 重新生成密码（旧密码立即失效）
    python accounts.py init                       # 给每个有人格档案的 QQ 各建一个
    python accounts.py list                       # 看有哪些账号
    python accounts.py show                       # 管理员用：连密码一起看
    python accounts.py remove 10001               # 删除账号

文件默认是仓库根目录的 ``accounts.json``（在 web 目录之外，不会被静态服务读到）；
测试/换位置用环境变量 ``NEBULA_ACCOUNTS``（密钥文件 ``NEBULA_ACCOUNTS_KEY``）。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.environ.get("NEBULA_ACCOUNTS") or os.path.join(HERE, "accounts.json")
ITER = 200_000
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # 去掉 i l o 0 1，念出来不歧义
KEY_NAME = "accounts.key"


def path_of(path: str | None = None) -> str:
    return path or os.environ.get("NEBULA_ACCOUNTS") or DEFAULT_PATH


def _empty() -> dict:
    return {"version": 1, "accounts": {}}


def load(path: str | None = None) -> dict[str, dict]:
    """读账号，返回 {qq: {salt, hash, iter, nick, created}}；坏了就当空。"""
    p = path_of(path)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    acc = data.get("accounts") if isinstance(data, dict) else None
    return acc if isinstance(acc, dict) else {}


def _harden(path: str) -> None:
    """账号文件和密钥只给当前用户读：Windows 默认会让 Users/Authenticated Users
    继承读权限，多用户电脑上别人可以直接拷走 json+key 解出密码。"""
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return
        user = os.environ.get("USERNAME") or ""
        domain = os.environ.get("USERDOMAIN") or ""
        who = f"{domain}\\{user}" if domain else user
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/remove:g", "*S-1-5-11", "*S-1-5-32-545",
             "/grant:r", f"{who}:(F)", "*S-1-5-18:(F)"],
            capture_output=True, timeout=10, check=False)
    except Exception:
        pass


def save(accounts: dict[str, dict], path: str | None = None) -> str:
    p = path_of(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    data = {"version": 1, "accounts": accounts}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    _harden(p)
    return p


def _derive(password: str, salt: str, iterations: int = ITER) -> str:
    raw = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return raw.hex()


def verify(password: str, rec: dict) -> bool:
    try:
        salt = str(rec.get("salt") or "")
        want = str(rec.get("hash") or "")
        if not salt or not want:
            return False
        got = _derive(password, salt, int(rec.get("iter") or ITER))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def _key_path(path: str | None = None) -> str:
    return (os.environ.get("NEBULA_ACCOUNTS_KEY")
            or os.path.join(os.path.dirname(path_of(path)) or ".", KEY_NAME))


def _key(path: str | None = None) -> bytes:
    """本机密钥：第一次用时生成 32 字节，存在 accounts.key（仅本用户可读）。"""
    p = _key_path(path)
    try:
        with open(p, encoding="ascii") as f:
            raw = f.read().strip()
        if len(raw) == 64:
            _harden(p)
            return bytes.fromhex(raw)
    except OSError:
        pass
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex() + "\n")
    _harden(p)
    return key


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, b"enc" + nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(password: str, path: str | None = None) -> str:
    """把密码加密成一段可逆文本（HMAC-SHA256 计数器流 + 认证标签），只给管理员看。"""
    data = password.encode("utf-8")
    key = _key(path)
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).hexdigest()[:32]
    return f"v1:{nonce.hex()}:{ct.hex()}:{tag}"


def unseal(blob: str, path: str | None = None) -> str:
    try:
        ver, nonce_h, ct_h, tag = str(blob).split(":")
        if ver != "v1":
            return ""
        nonce = bytes.fromhex(nonce_h)
        ct = bytes.fromhex(ct_h)
        key = _key(path)
        want = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(want, tag):
            return ""
        return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)))).decode("utf-8")
    except Exception:
        return ""


def reveal(rec: dict, path: str | None = None) -> str:
    """管理员查看密码；老账号没有加密副本时返回空串。"""
    return unseal(str(rec.get("enc") or ""), path)


def issue_password(n: int = 12) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def nick_of(qq: str) -> str:
    """从人格 meta.json 取昵称，只用来在账号列表里好认，不参与鉴权。"""
    try:
        p = os.path.join(HERE, "personas", str(qq), "meta.json")
        with open(p, encoding="utf-8") as f:
            return str(json.load(f).get("nick") or "")
    except Exception:
        return ""


def add(qq: str, password: str | None = None, path: str | None = None,
        admin: bool = False) -> tuple[str, str]:
    qq = str(qq).strip()
    if not qq.isdigit() or not qq:
        raise ValueError("账号必须是纯数字 QQ 号")
    pw = password or issue_password()
    if len(pw) < 8:
        raise ValueError("密码至少 8 位")
    accounts = load(path)
    if qq in accounts:
        raise ValueError(f"{qq} 已经有账号了；改密码用 passwd")
    salt = secrets.token_hex(16)
    accounts[qq] = {
        "salt": salt,
        "hash": _derive(pw, salt),
        "iter": ITER,
        "enc": seal(pw, path),
        "nick": nick_of(qq),
        "admin": bool(admin),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save(accounts, path)
    return qq, pw


def set_password(qq: str, password: str | None = None,
                 path: str | None = None) -> tuple[str, str]:
    qq = str(qq).strip()
    accounts = load(path)
    if qq not in accounts:
        raise KeyError(f"没有 {qq} 这个账号")
    pw = password or issue_password()
    if len(pw) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_hex(16)
    accounts[qq].update({
        "salt": salt,
        "hash": _derive(pw, salt),
        "iter": ITER,
        "enc": seal(pw, path),
        "nick": accounts[qq].get("nick") or nick_of(qq),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save(accounts, path)
    return qq, pw


def init_from_personas(path: str | None = None,
                       only: list[str] | None = None) -> list[tuple[str, str]]:
    """给每个有人格档案的 QQ 建账号（只补缺的），返回 [(qq, 新密码)]。"""
    base = os.path.join(HERE, "personas")
    qqs: list[str] = []
    try:
        for name in sorted(os.listdir(base)):
            if name.isdigit() and os.path.isdir(os.path.join(base, name)):
                qqs.append(name)
    except OSError:
        pass
    exist = load(path)
    want = [q for q in qqs if not only or q in only]
    made: list[tuple[str, str]] = []
    for qq in want:
        if qq in exist:
            continue
        made.append(add(qq, None, path))
    return made


def set_admin(qq: str, on: bool = True, path: str | None = None) -> bool:
    """设为/取消管理员（管理员能看所有人、能用全部功能、能管账号）。"""
    qq = str(qq).strip()
    accounts = load(path)
    if qq not in accounts:
        return False
    accounts[qq]["admin"] = bool(on)
    accounts[qq]["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save(accounts, path)
    return True


def list_accounts(path: str | None = None) -> list[dict]:
    """账号列表（含可查看的密码），给管理页/CLI 用。"""
    out: list[dict] = []
    for qq, rec in sorted(load(path).items()):
        out.append({
            "qq": qq,
            "nick": str(rec.get("nick") or ""),
            "admin": bool(rec.get("admin")),
            "created": str(rec.get("created") or ""),
            "password": reveal(rec, path),
            "viewable": bool(rec.get("enc")),
        })
    return out


def remove(qq: str, path: str | None = None) -> bool:
    qq = str(qq).strip()
    accounts = load(path)
    if qq not in accounts:
        return False
    del accounts[qq]
    save(accounts, path)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆星云账号管理（QQ 号 + 密码）")
    ap.add_argument("--file", help="账号文件（默认 accounts.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="新增账号；不给密码就生成一个")
    p_add.add_argument("qq")
    p_add.add_argument("--password")
    p_add.add_argument("--admin", action="store_true", help="设为管理员（能看所有人）")

    p_pw = sub.add_parser("passwd", help="重设密码；不给就生成一个")
    p_pw.add_argument("qq")
    p_pw.add_argument("--password")

    p_admin = sub.add_parser("admin", help="设为/取消管理员")
    p_admin.add_argument("qq")
    p_admin.add_argument("--off", action="store_true", help="取消管理员")

    p_rm = sub.add_parser("remove", help="删除账号")
    p_rm.add_argument("qq")

    p_init = sub.add_parser("init", help="给每个有人格档案的 QQ 各建一个账号（只补缺的）")
    p_init.add_argument("--only", nargs="*", help="只建这些 QQ")

    sub.add_parser("list", help="列出账号")
    sub.add_parser("show", help="列出账号和密码（管理员用）")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            qq, pw = add(args.qq, args.password, args.file, admin=args.admin)
            nick = nick_of(qq)
            print(f"已创建 {qq}{('（' + nick + '）') if nick else ''}"
                  f"{'（管理员）' if args.admin else ''}")
            print(f"密码：{pw}")
            print("把这个 QQ 号和密码发给对方；密码只显示这一次。")
        elif args.cmd == "passwd":
            qq, pw = set_password(args.qq, args.password, args.file)
            print(f"已重设 {qq} 的密码")
            print(f"新密码：{pw}")
        elif args.cmd == "admin":
            if not set_admin(args.qq, not args.off, args.file):
                print("没有这个账号")
            else:
                print(f"{args.qq} {'已取消管理员' if args.off else '已设为管理员（能看所有人、能改）'}")
        elif args.cmd == "show":
            rows = list_accounts(args.file)
            if not rows:
                print("还没有账号。")
            for r in rows:
                role = "管理员" if r["admin"] else "普通"
                pw = r["password"] or "（旧账号不可查看，请 passwd 重设）"
                print(f"{r['qq']}  {r['nick'] or '':18} {role}  密码：{pw}")
        elif args.cmd == "remove":
            print("已删除" if remove(args.qq, args.file) else "没有这个账号")
        elif args.cmd == "init":
            made = init_from_personas(args.file, args.only)
            if not made:
                print("没有需要新建的账号（都已有，或没有 QQ 只给了昵称/没档案）")
            for qq, pw in made:
                nick = nick_of(qq)
                print(f"{qq}{('（' + nick + '）') if nick else ''}  密码：{pw}")
            if made:
                print("上面每个密码只显示这一次，发给对应的人。")
        else:
            accounts = load(args.file)
            if not accounts:
                print("还没有账号。用：account.bat init（或 account.bat add <QQ号>）")
            for qq, rec in sorted(accounts.items()):
                role = "管理员" if rec.get("admin") else "普通"
                print(f"{qq}  {rec.get('nick') or '':18} {role}  建={rec.get('created') or '?'}")
        return 0
    except (ValueError, KeyError) as e:
        print(f"失败：{e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())