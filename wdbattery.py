#!/usr/bin/env python3
"""wdbattery.py — 无线键鼠电量读取（VGN V98Pro V3 / 狼蛛 SC620）

两条路径：
  bt     蓝牙 BLE GATT（0x180F / 0x2A19）——标准协议，切到蓝牙模式并配对后可靠可读
  2g     2.4G 接收器私有 HID 协议——已按真机描述符定位到命令通道，但握手序列需实机标定

命令：
  python wdbattery.py list             列出所有 HID 接口及已识别设备
  python wdbattery.py bt               扫描并读取蓝牙电量
  python wdbattery.py 2g               尝试 2.4G 私有协议读取
  python wdbattery.py watch -s 10      被动抓 10 秒输入报文（唤醒设备后用来定位电量字节）
  python wdbattery.py                  自动：先 2.4G，再蓝牙

依赖：pip install hidapi bleak
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

try:
    import hid
except ImportError:
    sys.exit("缺少依赖：pip install hidapi bleak")


# ── 设备指纹 ────────────────────────────────────────────────────
DEVICES = {
    (0x258A, 0x024F): "VGN V98Pro V3 2.4G Dongle",
    (0xA8A5, 0x2255): "狼蛛 SC620 USB Receiver",
    (0xA8A4, 0x2255): "狼蛛 SC620（有线模式·鼠标本体）",
}

# 鼠标插线充电时会从无线接收器（A8A5）切到有线 USB 设备（A8A4），
# 两者 PID 只差最高位，是同一只鼠标的两种枚举形态。
GM_WIRELESS = (0xA8A5, 0x2255)
GM_WIRED = (0xA8A4, 0x2255)

OTHER_VID = {
    0x05AC: "AULA F75 Max 接收器（quirk VID）",
    0x3554: "VXE / VGN 无线",
    0x373B: "ATK / VXE 无线",
    0x046D: "Logitech",
    0x1532: "Razer",
}

VENDOR_PAGE_MIN = 0xFF00


@dataclass
class Iface:
    path: bytes
    vid: int
    pid: int
    product: str
    usage_page: int
    usage: int
    interface: int

    @property
    def page_hex(self) -> str:
        return f"0x{self.usage_page:04X}"

    @property
    def vidpid(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def label(self) -> str:
        name = DEVICES.get((self.vid, self.pid), self.product or OTHER_VID.get(self.vid, ""))
        return f"{name} [{self.page_hex}/0x{self.usage:04X}]"


@dataclass
class Reading:
    name: str
    level: int | None = None
    status: str = "unknown"
    source: str = ""
    detail: str = ""
    charging: bool = False
    raw: list[str] = field(default_factory=list)
    vid: int = 0
    pid: int = 0

    def ok(self) -> bool:
        return self.level is not None


def enumerate_ifaces() -> list[Iface]:
    out = []
    for d in hid.enumerate():
        out.append(
            Iface(
                path=d["path"],
                vid=d["vendor_id"],
                pid=d["product_id"],
                product=d.get("product_string") or "",
                usage_page=d.get("usage_page") or 0,
                usage=d.get("usage") or 0,
                interface=d.get("interface_number", -1),
            )
        )
    out.sort(key=lambda i: (i.vid, i.pid, i.interface, i.usage_page, i.usage))
    return out


def hexs(b: bytes, n: int = 24) -> str:
    return " ".join(f"{x:02X}" for x in b[:n])


# ── 2.4G 协议层 ────────────────────────────────────────────────

def aula_frame(length: int = 64, with_report_id: bool = True) -> bytes | None:
    """AULA 帧族：头 `20 01`，校验位 = 其余字节累加 & 0xFF。

    with_report_id=True：首字节是 report ID 占位 0x00，校验位在 index 32
    with_report_id=False：首字节即 0x20，校验位在 index 31（无编号 report）
    """
    if not 4 <= length <= 255:
        return None
    buf = bytearray(length)
    if with_report_id:
        buf[1], buf[2] = 0x20, 0x01
        idx = 32
    else:
        buf[0], buf[1] = 0x20, 0x01
        idx = 31
    if idx >= length:
        return None
    buf[idx] = 0
    buf[idx] = sum(buf) & 0xFF
    return bytes(buf)


def parse_aula(buf: bytes) -> int | None:
    pct = None
    if len(buf) > 3 and buf[0] == 0x20 and buf[1] == 0x01:
        pct = buf[3]
    elif len(buf) > 4 and buf[1] == 0x20 and buf[2] == 0x01:
        pct = buf[4]
    if pct is not None and 1 <= pct <= 100:
        return pct
    return None


def drain(dev, ms: int = 30) -> None:
    try:
        while dev.read(64, ms):
            pass
    except OSError:
        pass


# ── VGN V98Pro V3 键盘协议（自 WebHUB drive.vgnlab.com.cn 逆向）───────────
# 通道：usage_page 0xFF02 / usage 0x0002，report ID 0x13，19 字节双向
# 电量命令：payload [0x4A, 0x01, 0x00, 0x00, 0x00] 补零到 19B（含 report id 0x13 前置）
# 应答：   [0x13, 0x4A, 0x01, 0x00, mode, pct, flag, ...]
#          pct = 电量百分比（index 5），mode 2=2.4G
#          flag = 状态位域：0x10=充电中，0x01=电池供电（实机标定：
#                 未充电时 flag=0x01，插线充电时 flag=0x10）

VGN_BATT_CMD = bytes([0x4A, 0x01, 0x00, 0x00, 0x00])
VGN_CHANNEL = (0x258A, 0x024F, 0xFF02, 0x0002)
VGN_FLAG_CHARGING = 0x10


def vgn_battery_frame(report_id: int = 0x13, length: int = 19) -> bytes:
    """构造 VGN 电量查询帧：report_id + [0x4A,1,0,0,0] 补零到 length 字节。"""
    frame = bytes([report_id]) + VGN_BATT_CMD
    if len(frame) < length:
        frame += bytes(length - len(frame))
    return frame


def parse_vgn(buf: bytes) -> tuple[int | None, int | None, int | None]:
    """解析 VGN 应答 → (电量%, 连接模式, 状态标志)。

    hidapi 读回的数据首字节是 report id(0x13)，故电量在 index 5。
    """
    if len(buf) < 6:
        return None, None, None
    # 首字节可能是 report id 0x13，命令回显 0x4A 在 index 1
    if buf[1] != 0x4A and buf[0] != 0x4A:
        return None, None, None
    pct = buf[5] if len(buf) > 5 else None
    mode = buf[4] if len(buf) > 4 else None
    flag = buf[6] if len(buf) > 6 else None
    if pct is not None and 1 <= pct <= 100:
        return pct, mode, flag
    return None, mode, flag


def read_vgn(dev) -> Reading:
    """读取 VGN V98Pro V3 键盘电量（已实机验证）。"""
    r = Reading(name="VGN V98Pro V3", source="2.4G HID 私有协议（WebHUB 逆向）", vid=0x258A, pid=0x024F)
    drain(dev)
    frame = vgn_battery_frame()
    try:
        dev.write(frame)
    except OSError as e:
        r.detail = f"写入失败：{e}"
        return r
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        try:
            data = dev.read(64, 150)
        except OSError:
            r.detail = "读取错误"
            return r
        if not data:
            continue
        raw = bytes(data)
        pct, mode, flag = parse_vgn(raw)
        r.raw.append(hexs(raw))
        if pct is not None:
            r.level = pct
            link = {1: "有线", 2: "2.4G", 3: "蓝牙"}.get(mode, f"模式{mode}")
            r.charging = bool(flag is not None and flag & VGN_FLAG_CHARGING)
            r.detail = f"{link} · 充电中" if r.charging else link
            r.status = "ok"
            return r
    r.detail = "命令通道有回包但电量字节未识别" if r.raw else "静默无应答"
    return r


def read_aula_mouse(dev, iface: Iface) -> Reading:
    """读取狼蛛 SC620 鼠标电量（Gmouse/MCHOSE G7 族协议，已实机验证 → 50%）。

    协议来源：BrimDeck (MoFeng2223) 的 DeviceProtocols.cs，Gmouse 白名单
    恰好收录 0xA8A5:0x2255（与 SC620 接收器同 VID:PID，同 Sino Wealth 方案）。

    通道：0xFF01/0x0010，64B 无编号双向。
    命令帧（hidapi 65B，首字节 0x00 为 report-id 占位，会被剥离）：
        00 55 30 A5 0B 2E 01 01 01 + 补零
    应答（64B 输入）：
        AA 30 A5 [status] 0A 01 01 01 [pct] ...
          status = byte3：0x0B=待机 / 0x5E=充电中 / 0x63=休眠（见 parse_gmouse）
          pct    = byte8（0 为"未就绪"占位，不是真 0%）
    """
    r = Reading(name="狼蛛 SC620", source="2.4G HID 私有协议（Gmouse 0x30）", vid=0xA8A5, pid=0x2255)
    drain(dev)
    frame = bytearray(65)
    frame[0] = 0x00  # report id 占位（hidapi 剥离后发出 64B 裸帧）
    for idx, b in enumerate((0x55, 0x30, 0xA5, 0x0B, 0x2E, 0x01, 0x01, 0x01)):
        frame[1 + idx] = b
    try:
        dev.write(bytes(frame))
    except OSError as e:
        r.detail = f"写入失败：{e}"
        return r
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        try:
            data = dev.read(64, 100)
        except OSError:
            break
        if not data:
            continue
        raw = bytes(data)
        r.raw.append(hexs(raw))
        pct, charging, sleeping = parse_gmouse(raw)
        if pct is not None:
            r.level = pct
            r.status = "ok"
            r.charging = charging
            if charging:
                r.detail = "2.4G · 充电中"
            elif sleeping:
                r.detail = "2.4G · 休眠"
            else:
                r.detail = "2.4G"
            return r
    if not r.raw:
        r.detail = "静默无应答（鼠标可能休眠，移动鼠标后按 R 重试）"
    return r


def parse_gmouse(buf: bytes) -> tuple[int | None, bool, bool]:
    """解析 Gmouse 应答 AA 30 ... → (电量%, 是否充电, 是否休眠)。

    实机标定（SC620，2026-11-16 逐帧对照）：
      - byte3 = 状态位域
          bit4 (0x10) = 充电中      实测 0x5E / 0x72 均置位
          bit5 (0x20) = 休眠        实测 0x63 置位，且此时电量字节保持最后一次值
          0x0B = 电池供电待机（两个位都不置位）
        注意：早期用 (status & 0x30) 判充电是错的——休眠的 0x63 含 bit5，
        会把"鼠标睡了"误判成"正在充电"，托盘图标因此变青色。
        0x3B 那种两位置位的情况仍按充电处理（bit4 优先）。
      - byte8 = 电量百分比（粗粒度电压换算，±5% 波动属正常）
      - byte9 = BrimDeck 标注的充电位，但 SC620 固件恒为 0，不可用
    电量 == 0 是"应答未就绪"占位，不算有效读数。
    """
    at = -1
    if len(buf) > 10 and buf[0] == 0xAA and buf[1] == 0x30:
        at = 0
    elif len(buf) > 11 and buf[1] == 0xAA and buf[2] == 0x30:
        at = 1
    if at < 0:
        return None, False, False
    pct = buf[at + 8]
    status = buf[at + 3]
    charging = bool(status & 0x10)
    sleeping = bool(status & 0x20) and not charging
    if 1 <= pct <= 100:
        return pct, charging, sleeping
    return None, charging, sleeping


def read_gmouse_wired(dev) -> Reading:
    """有线模式（鼠标本体 A8A4:2255）——只能确认"正在充电"，拿不到新电量。

    实机标定（SC620 插线后）：
      - 鼠标从无线形态（A8A5）切到有线形态（A8A4），接收器通道的电量随即冻结
      - 有线通道对任意子命令都回同一帧 `AA 30 A5 72 0A 01 01 01 64 01`，
        电量字节恒为 0x64(100)，是就绪占位而非真实电量
      - status 0x72 含 bit4，按充电处理
    因此本函数只回报"充电中/有线在线"，不回报电量（level 保持 None）。
    """
    r = Reading(name="狼蛛 SC620（有线模式）", source="USB 有线 HID", vid=0xA8A4, pid=0x2255)
    drain(dev)
    frame = bytearray(65)
    frame[0] = 0x00
    for idx, b in enumerate((0x55, 0x30, 0xA5, 0x0B, 0x2E, 0x01, 0x01, 0x01)):
        frame[1 + idx] = b
    try:
        dev.write(bytes(frame))
    except OSError as e:
        r.detail = f"写入失败：{e}"
        return r
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        try:
            data = dev.read(64, 100)
        except OSError:
            break
        if not data:
            continue
        raw = bytes(data)
        r.raw.append(hexs(raw))
        pct, charging, sleeping = parse_gmouse(raw)
        if pct is not None:
            r.charging = charging
            if charging:
                r.detail = "有线模式 · 充电中"
            elif sleeping:
                r.detail = "有线模式 · 休眠"
            else:
                r.detail = "有线模式"
            # 0x64 是固件占位值，采信它会让界面显示假满电
            if pct < 100:
                r.level = pct
            return r
    r.detail = "有线模式无应答"
    return r


def try_frame(dev, frame: bytes, deadline: float = 1.0) -> tuple[int | None, bytes | None]:
    drain(dev)
    try:
        dev.write(frame)
    except OSError:
        return None, None
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            data = dev.read(64, 200)
        except OSError:
            return None, None
        if not data:
            continue
        raw = bytes(data)
        pct = parse_aula(raw)
        if pct:
            return pct, raw
    return None, None


def read_24g(
    ifaces: list[Iface],
    verbose: bool = True,
    only_primary: bool = False,
    deadline: float = 1.0,
) -> list[Reading]:
    """对两台已知接收器的 vendor 接口逐个尝试。

    only_primary=True 只打每个设备最可能的主通道，用于 GUI 快速刷新。
    """
    results: list[Reading] = []
    targets = [i for i in ifaces if i.usage_page >= VENDOR_PAGE_MIN and (i.vid, i.pid) in DEVICES]
    if only_primary:
        targets = [
            i
            for i in targets
            if (i.vid, i.pid, i.usage_page, i.usage)
            in {
                (0x258A, 0x024F, 0xFF02, 0x0002),
                (0xA8A5, 0x2255, 0xFF01, 0x0010),
                (0xA8A4, 0x2255, 0xFF01, 0x0010),
            }
        ]
    if not targets:
        return results

    for i in targets:
        # VGN V98Pro V3 键盘：走已逆向验证的 0x4A 电量协议
        if (i.vid, i.pid, i.usage_page, i.usage) == VGN_CHANNEL:
            try:
                dev = hid.device()
                dev.open_path(i.path)
            except OSError as e:
                r = Reading(name=i.label, source="2.4G HID 私有协议", vid=i.vid, pid=i.pid)
                r.detail = f"打开失败：{e}"
                results.append(r)
                continue
            try:
                r = read_vgn(dev)
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
            if verbose:
                tail = f" → {r.level}%" if r.ok() else ""
                print(f"  {i.label}: {r.detail}{tail}")
            results.append(r)
            continue

        # 打开设备（狼蛛与通用分支共用）
        try:
            dev = hid.device()
            dev.open_path(i.path)
        except OSError as e:
            r = Reading(name=i.label, source="2.4G HID 私有协议", vid=i.vid, pid=i.pid)
            r.detail = f"打开失败：{e}"
            results.append(r)
            continue
        try:
            # 狼蛛 SC620（A8A5:2255）走 0xFF01/0x0010 通道：0x06 命令，应答帧头 AA FA A5
            if (i.vid, i.pid) == GM_WIRED and (i.usage_page, i.usage) == (0xFF01, 0x0010):
                r = read_gmouse_wired(dev)
            elif (i.vid, i.pid) == (0xA8A5, 0x2255) and i.usage_page == 0xFF01:
                r = read_aula_mouse(dev, i)
            else:
                r = Reading(name=i.label, source="2.4G HID 私有协议", vid=i.vid, pid=i.pid)
                # 有编号 report 的接口（多数 vendor collection）
                for ln in (64, 33):
                    f = aula_frame(ln, True)
                    pct, raw = try_frame(dev, f, deadline) if f else (None, None)
                    if pct:
                        r.level, r.detail = pct, f"AULA 帧族命中（{ln}B）"
                        r.raw.append(hexs(raw or b""))
                        break
                # 无编号 report 的接口（如 0xFF01/0x0010，64B 裸帧）
                if not r.ok():
                    f = aula_frame(64, False)
                    pct, raw = try_frame(dev, f, deadline) if f else (None, None)
                    if pct:
                        r.level, r.detail = pct, "AULA 帧族命中（无 report ID 形态）"
                        r.raw.append(hexs(raw or b""))
        finally:
            try:
                dev.close()
            except Exception:
                pass
        if not r.detail:
            r.detail = "静默无应答（设备可能深度休眠）"
        if verbose:
            tail = f" → {r.level}%" if r.ok() else ""
            print(f"  {i.label}: {r.detail}{tail}")
        results.append(r)
    return results


def silent_hint(results: list[Reading]) -> None:
    if results and not any(r.ok() for r in results):
        print(
            "\n提示：全部通道静默，最常见原因是「设备已休眠」——\n"
            "      按两下键盘、晃一下鼠标（保持 2.4G 模式），10 秒内重跑本命令。\n"
            "      若仍然静默，说明该型号的私有协议握手不在已收录族里。"
        )


# ── 蓝牙 BLE 路径 ──────────────────────────────────────────────

BATTERY_SVC = "0000180f"
BATTERY_CHR = "00002a19-0000-1000-8000-00805f9b34fb"
WANTED = ("vgn", "v98", "aula", "sc620", "狼蛛", "keyboard", "mouse")


async def read_ble(timeout: float = 10.0) -> list[Reading]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("  需要 bleak：pip install bleak")
        return []

    out: list[Reading] = []
    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        items = list(found.values())
    except TypeError:
        items = [(d, None) for d in await BleakScanner.discover(timeout=timeout)]

    hits = []
    for d, adv in items:
        name = (d.name or "").lower()
        uuids = [u.lower() for u in (adv.service_uuids or [])] if adv is not None else []
        if any(k in name for k in WANTED) or any(u.startswith(BATTERY_SVC) for u in uuids):
            hits.append(d)

    if not hits:
        return out

    for d in hits:
        r = Reading(name=f"{d.name} [{d.address}]", source="BLE GATT 0x180F")
        try:
            async with BleakClient(d.address, timeout=15.0) as cli:
                if not cli.is_connected:
                    r.detail = "连接失败"
                    out.append(r)
                    continue
                pct = None
                try:
                    data = await cli.read_gatt_char(BATTERY_CHR)
                    pct = int(data[0])
                except Exception:
                    for svc in cli.services:
                        if svc.uuid.lower().startswith(BATTERY_SVC):
                            for ch in svc.characteristics:
                                try:
                                    data = await cli.read_gatt_char(ch)
                                    if data and 0 <= int(data[0]) <= 100:
                                        pct = int(data[0])
                                        break
                                except Exception:
                                    continue
                if pct is not None:
                    r.level, r.detail = pct, "GATT Battery Service"
                else:
                    r.detail = "已连接但无电量特征值"
        except Exception as e:
            r.detail = f"异常：{type(e).__name__}: {e}"
        out.append(r)
    return out


# ── 被动抓包 ──────────────────────────────────────────────────

def cmd_watch(seconds: float) -> None:
    ifaces = [i for i in enumerate_ifaces() if i.usage_page >= VENDOR_PAGE_MIN and (i.vid, i.pid) in DEVICES]
    if not ifaces:
        print("没找到已知接收器的 vendor 接口。")
        return
    print(f"\n被动监听 {seconds:.0f} 秒 —— 现在请按键 / 晃鼠标。\n")
    for i in ifaces:
        try:
            dev = hid.device()
            dev.open_path(i.path)
        except OSError as e:
            print(f"{i.label}: 打开失败 {e}")
            continue
        print(f"--- {i.label}")
        end = time.monotonic() + seconds
        n = 0
        while time.monotonic() < end:
            try:
                data = dev.read(64, 200)
            except OSError:
                break
            if data:
                n += 1
                print(f"  {time.strftime('%H:%M:%S')} <- {hexs(bytes(data))}")
                if n >= 20:
                    break
        if n == 0:
            print("  （无报文）")
        dev.close()


# ── CLI ────────────────────────────────────────────────────────

def cmd_list() -> None:
    ifaces = enumerate_ifaces()
    print(f"\n共 {len(ifaces)} 个 HID 接口\n")
    print(f"{'VID:PID':<11} {'if':>3} {'page':>7} {'usage':>7}  {'产品':<30} 标记")
    print("-" * 92)
    for i in ifaces:
        tag = "vendor" if i.usage_page >= VENDOR_PAGE_MIN else ""
        if (i.vid, i.pid) in DEVICES:
            tag = (tag + " ★已知设备").strip()
        name = (DEVICES.get((i.vid, i.pid)) or i.product or "-")[:30]
        print(f"{i.vidpid:<11} {i.interface:>3} {i.page_hex:>7} 0x{i.usage:04X}  {name:<30} {tag}")
    print("\n页码 >= 0xFF00 是厂商自定义接口，私有协议从这里进。\n")


def cmd_2g() -> None:
    ifaces = enumerate_ifaces()
    targets = [i for i in ifaces if i.usage_page >= VENDOR_PAGE_MIN and (i.vid, i.pid) in DEVICES]
    if not targets:
        print("没找到已知接收器的 vendor 接口，2.4G 接收器插好了吗？")
        return
    print(f"\n向 {len(targets)} 个 vendor 接口发送电量查询帧…\n")
    res = read_24g(ifaces)
    ok = [r for r in res if r.ok()]
    if ok:
        print("\n结果：")
        for r in ok:
            print(f"  {r.name} → {r.level}%")
    silent_hint(res)


def cmd_bt() -> None:
    import asyncio

    print("\n扫描 BLE 键鼠（设备需切到蓝牙模式并已配对）…\n")
    res = asyncio.run(read_ble())
    if not res:
        print("  没扫到匹配设备。检查：是否切到蓝牙模式、是否已在系统设置里配对。")
        return
    for r in res:
        print(f"  {r.name}: {r.level}%  ({r.detail})" if r.ok() else f"  {r.name}: {r.detail}")


def cmd_auto() -> None:
    print("\n[1/2] 2.4G 接收器")
    res = read_24g(enumerate_ifaces())
    ok = [r for r in res if r.ok()]
    if ok:
        for r in ok:
            print(f"  {r.name} → {r.level}%")
        return
    print("\n[2/2] 蓝牙")
    cmd_bt()


def main() -> None:
    ap = argparse.ArgumentParser(description="无线键鼠电量读取")
    ap.add_argument("cmd", nargs="?", default="auto", choices=["list", "2g", "bt", "watch", "auto"])
    ap.add_argument("-s", "--seconds", type=float, default=8.0, help="watch 监听秒数")
    args = ap.parse_args()
    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "2g":
        cmd_2g()
    elif args.cmd == "bt":
        cmd_bt()
    elif args.cmd == "watch":
        cmd_watch(args.seconds)
    else:
        cmd_auto()


if __name__ == "__main__":
    main()
