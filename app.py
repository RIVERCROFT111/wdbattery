#!/usr/bin/env python3
"""键鼠电量 — 控制台版（打包 exe 双击运行）"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import sys
import time

import wdbattery as core

KEYBOARD = (0x258A, 0x024F)
MOUSE = (0xA8A5, 0x2255)
W = 46

CLS = "\033[2J\033[H"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
AMBER = "\033[33m"
RED = "\033[31m"
BLUE = "\033[36m"


def enable_vt() -> bool:
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        k.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


def line(ch: str = "-") -> str:
    return ch * W


def pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文算 2 列）"""
    disp = sum(2 if ord(c) > 127 else 1 for c in text)
    return text + " " * max(0, width - disp)


def read_state() -> dict:
    kb_id = (0x258A, 0x024F)
    ms_wl, ms_wd = core.GM_WIRELESS, core.GM_WIRED
    ifaces = core.enumerate_ifaces()
    present = {(i.vid, i.pid) for i in ifaces}
    readings = core.read_24g(ifaces, verbose=False, only_primary=True, deadline=0.5)
    best: dict = {}
    for r in readings:
        key = (r.vid, r.pid)
        if r.ok() or key not in best:
            best[key] = r

    out = {}
    kb = best.get(kb_id)
    out["kb"] = (kb_id in present, kb.level if kb and kb.ok() else None, kb.detail if kb else "")

    # 鼠标：电量取无线通道（有线模式拿不到真实电量），detail 优先用有线通道的充电状态
    wl, wd = best.get(ms_wl), best.get(ms_wd)
    level = wl.level if wl and wl.ok() else (wd.level if wd and wd.ok() else None)
    if wd is not None and wd.raw:
        detail = wd.detail
    else:
        detail = wl.detail if wl else ""
    out["ms"] = ((ms_wl in present) or (ms_wd in present), level, detail)
    return out


def render(state: dict, note: str, last: str) -> str:
    rows = [f"{BOLD}  键鼠电量{RESET} {DIM}VGN V98Pro V3 / 狼蛛 SC620{RESET}", line("=")]
    for tag, name, label in (("kb", "键盘", "VGN V98Pro V3"), ("ms", "鼠标", "狼蛛 SC620")):
        item = state.get(tag, (None, None, ""))
        present = item[0]
        level = item[1] if len(item) > 1 else None
        detail = item[2] if len(item) > 2 else ""
        if present is None:
            status, color, value = "检测中…", DIM, "--"
        elif not present:
            status, color, value = "接收器未插上", DIM, "--"
        elif level is None:
            if detail and "休眠" in detail:
                status, color, value = "深度休眠 · 晃动鼠标后按 R 重试", AMBER, "--"
            else:
                status, color, value = "接收器在线 · 电量协议未标定", AMBER, "--"
        else:
            color = GREEN if level >= 60 else AMBER if level >= 25 else RED
            extra = f" · {detail}" if detail else ""
            status, value = f"接收器在线{extra}", f"{level}%"
        rows.append("")
        rows.append(f"  {BOLD}{name}{RESET}  {label}")
        rows.append(f"        {color}{pad(status, 34)}{RESET}")
        rows.append(f"        {BOLD}{value}{RESET}")
    rows.append("")
    rows.append(line("-"))
    rows.append(f"  {BLUE}[R]{RESET} 刷新   {BLUE}[C]{RESET} 诊断抓包   {BLUE}[B]{RESET} 蓝牙读取   {BLUE}[Q]{RESET} 退出")
    rows.append(line("-"))
    rows.append(f"  {DIM}{note}{RESET}")
    rows.append(f"  {DIM}{last}{RESET}")
    return "\n".join(rows)


def main() -> None:
    once = "--once" in sys.argv
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    vt = enable_vt()
    state = {"kb": (None, None, ""), "ms": (None, None, "")}
    note = "按 R 刷新，C 抓诊断日志，B 走蓝牙读取"
    last = ""

    def refresh(msg: str = "读取中…"):
        nonlocal state, note
        note = msg
        draw()
        state = read_state()
        note = "已刷新"

    def draw():
        if vt:
            sys.stdout.write(CLS + render(state, note, last) + "\n")
        else:
            os.system("cls")
            sys.stdout.write(render(state, note, last) + "\n")
        sys.stdout.flush()

    refresh()
    last = f"刷新时间 {dt.datetime.now():%H:%M:%S}"
    draw()

    if once:
        return

    import msvcrt

    next_auto = time.monotonic() + 30
    while True:
        if time.monotonic() >= next_auto:
            refresh()
            last = f"刷新时间 {dt.datetime.now():%H:%M:%S} · 自动刷新已开启"
            next_auto = time.monotonic() + 30
            draw()
        if not msvcrt.kbhit():
            time.sleep(0.08)
            continue
        ch = msvcrt.getch().lower()
        if ch in (b"q", b"\x1b"):
            break
        if ch == b"r":
            refresh()
            last = f"刷新时间 {dt.datetime.now():%H:%M:%S}"
            draw()
        elif ch == b"c":
            note = "抓包 10 秒 — 现在请按键、晃鼠标"
            draw()
            path = capture()
            last = f"日志：{os.path.basename(path)}" if path else "抓包失败"
            note = "抓包完成"
            draw()
        elif ch == b"b":
            note = "扫描蓝牙（需设备处于蓝牙模式并已配对）…"
            draw()
            res = run_ble()
            last = res
            note = "蓝牙扫描结束"
            draw()


def capture() -> str | None:
    import time

    import hid

    lines = [f"诊断日志 {dt.datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    ifaces = [i for i in core.enumerate_ifaces() if (i.vid, i.pid) in core.DEVICES]
    for i in ifaces:
        lines.append(f"{i.vidpid} if{i.interface} {i.page_hex}/0x{i.usage:04X}")
    opened = []
    for i in [x for x in ifaces if x.usage_page >= core.VENDOR_PAGE_MIN]:
        lines.append(f"--- {i.label}")
        try:
            dev = hid.device()
            dev.open_path(i.path)
        except OSError as e:
            lines.append(f"    打开失败 {e}")
            continue
        try:
            lines.append(f"    描述符: {core.hexs(dev.get_report_descriptor() or b'', 64)}")
        except Exception:
            pass
        for rid, n in ((0x05, 5), (0x0A, 7)):
            for total in (n, n + 1):
                try:
                    buf = dev.get_feature_report(rid, total)
                    lines.append(f"    feature[{rid:02X}] len={total}: {core.hexs(bytes(buf), 32)}")
                    break
                except Exception:
                    continue
        opened.append((i.label, dev))
    end = time.monotonic() + 10
    while time.monotonic() < end:
        for label, dev in opened:
            try:
                data = dev.read(64, 15)
            except OSError:
                continue
            if data:
                lines.append(f"{time.strftime('%H:%M:%S')} {label} <- {core.hexs(bytes(data), 32)}")
    for _, dev in opened:
        try:
            dev.close()
        except Exception:
            pass

    try:
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(
            os.path.abspath(__file__)
        )
        path = os.path.join(base, f"诊断日志_{dt.datetime.now():%Y%m%d_%H%M%S}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception:
        return None


def run_ble() -> str:
    import asyncio

    try:
        res = asyncio.run(core.read_ble(timeout=8))
    except Exception as e:
        return f"蓝牙读取异常：{type(e).__name__}"
    if not res:
        return "没扫到蓝牙键鼠（需切到蓝牙模式并已配对）"
    got = [f"{r.name} {r.level}%" for r in res if r.ok()]
    return "蓝牙：" + (" | ".join(got) if got else "已连接但无电量特征值")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
