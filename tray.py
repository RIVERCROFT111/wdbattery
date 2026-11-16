#!/usr/bin/env python3
"""键鼠电量 — 托盘常驻版

托盘图标显示电池状态（填充高度 = 电量，颜色分级），悬停提示双设备电量，
菜单支持：刷新 / 开机自启开关 / 打开主窗口 / 退出。
电量低于 20% 时弹一次气泡提醒。

隐藏参数：--smoke  单次读取并写 smoke.txt 后退出（构建自检用）
"""

from __future__ import annotations

import sys
import threading
import time

import wdbattery as core

# ── 开机自启（注册表 HKCU Run）────────────────────────────────
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "键鼠电量托盘"


def _exe_path() -> str:
    """当前 exe（或脚本）的绝对路径，用于注册表自启。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return f'"{sys.executable}" "{sys.argv[0]}"'


def is_autostart() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    import winreg

    if enable:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _exe_path())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, AUTOSTART_NAME)
        except OSError:
            pass


# ── 状态读取 ──────────────────────────────────────────────────

def read_both() -> dict:
    """返回 {'kb': (level|None, charging, present), 'ms': (...)}。"""
    kb_id, ms_id = (0x258A, 0x024F), (0xA8A5, 0x2255)
    ifaces = core.enumerate_ifaces()
    present = {(i.vid, i.pid) for i in ifaces}
    readings = core.read_24g(ifaces, verbose=False, only_primary=True, deadline=0.35)
    best: dict = {}
    for r in readings:
        key = (r.vid, r.pid)
        if r.ok() or key not in best:
            best[key] = r
    out = {}
    for key, tag in ((kb_id, "kb"), (ms_id, "ms")):
        r = best.get(key)
        level = r.level if r and r.ok() else None
        charging = bool(r and (getattr(r, "charging", False) or "充电" in r.detail))
        out[tag] = (level, charging, key in present)
    return out


# ── 图标绘制 ──────────────────────────────────────────────────

COLOR_OK = (76, 175, 80, 255)     # 绿
COLOR_MID = (255, 152, 0, 255)    # 琥珀
COLOR_LOW = (244, 67, 54, 255)    # 红
COLOR_OFF = (158, 158, 158, 255)  # 灰（未知/离线）
COLOR_CHARGE = (0, 188, 212, 255)  # 青（充电中）


def _bar_color(level: int | None, charging: bool):
    if charging:
        return COLOR_CHARGE
    return cfor(level)


def cfor(level: int | None):
    if level is None:
        return COLOR_OFF
    if level >= 60:
        return COLOR_OK
    if level >= 25:
        return COLOR_MID
    return COLOR_LOW


def dual_icon(kb: tuple, ms: tuple) -> "Image":
    """双竖柱图标：左柱=键盘，右柱=鼠标，柱高=电量。

    每柱颜色：青=充电中，绿/琥珀/红=电量分级，灰=离线或未知。
    kb/ms 均为 (level|None, charging, present)。
    """
    from PIL import Image, ImageDraw

    S = 256  # 128 单位坐标系 × 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def u(v: float) -> int:
        return int(round(v * 2))

    for (level, charging, present), x0 in ((kb, 24), (ms, 70)):
        if not present or level is None:
            h = u(6)  # 灰色矮桩表示离线/未知
            color = COLOR_OFF
        else:
            h = max(u(6), u(104 * min(level, 100) / 100))
            color = _bar_color(level, charging)
        d.rounded_rectangle([u(x0), u(116) - h, u(x0 + 34), u(116)], radius=u(6), fill=color)
    return img


def tooltip(state: dict) -> str:
    parts = []
    label = {"kb": "键盘", "ms": "鼠标"}
    for tag in ("kb", "ms"):
        level, charging, present = state[tag]
        name = label[tag]
        if not present:
            parts.append(f"{name} 未连接")
        elif level is None:
            parts.append(f"{name} --")
        else:
            parts.append(f"{name} {level}%" + ("⚡充电中" if charging else ""))
    return "键鼠电量\n" + " | ".join(parts)


# ── 托盘主程序 ────────────────────────────────────────────────

def main() -> None:
    import pystray
    from PIL import Image

    if "--smoke" in sys.argv:
        state = read_both()
        with open("smoke.txt", "w", encoding="utf-8") as f:
            f.write(repr(state))
        return

    state = read_both()
    low_alerted = {"kb": False, "ms": False}

    icon = pystray.Icon(
        "键鼠电量",
        icon=dual_icon(state["kb"], state["ms"]),
        title=tooltip(state),
    )

    def apply(new_state: dict) -> None:
        nonlocal state
        state = new_state
        icon.icon = dual_icon(state["kb"], state["ms"])
        icon.title = tooltip(state)
        # 低电量提醒（每次跌破 20% 只弹一次）
        for tag, name in (("kb", "键盘"), ("ms", "鼠标")):
            level = state[tag][0]
            if level is not None and level < 20:
                if not low_alerted[tag]:
                    low_alerted[tag] = True
                    try:
                        icon.notify(f"{name} 电量仅剩 {level}%，请及时充电", "键鼠电量低")
                    except Exception:
                        pass
            elif level is not None and level >= 25:
                low_alerted[tag] = False

    def refresh(icon=None, item=None) -> None:
        apply(read_both())

    def toggle_autostart(icon, item) -> None:
        set_autostart(not item.checked)

    def open_console(icon, item) -> None:
        import os

        if getattr(sys, "frozen", False):
            target = os.path.join(os.path.dirname(sys.executable), "键鼠电量.exe")
        else:
            target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "键鼠电量v2", "键鼠电量.exe")
        if os.path.exists(target):
            os.startfile(target)

    def quit_app(icon, item) -> None:
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("刷新", refresh, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: is_autostart()),
        pystray.MenuItem("打开主窗口", open_console),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    # 后台轮询线程：60 秒一刷
    def worker() -> None:
        while True:
            time.sleep(60)
            try:
                apply(read_both())
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
