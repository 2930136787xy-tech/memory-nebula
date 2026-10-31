# -*- coding: utf-8 -*-
"""每天跑一次记忆召回评测，把结果追加到 data/eval-history.jsonl。

为什么要做：参数（关键词权重、拒答阈值、分流正则）改完只能靠手动跑一次，
时间一长没人记得上次是多少。留一条历史，退化一眼能看见。

用法：
    python scripts/eval-daily.py            # 跑一次并记录
    python scripts/eval-daily.py --show     # 只看最近 10 条历史，不跑

退出码：0 正常；1 评测失败；2 指标明显退化（Hit@1 比上次掉 10 个点以上）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "eval-history.jsonl"
EVAL = ROOT / "scripts" / "eval_memory.py"
PYTHON = sys.executable or r"python"

# 退化告警线：总体 Hit@1 比上次掉这么多点就提示
DROP_ALERT = 0.10


def _load_recent(n: int = 10) -> list[dict]:
    if not HISTORY.exists():
        return []
    out: list[dict] = []
    with HISTORY.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out[-n:]


def _extract(stdout: str) -> dict:
    """从评测输出里抠出 JSON（表格和 JSON 可能混在一起）。"""
    start = stdout.find('{\n  "modes"')
    if start < 0:
        start = stdout.find("{")
    if start < 0:
        raise ValueError("输出里没有 JSON")
    data = json.loads(stdout[start:])
    modes = data.get("modes") or {}
    fused = modes.get("fuse") or modes.get("向量+关键词融合") or {}
    base = modes.get("vector") or {}
    fo = (fused.get("metrics") or {}).get("overall") or {}
    bo = (base.get("metrics") or {}).get("overall") or {}
    comp = data.get("comparison") or {}
    return {
        "hit1": fo.get("hit1"), "hit3": fo.get("hit3"), "hit5": fo.get("hit5"),
        "mrr": fo.get("mrr"), "correct_rate": fo.get("correct_rate"),
        "base_hit1": bo.get("hit1"), "base_hit5": bo.get("hit5"),
        "delta_hit1": comp.get("hit1_delta"), "delta_hit5": comp.get("hit5_delta"),
    }


def run() -> int:
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [PYTHON, str(EVAL), "--auto", "--parity", "--route", "--json"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900)
    if proc.returncode != 0:
        print("评测失败：")
        print((proc.stderr or proc.stdout or "")[-1500:])
        return 1
    try:
        row = _extract(proc.stdout)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"解析输出失败：{e}")
        print(proc.stdout[-800:])
        return 1
    row["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    row["secs"] = round(time.time() - t0, 1)

    prev = _load_recent(1)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[{row['ts']}] Hit@1 {row['hit1']:.1%}  Hit@5 {row['hit5']:.1%}  "
          f"MRR {row['mrr']:.3f}  端到端 {row['correct_rate']:.1%}  "
          f"({row['secs']}s)")
    code = 0
    if prev:
        old = prev[0].get("hit1")
        if isinstance(old, (int, float)) and isinstance(row["hit1"], (int, float)):
            drop = old - row["hit1"]
            if drop >= DROP_ALERT:
                print(f"⚠ 退化：Hit@1 {old:.1%} → {row['hit1']:.1%}（掉 {drop:.1%}）")
                code = 2
    return code


def show() -> int:
    rows = _load_recent(10)
    if not rows:
        print("还没有评测历史。")
        return 0
    print(f"{'时间':<20}{'Hit@1':>8}{'Hit@5':>8}{'MRR':>8}{'端到端':>8}")
    for r in rows:
        def pct(v):
            return f"{v:.1%}" if isinstance(v, (int, float)) else "-"
        print(f"{r.get('ts', ''):<20}{pct(r.get('hit1')):>8}"
              f"{pct(r.get('hit5')):>8}{r.get('mrr', 0):>8.3f}"
              f"{pct(r.get('correct_rate')):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(show() if "--show" in sys.argv else run())
