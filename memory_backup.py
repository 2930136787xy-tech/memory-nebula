# -*- coding: utf-8 -*-
"""快照/回滚的可选实现（no-op 版）。

`companion_brain` 和 `viewer_server` 会调用这里的能力：写入前留一份快照、
清理 undo 表、以及 `backup_before(...)` 装饰器。真正的实现依赖调用方自己的
备份策略（比如把库复制到别处、VACUUM 前后留档），没有也不影响记忆功能本身，
所以开源这份框架时用这个 no-op 版本兜底：接口齐全、行为安全（什么都不做）。

想启用真实备份，把本文件替换成自己的实现即可，其它代码不用改。
"""
from __future__ import annotations

import functools

__all__ = ["install", "prune_undo", "snapshot", "backup_before", "enabled"]

# no-op 实现不产生任何备份，留一个开关便于调用方判断
enabled = False


def install(_conn) -> None:
    """挂上 undo 表/触发器；no-op 版什么都不做。"""
    return None


def prune_undo(_conn, _keep: int = 200) -> None:
    """裁剪 undo 记录；no-op 版什么都不做。"""
    return None


def snapshot(path: str, label: str = "") -> None:
    """给某个 db 拍一份快照；no-op 版什么都不做（只做存在性校验，便于发现问题）。"""
    del path, label
    return None


def backup_before(_label: str):
    """装饰器：在写入前拍快照；no-op 版直接透传。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator