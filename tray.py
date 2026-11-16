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

import levelmap
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

# 电量读数来自设备固件的电压估算，本身有 ±2~3% 抖动（换过大容量电池后更明显）。
# 用最近 3 次的中位数做平滑，避免托盘数字来回跳。
_HIST: dict[str, list[int]] = {"kb": [], "ms": []}


def _smooth(tag: str, level: int | None, reset: bool = False) -> int | None:
    hist = _HIST[tag]
    if reset or level is None:
        hist.clear()
        return level
    hist.append(level)
    if len(hist) > 3:
        hist.pop(0)
    return sorted(hist)[len(hist) // 2]


# 最近一次平滑后的原始固件读数（未经满电基准缩放），供"校准"菜单使用
_RAW: dict[str, int | None] = {"kb": None, "ms": None}
# 上一轮的充电状态，用来捕捉"刚拔线"这一刻（此时电量最接近满值）
_PREV_CHARGING: dict[str, bool] = {"kb": False, "ms": False}


def read_both() -> dict:
    """返回 {'kb': (level|None, charging, present), 'ms': (...)}。

    鼠标插线充电时会切到有线形态（A8A4），接收器通道的电量随之冻结。
    因此电量优先取无线通道，充电状态由无线/有线任一通道判定。
    level 是经过满电基准缩放后的显示值（见 levelmap.py）。
    """
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

    def pack(r):
        level = r.level if r and r.ok() else None
        charging = bool(r and (getattr(r, "charging", False) or "充电" in r.detail))
        return level, charging

    out = {}
    kb_lv, kb_ch = pack(best.get(kb_id))
    kb_raw = _smooth("kb", kb_lv, reset=kb_ch)

    lv_wl, ch_wl = pack(best.get(ms_wl))
    lv_wd, ch_wd = pack(best.get(ms_wd))
    level = lv_wl if lv_wl is not None else lv_wd
    # 充电/休眠已由 parse_gmouse 按位域区分（0x10=充电，0x20=休眠），这里直接采信
    charging = ch_wl or ch_wd
    # 充电状态跳变时清空历史，避免插拔瞬间的读数污染中位数
    ms_raw = _smooth("ms", level, reset=charging)

    # 刚拔线的这一刻电量最接近满值，自动把它记为满电基准（下一次充得更满会自动上移）
    for tag, raw, ch in (("kb", kb_raw, kb_ch), ("ms", ms_raw, charging)):
        if _PREV_CHARGING.get(tag) and not ch and raw:
            levelmap.set_anchor(tag, raw)
        _PREV_CHARGING[tag] = ch

    _RAW["kb"], _RAW["ms"] = kb_raw, ms_raw
    out["kb"] = (levelmap.apply("kb", kb_raw), kb_ch, kb_id in present)
    out["ms"] = (levelmap.apply("ms", ms_raw), charging, (ms_wl in present) or (ms_wd in present))
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
        if not present:
            h, color = u(6), COLOR_OFF
        elif charging:
            # 充电中一律青色；有最后读数按它画，没有就画中等高度表示"供电中"
            base = level if level is not None else 55
            h = max(u(20), u(104 * min(base, 100) / 100))
            color = COLOR_CHARGE
        elif level is None:
            h, color = u(6), COLOR_OFF
        else:
            h = max(u(6), u(104 * min(level, 100) / 100))
            color = cfor(level)
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
        elif charging:
            # 充电期间固件不刷新电量，与其显示冻结的旧数字，不如只说"充电中"
            parts.append(f"{name} 充电中")
        elif level is None:
            parts.append(f"{name} --")
        else:
            parts.append(f"{name} {level}%")
    return "键鼠电量\n" + " | ".join(parts)


# ── 托盘主程序 ────────────────────────────────────────────────

def main() -> None:
    import os

    import pystray
    from PIL import Image

    # --raw（或 WDBATTERY_RAW=1）时直接显示固件原始读数，不做满电基准缩放
    if "--raw" in sys.argv or os.environ.get("WDBATTERY_RAW") == "1":
        levelmap.set_enabled(False)

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
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "键鼠电量v2")
        # 控制台版是独立的 onedir 包，放在子目录里各自带 _internal
        for rel in ("控制台版/键鼠电量.exe", "键鼠电量.exe"):
            target = os.path.join(base, rel.replace("/", os.sep))
            if os.path.exists(target):
                os.startfile(target)
                return

    def quit_app(icon, item) -> None:
        icon.stop()

    # ── 满电基准校准 ──────────────────────────────────────
    def calibrate(tag: str) -> None:
        """把当前原始读数记为 100% 基准（充满电时点一次最准）。"""
        read_both()  # 刷新 _RAW
        raw = _RAW.get(tag)
        if raw:
            levelmap.set_anchor(tag, raw)
        apply(read_both())

    def toggle_raw(icon, item) -> None:
        levelmap.set_enabled(not item.checked)
        apply(read_both())

    icon.menu = pystray.Menu(
        pystray.MenuItem("刷新", refresh, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: is_autostart()),
        pystray.MenuItem(
            "电量校准",
            pystray.Menu(
                pystray.MenuItem("以当前键盘读数为满电基准", lambda i, it: calibrate("kb"),
                                 enabled=lambda it: _RAW.get("kb") is not None),
                pystray.MenuItem("以当前鼠标读数为满电基准", lambda i, it: calibrate("ms"),
                                 enabled=lambda it: _RAW.get("ms") is not None),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("显示固件原始读数", toggle_raw,
                                 checked=lambda item: not levelmap.load()["enabled"]),
            ),
        ),
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
