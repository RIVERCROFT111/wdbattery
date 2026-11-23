#!/usr/bin/env python3
"""键鼠电量 — 托盘常驻版

托盘图标是紫粉方圆底板上的两个圆圈（外环=键盘、内点=鼠标），颜色即状态；
悬停提示显示双设备电量与充电/休眠状态，
菜单支持：刷新 / 开机自启开关 / 电量校准 / 打开主窗口 / 退出。
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
        sleeping = bool(r and getattr(r, "sleeping", False))
        return level, charging, sleeping

    out = {}
    kb_lv, kb_ch, _ = pack(best.get(kb_id))
    kb_raw = _smooth("kb", kb_lv, reset=kb_ch)

    lv_wl, ch_wl, sl_wl = pack(best.get(ms_wl))
    lv_wd, ch_wd, sl_wd = pack(best.get(ms_wd))
    level = lv_wl if lv_wl is not None else lv_wd
    wired_present = ms_wd in present
    # 真在充电必然插着线，而插线后鼠标必以有线形态（A8A4）枚举出来。
    # 位域只作参考：休眠状态字节五花八门（实测 0x0B / 0x61 / 0x63），
    # 不排除某些休眠态带充电位 bit4，单看位域会重演"睡着被当成充电"。
    charging = (ch_wl or ch_wd) and wired_present
    sleeping = (sl_wl or sl_wd) and not charging
    # 充电状态跳变时清空历史，避免插拔瞬间的读数污染中位数
    ms_raw = _smooth("ms", level, reset=charging)

    # 刚拔线的这一刻电量最接近满值，自动把它记为满电基准（下一次充得更满会自动上移）
    for tag, raw, ch in (("kb", kb_raw, kb_ch), ("ms", ms_raw, charging)):
        if _PREV_CHARGING.get(tag) and not ch and raw:
            levelmap.set_anchor(tag, raw)
        _PREV_CHARGING[tag] = ch

    _RAW["kb"], _RAW["ms"] = kb_raw, ms_raw
    out["kb"] = (levelmap.apply("kb", kb_raw), kb_ch, kb_id in present, False)
    out["ms"] = (levelmap.apply("ms", ms_raw), charging, (ms_wl in present) or wired_present, sleeping)
    return out


# ── 图标绘制 ──────────────────────────────────────────────────
#
# 图标就是两个基本图形：一大一小两个圆套在一起（外环 + 内点），底板是紫粉方圆。
#   外环 = 键盘   内点 = 鼠标
# 颜色即状态：白=正常(≥60%) / 琥珀=25~59% / 红=<25% / 青=充电中 / 半透明白=离线未知

COLOR_OK = (255, 255, 255, 255)     # 白（正常）
COLOR_MID = (255, 209, 102, 255)    # 琥珀
COLOR_LOW = (255, 69, 90, 255)      # 红
COLOR_OFF = (255, 255, 255, 92)     # 半透明白（离线/未知）
COLOR_CHARGE = (77, 225, 240, 255)  # 青（充电中）

BG_TOP, BG_BOT = (156, 108, 255), (232, 92, 158)
ICON_SIZE = 256
_BASE_CACHE = None


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _badge_base():
    """紫粉纵向渐变 + 圆角方形遮罩，缓存一份供每次复制。"""
    global _BASE_CACHE
    if _BASE_CACHE is None:
        from PIL import Image, ImageDraw

        S = ICON_SIZE
        grad = Image.new("RGB", (S, S))
        gd = ImageDraw.Draw(grad)
        for y in range(S):
            gd.line([0, y, S, y], fill=_lerp(BG_TOP, BG_BOT, y / (S - 1)))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([14, 14, 242, 242], radius=58, fill=255)
        base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        base.paste(grad.convert("RGBA"), (0, 0), mask)
        _BASE_CACHE = base
    return _BASE_CACHE.copy()


def cfor(level: int | None):
    """纯电量分级取色（离线/未知为半透明白）。"""
    if level is None:
        return COLOR_OFF
    if level >= 60:
        return COLOR_OK
    if level >= 25:
        return COLOR_MID
    return COLOR_LOW


def state_color(level: int | None, charging: bool, present: bool):
    """单个设备的状态色：充电优先，其次离线，再按电量分级。"""
    if charging:
        return COLOR_CHARGE
    if not present or level is None:
        return COLOR_OFF
    return cfor(level)


def dual_icon(kb: tuple, ms: tuple) -> "Image":
    """圆形图标：外环=键盘、内点=鼠标，只有两个基本图形。

    kb/ms 均为 (level|None, charging, present, sleeping)。
    """
    from PIL import ImageDraw

    img = _badge_base()
    d = ImageDraw.Draw(img)

    kb_level, kb_ch, kb_present = kb[0], kb[1], kb[2]
    ms_level, ms_ch, ms_present = ms[0], ms[1], ms[2]

    ring_color = state_color(kb_level, kb_ch, kb_present)
    dot_color = state_color(ms_level, ms_ch, ms_present)

    # 外环：r=88（直径 176），环宽 18
    d.ellipse([40, 40, 216, 216], outline=ring_color, width=18)
    # 内点：r=40（直径 80）
    d.ellipse([88, 88, 168, 168], fill=dot_color)
    return img


def tooltip(state: dict) -> str:
    parts = []
    label = {"kb": "键盘", "ms": "鼠标"}
    for tag in ("kb", "ms"):
        level, charging, present, sleeping = state[tag]
        name = label[tag]
        if not present:
            parts.append(f"{name} 未连接")
        elif charging:
            # 充电期间固件不刷新电量，与其显示冻结的旧数字，不如只说"充电中"
            parts.append(f"{name} 充电中")
        elif level is None:
            parts.append(f"{name} --")
        else:
            parts.append(f"{name} {level}%" + ("（休眠）" if sleeping else ""))
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

    # 首次读取也放独立线程 + 超时，避免极端情况下启动即卡死在 HID 读上
    box: dict = {}

    def probe() -> None:
        box["s"] = read_both()

    _t = threading.Thread(target=probe, daemon=True)
    _t.start()
    _t.join(timeout=15)
    state = box.get("s") or {"kb": (None, False, False, False), "ms": (None, False, False, False)}
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

    # 后台轮询线程：60 秒一刷。
    # 每轮用独立线程 + 超时看门狗：万一 HID 读写卡死（设备休眠瞬间的偶发问题），
    # 不会让整个轮询线程挂死、图标冻结在旧状态（比如一直显示"充电中"）。
    def worker() -> None:
        while True:
            time.sleep(60)
            box: dict = {}

            def probe() -> None:
                box["s"] = read_both()

            t = threading.Thread(target=probe, daemon=True)
            t.start()
            t.join(timeout=15)
            if "s" not in box:
                continue  # 本轮卡住/失败，丢弃，下一轮重试
            try:
                apply(box["s"])
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
