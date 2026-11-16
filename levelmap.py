#!/usr/bin/env python3
"""电量显示映射（满电基准缩放）。

背景：设备固件按原装 400mAh 电池的电压曲线估算百分比。换成 800mAh 之后，
满电只报到 87~89%，看起来永远充不满，而且读数在中段也比实际偏低。

做法：把"该设备实测的满电读数"记作锚点 anchor，显示值 = 原始读数 × 100 / anchor。
于是满电时显示 100%，然后随着真实电量线性往下掉——数字是缩放的，趋势仍是真实的。

配置存在 exe（或脚本）同目录的 levelmap.json，可手动编辑：
    {"enabled": true, "auto_bump": true, "anchor": {"kb": 100, "ms": 88}}

- enabled   false 则原样输出固件读数（等同 --raw）
- auto_bump true 时，若原始读数超过锚点会自动上移锚点（充得更满时校准更准）
- anchor    该设备的"满电"原始读数，10~100
"""

from __future__ import annotations

import json
import os
import sys
import threading

DEFAULT_ANCHOR = {"kb": 100, "ms": 88}
_LOCK = threading.RLock()
_CACHE: dict | None = None


def config_dir() -> str:
    """配置放在可执行文件旁边（打包后是 exe 目录，源码运行时是脚本目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(config_dir(), "levelmap.json")


def _default() -> dict:
    return {"enabled": True, "auto_bump": True, "anchor": dict(DEFAULT_ANCHOR)}


def load() -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        cfg = _default()
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            for key in ("enabled", "auto_bump"):
                if isinstance(data.get(key), bool):
                    cfg[key] = data[key]
            anchors = data.get("anchor")
            if isinstance(anchors, dict):
                for tag in ("kb", "ms"):
                    v = anchors.get(tag)
                    if isinstance(v, int) and not isinstance(v, bool) and 10 <= v <= 100:
                        cfg["anchor"][tag] = v
        _CACHE = cfg
        return cfg


def save(cfg: dict) -> None:
    global _CACHE
    with _LOCK:
        _CACHE = cfg
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def anchor(tag: str) -> int:
    return int(load()["anchor"].get(tag, 100))


def set_anchor(tag: str, raw: int) -> int:
    """把当前原始读数记为满电基准。"""
    cfg = load()
    cfg["anchor"][tag] = max(10, min(100, int(raw)))
    save(cfg)
    return cfg["anchor"][tag]


def set_enabled(flag: bool) -> None:
    cfg = load()
    cfg["enabled"] = bool(flag)
    save(cfg)


def reset(tag: str | None = None) -> None:
    """恢复默认锚点（tag 为 None 时两台设备一起）。"""
    cfg = load()
    for t in ("kb", "ms") if tag is None else (tag,):
        cfg["anchor"][t] = DEFAULT_ANCHOR[t]
    save(cfg)


def _map(raw: int | None, a: int) -> int | None:
    """纯换算，不读写配置。"""
    if raw is None:
        return None
    if a >= 100:
        return max(0, min(100, int(raw)))
    return max(0, min(100, int(round(raw * 100.0 / a))))


def preview(tag: str, raw: int) -> int:
    """按当前锚点试算显示值，不触发自动校准。"""
    return _map(raw, anchor(tag))


def apply(tag: str, raw: int | None) -> int | None:
    """原始读数 → 显示读数。"""
    if raw is None:
        return None
    cfg = load()
    if not cfg["enabled"]:
        return int(raw)
    a = int(cfg["anchor"].get(tag, 100))
    if cfg["auto_bump"] and 0 < raw - a <= 12:
        # 这次充得更满，锚点跟着上移，显示依然封顶 100%
        # （限幅 12 点，避免偶发的异常高读数把基准一次拉坏）
        a = min(100, int(raw))
        cfg["anchor"][tag] = a
        save(cfg)
    return _map(raw, a)


def describe(tag: str) -> str:
    """给菜单/提示用的一句话说明。"""
    cfg = load()
    if not cfg["enabled"]:
        return "原始固件读数"
    a = cfg["anchor"].get(tag, 100)
    if a >= 100:
        return "原始读数（未缩放）"
    return f"满电基准 {a}%（读数已按比例放大）"


if __name__ == "__main__":
    # 快速试算（不改配置）：python levelmap.py ms 88 85 60 30 5
    import sys as _sys

    tag = _sys.argv[1] if len(_sys.argv) > 1 else "ms"
    vals = [int(x) for x in _sys.argv[2:]] or list(range(100, -1, -10))
    print(f"锚点 {anchor(tag)}%  配置 {config_path()}")
    for v in vals:
        print(f"  原始 {v:3d}%  →  显示 {preview(tag, v)}%")
