# wdbattery · 无线键鼠电量

Windows 下读取 **2.4G 无线接收器** 后面那把键盘和那只鼠标的**真实电量**——不需要装厂商驱动，不弹广告，不联网。

自带两种界面：

| 界面 | 文件 | 说明 |
| --- | --- | --- |
| 控制台 TUI | `键鼠电量.exe` | 每 30 秒自动刷新，按 `R` 手动刷新、`C` 抓诊断日志、`B` 走蓝牙 |
| 系统托盘常驻 | `键鼠电量托盘.exe` | 无窗口，双竖柱图标实时显示两台设备电量，支持开机自启 |

托盘图标是**双竖柱**：左柱键盘、右柱鼠标，柱高 = 电量，颜色分级（≥60% 绿 / 25–59% 琥珀 / <25% 红 / 充电中青 / 离线灰）。悬停可看精确数字。

## 实测支持设备

| 设备 | VID:PID | 通道 | 状态 |
| --- | --- | --- | --- |
| VGN V98Pro V3（2.4G dongle） | `258A:024F` | `0xFF02 / 0x0002`，report ID `0x13` | ✅ 电量 + 充电状态 |
| 狼蛛 SC620（USB 接收器） | `A8A5:2255` | `0xFF01 / 0x0010`，无编号 64B | ✅ 电量 + 充电状态 |

> 三星财富（Sino Wealth）方案的设备协议高度相似，MCHOSE G7、AULA 等型号大概率也能直接用，欢迎提 Issue 反馈。

## 协议细节（逆向所得）

### VGN V98Pro V3

协议来自 VGN 官方 WebHUB（`drive.vgnlab.com.cn`）的 JS 逆向。

```
请求  [0x13] 4A 01 00 00 00 补零到 19 字节
应答  [0x13] 4A 01 00 [mode] [pct] [flag] ...
                ↑ 命令回显      ↑电量   ↑状态位域
```

- `pct` = index 5，电量百分比
- `mode` = index 4：1=有线 / 2=2.4G / 3=蓝牙
- `flag` = index 6：`0x10` = 充电中，`0x01` = 电池供电（实机标定）

### 狼蛛 SC620（Gmouse 协议）

协议来源：[BrimDeck](https://github.com/MoFeng2223/BrimDeck) 的 `DeviceProtocols.cs`，其中 Gmouse 白名单正好收录了 `0xA8A5:0x2255`。

```
请求  00 55 30 A5 0B 2E 01 01 01 补零到 65 字节（首字节 0x00 是 report-id 占位）
应答  AA 30 A5 [status] 0A 01 01 01 [pct] ...
                  ↑ 0x0B=电池, 0x3B=充电      ↑ 电量
```

- `pct` = byte 8，电量为 0 是"应答未就绪"占位，不算有效读数
- 充电判定用 `status & 0x30`。注意 BrimDeck 标注的 byte 9 在 SC620 固件上恒为 0，不可用——本项目的位域是拔插对照实测标定的

## 使用

### 直接下载 exe

到 [Releases](../../releases) 下载 `wdbattery-win64.zip`，解压后双击：

- `键鼠电量托盘.exe` — 常驻托盘，右键菜单可开关开机自启
- `键鼠电量.exe` — 控制台界面

exe 是 PyInstaller onedir 打包，**整个文件夹一起解压**，不要只拿 exe 出来。

### 从源码跑

```bash
pip install hidapi bleak pystray pillow

python app.py          # 控制台界面
python tray.py         # 托盘常驻
```

底层的 `wdbattery.py` 也能单独当 CLI 用：

```bash
python wdbattery.py list            # 列出所有 HID 接口，标出已识别设备
python wdbattery.py 2g              # 扫描并读取 2.4G 设备
python wdbattery.py bt              # 走蓝牙 BLE（0x180F / 0x2A19）
python wdbattery.py watch -s 10     # 被动抓 10 秒报文，用来定位新设备的电量字节
```

### 自己打包

```bash
pip install pyinstaller
pyinstaller BatteryTray.spec     # 托盘版
pyinstaller Battery.spec         # 控制台版
```

## 设备静默无应答怎么办

最常见的原因是**设备已休眠**。按两下键盘、晃一下鼠标（保持 2.4G 模式），10 秒内重试即可。若长期静默，说明该型号的私有协议握手不在已收录的协议族里，可以按下面的方式抓包分析：

```bash
python wdbattery.py watch -s 10
```

配合 `python wdbattery.py list` 查看有哪些 vendor 接口（usage page ≥ 0xFF00）。

## 已知限制

- **只支持 Windows**：依赖 `hidapi` 的 Windows 后端与注册表自启
- 托盘图标只有 16×16 像素，1% 电量约等于 0.13 像素，**小幅波动（±2%）肉眼看不出来**，看悬停提示更准
- 电量由设备固件做电压估算，本身有 ±5% 抖动，不是精密仪表
- 同时插多个同型号接收器可能混淆，当前按 VID:PID 匹配

## 致谢

- [BrimDeck](https://github.com/MoFeng2223/BrimDeck) — 狼蛛 Gmouse 协议的来源
- VGN WebHUB — 键盘协议逆向的参考实现

## License

MIT
