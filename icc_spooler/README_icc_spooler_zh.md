# ICC Spooler（PostScript 颜色后处理，个人用原型）

目标：把现有依赖 Ghostscript 的整套打印程序，替换为
**虚拟 PostScript 打印机 + 监视服务** 的方案：应用打印到虚拟队列 →
服务读取 PostScript 文件 → 改写颜色操作符 → 以 RAW 转发到真实 Brother 队列。

当前为 **Python 原型**，后续可把同一套改写引擎移植为 **语言监视器
（language monitor）**，从而对所有应用透明生效。

程序的入口是**图形界面**（Toga 窗口 + pystray 托盘）：在界面里启动/停止
后台服务、查看实时状态与日志、编辑全部色彩设置，并支持**开机自启动**与
**启动即最小化到托盘**（关闭窗口默认只是隐藏到托盘，服务继续运行）。

## 局限（务必了解）

- 只处理 **PostScript 颜色操作符**（`setcmykcolor` / `setrgbcolor` /
  `setgray` / `setcolor` / `sc` / `scn` / `SC` / `SCN`）**以及栅格图像**。
- PScript5 驱动常在序言里用短别名，例如 `/rg/setrgbcolor load def`、
  `/g/setgray load def`、`/k/setcmykcolor load def`，正文再调用 `rg`/`g`/`k`。
  服务会识别这些别名定义并按对应颜色操作符处理（否则整份文档的颜色都改不到）。
- 图片类文档的页面颜色几乎都在栅格图像里（Brother BR-Script3 / PScript5
  驱动会把整页栅格化为 `ASCII85Decode`+`FlateDecode` 的 `colorimage`）。
  本原型会**解码这些图像像素**，用 ICC 转为 CMYK 并做 K 减量/GCR/TAC 等，
   再以 `/DeviceCMYK` 的字典形式 `image` 回填（`process_images` 控制，默认开）。
   纯 Python 实现（ICC 交给 Pillow），**无 numpy 依赖**；仅当存在 CMYK 侧
   调整（K 减量/GCR/TAC/gamma 等）时才逐像素运算，否则直接沿用 ICC 结果。
- **自动转换**：只要设置了任一 CMYK 侧调整，RGB 与灰色内容会**自动**先转成
  CMYK 再调整（文字/矢量与图片像素都处理，预览也一并显示），无需手动打开
  `rgb_to_cmyk` / `gray_to_k` / `process_images`——这三个开关仅用于"没有任何
  CMYK 调整时也强制转换"的场景。
- **中性灰改走 K（`neutral_to_k`）**：近中性 RGB（通道最大差 ≤ `neutral_threshold`，
  默认 0.04≈10/255）不走 ICC，而是直接映射为纯 K（K = 1 − 通道均值），
  彩色内容仍走 ICC。用于消除灰阶在非校准打印机上出现的青偏/蓝绿偏色。
  为避免**近黑但带轻微色调的格子**（如很暗的蓝黑）被压成中性灰，亮度低于
  `neutral_min`（默认 0.05）的中性色保留 ICC 结果；灰阶 ramp 本身远高于该值，
  不受影响。
  `neutral_vector_only`（默认 true）把该功能限制在**矢量**内容：图片像素仍走 ICC，
  因为照片通常不需要强制为纯 K。在界面中勾选「中性灰改走 K」后才会显示下面的
  「仅矢量 / 阈值 / 下限」三个选项。
- 界面为可折叠式：「K 减少」「分色与总量」「中性灰改走 K」的参数默认**收起**，
  勾选各自的「显示…」开关后才展开，其余元素会自动上移靠拢（不是只把控件藏起来）。
- 文件端口每个作业会覆盖同名文件 → **只能顺序打印**（不要并发）。
- 若驱动输出二进制 PostScript，词法器仍会按字节透传。
- RGB→CMYK 需要目标 ICC；缺失时图像会退化为简单换算（无 ICC）。
- **色彩引擎（CMM）**：RGB→CMYK 可选 `lcms`（内置 littleCMS，默认）或
  `wcs`（Windows 自带 CMM，通过 `mscms.dll`，无第三方依赖）。两者都是 8-bit，
  只是**数值不同**——`wcs` 给出 Windows/GDI 原生的转换结果；`wcs` 不可用时自动回退到
  `lcms`。配置项为 `color.cmm`（`lcms` / `wcs`）。

## 单实例（同一时间只有一个托盘 / 后台）

- 程序用**命名互斥体** `Local\IccSpooler_SingleInstance` 保证单实例：
  重复双击启动**不会再生成第二个托盘图标，也不会再跑一个服务**。
- 再次启动时，新进程只写一个信号文件（`%LOCALAPPDATA%\IccSpooler\activate.signal`）
  随即退出；已运行的实例在自己的界面循环里收到信号后**显示窗口**，
  并在**托盘图标已崩（线程已死）时自动重建托盘**。因此"托盘崩了但后台还在"
  时，重新打开只是唤回原来的后台，而不是再起一个。

## 配置要点

**自动保存**：在界面里改任何参数后，约 0.8 秒无操作就会自动写入 `config.json`
（服务在运行时会自动重启以应用新设置）。「保存」按钮（立即保存并应用）仍然保留，
「另存为…」用于导出到别的文件。

`config.json` 顶层：

| 字段 | 作用 |
| --- | --- |
| `printer` | 真实 Brother 队列名（RAW 转发目标） |
| `virtual_printer` | 虚拟打印机队列名（界面「虚拟打印机」页使用） |
| `virtual_port` | 虚拟打印机的文件端口（即 spool 文件 `in.prn` 路径） |
| `spool_dir` | 虚拟打印机文件端口所在目录 |
| `archive_dir` | 处理后的归档目录 |
| `spool_pattern` | 匹配文件名（文件端口是 `in.prn`，故默认 `*.prn`） |
| `poll_seconds` | 轮询间隔 |
| `dry_run` | 试运行：只生成中间文件（归档到 `archive_dir`），不发送到真实打印机；处理完会删除 spool 文件，避免重启后重发 |
| `auto_start_service` | 打开 GUI 时自动启动服务 |
| `autostart` | 是否开机自启动（实际状态以注册表 HKCU\\...\\Run 为准） |
| `start_minimized` | 启动时最小化到托盘 |
| `minimize_to_tray` | 关闭窗口时最小化到托盘（服务继续运行） |
| `lang` | 界面语言代码（默认 `en`）。改语言需重启程序生效 |
| `log_file` | 日志文件路径（空则用 `<spool_dir>\icc_spooler.log`） |

**界面语言**：界面文案默认英文；切换语言在「基本」页的「语言」下拉框里选，
保存后**重启程序**生效。语言文件放在 `icc_spooler/lang/<code>.json`，
把「英文原文 → 译文」逐条写进去即可（`__name__` 是显示名），源码英文为兜底，
缺条目时自动回退英文。内置 `en`（英文）与 `zh`（中文）。

`color` 段见 `config.py`：K 减量 / 动态 K 曲线 / 保留纯黑 / CMY 缩放 /
逐通道 gamma / GCR / UCR / rich black / TAC / RGB→CMYK(ICC) / 灰→K /
中性灰改走 K（`neutral_to_k` + `neutral_threshold` + `neutral_min`）/
矢量文字近黑变实心黑（`text_solid_black` + `text_solid_black_luma`）/ `process_images`。

**矢量文字实心黑**（`text_solid_black`，默认关）：开启后，绘制文字时如果当时
的填充色接近黑色（亮度 ≤ `text_solid_black_luma`，默认 0.1 且近中性），就把这段
文字临时改成实心 `K=100` 再绘制，画完恢复原色。原因是：任何低于 100% 的颜色都要
挂网（half-tone），小字笔画细，网点间距一大就糊成「网状」；实心黑不挂网。它只影响
文字绘制算子（`show` 等），不影响图形和图像。


## 安装（概要）

1. 安装 Brother BR-Script3 驱动，确认真实队列存在
   （默认 `Brother MFC-L3750CDW BR-Script3`）。
2. 启动程序：双击 `IccSpooler.pyw`（或 `python -m icc_spooler`）打开图形界面。
3. 在「虚拟打印机」页创建虚拟队列（见下）。队列名 / 驱动 / 端口文件都可编辑，
   点「创建 / 更新」即可；如需删除点「删除」，也可「设为默认」。
4. 在「服务」页点「启动服务」；也可设「开机自动启动」「启动时最小化到托盘」。
5. 把"Brother ICC Virtual"设为默认打印机，从任意应用打印。

更多命令说明见 `python -m icc_spooler install-help`。

### 虚拟打印机管理（界面内完成，无需 PowerShell）

「虚拟打印机」页直接调用 **Win32 API** 创建/删除队列，不依赖 PowerShell：

- **文件端口**：通过 `XcvData("AddPort")` 驱动系统「Local Port」端口监视器，
  端口名即目标文件路径（经典 print-to-file 端口）。
- **队列**：`AddPrinter`（PRINTER_INFO_2，DEVMODE 从同驱动的现有队列借用）。
- **删除**：`DeletePrinter`（管理员句柄）+ `XcvData("DeletePort")` 移除端口。
- **提权**：创建/删除/设默认需要管理员权限。非管理员时界面用
  `ShellExecuteExW("runas")` 以 UAC 重新拉起本程序的 `printer-admin`
  子命令执行，并把结果以 JSON 回传；已是管理员则直接执行。

> 仍保留 `setup_virtual_printer.ps1` 作为**可选**的旧脚本方式，
> 但界面已经不再需要它。

## 组件

| 文件 | 作用 |
| --- | --- |
| `config.py` | `ColorSettings` / `AppConfig`，JSON 读写与校验 |
| `pstokens.py` | PostScript 词法器（无损切分，保护字符串/字典/二进制） |
| `colorops.py` | 颜色变换引擎（GCR/UCR/K 缩减/动态 K 曲线/rich black/CMY 缩放/逐通道 gamma/TAC/ICC） |
| `engine.py` | 单遍改写 PostScript 颜色操作符，失败时原样返回 |
| `imageops.py` | 解码 `colorimage` 图像像素，ICC 转 CMYK + 数值流水线，回填 `/DeviceCMYK` 字典形式 `image` |
| `service.py` | 监视 spool 目录、归档、RAW 转发（支持 `stop_event`） |
| `controller.py` | `ServiceController`：在线程里跑监视循环，向界面提供状态/统计/日志 |
| `gui.py` | Toga 图形界面：设置 + 服务控制 + 状态/日志 + 托盘开关 |
| `tray.py` | pystray 托盘图标（独立线程，菜单经命令队列回主线程） |
| `autostart.py` | 开机自启动（注册表 HKCU\\...\\Run） |
| `single_instance.py` | 单实例（命名互斥体）+ 重复启动时唤醒已有实例 |
| `printer_admin.py` | 原生 Win32 管理虚拟打印机（`XcvData`/`AddPrinter`/`DeletePrinter`）+ UAC 提权（`runas`） |
| `sample.py` | 生成 CMYK/RGB/gray 测试页 |
| `__main__.py` | 入口（无子命令即打开 GUI） |
| `setup_virtual_printer.ps1` | 创建/删除虚拟打印机（可选，界面已内置等价功能） |
| `IccSpooler.pyw`（仓库根） | 双击 / 自启动的图形界面启动器 |

## 常用命令

```powershell
# 图形界面（默认入口）
python -m icc_spooler                 # 等同 gui
python -m icc_spooler gui --config config.json [--minimized]

# 命令行 / 调试
python -m icc_spooler sample  sample.ps
python -m icc_spooler process sample.ps out.ps --config config.json
python -m icc_spooler watch   --config config.json [--dry-run]
python -m icc_spooler init-config config.json
python -m icc_spooler install-help
```

`--dry-run`：不发送到打印机，只写归档并打印统计，便于验证。
