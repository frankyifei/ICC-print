# ICC Spooler（虚拟 PostScript 打印机 + 颜色后处理）

用一台**虚拟 PostScript 打印机**截获应用的打印输出，在后台用 Python 改写
PostScript 里的颜色操作符与图像像素，再以 **RAW** 转发给真实打印机。
支持 Windows。

- 不依赖 Ghostscript，不生成中间 PDF，不做光栅整页转换。
- 打印链路：`应用 → 虚拟队列（文件端口）→ in.prn → 服务改写 → 真实 Brother 队列`。
- 目标配置文件默认 `JapanColor2001Uncoated.icc`（Japan Color 2001 Uncoated）。
- 单用户、本机使用；颜色处理只针对 PostScript。

---

## 1. 它解决什么问题

1. **色彩不准**：应用输出的 RGB / 灰色内容直接交给打印机，等于让驱动自己猜。
   这里统一用目标 ICC 转到 CMYK，并允许再做 K 减量、GCR/UCR、TAC 限墨等调整。
2. **小字发虚、发“网状”**：任何低于 100% 的墨量都要挂网。细笔画一旦被四色网屏
   （四个角度叠加）打，边缘就碎。中性文字改走**单通道 K**（Acrobat 的做法），
   笔画变实。
3. **灰阶偏色**：非校准打印机上 CMY 复合灰常偏青/蓝绿。近中性 RGB 可以直接
   落到纯 K，避开 ICC 的色偏。

---

## 2. 架构与数据流

```
应用（任意程序）
   │  打印到 "Brother ICC Virtual"
   ▼
Local Port（文件端口：C:\ICCSpool\in.prn）
   │  服务按 spool_pattern 轮询，等文件大小连续两次不变
   ▼
icc_spooler.service.process_file
   │  读取 → engine.process_ps 改写 → 写归档（archive_dir，同名覆盖）
   ▼
icc_spooler.service.forward_raw
   │  win32print StartDocPrinter(..., "RAW") 直接投递改写后的 PostScript
   ▼
真实队列 "Brother MFC-L3750CDW BR-Script3"
```

处理完成后**无论成功失败都删除 spool 文件**（dry-run 也一样），避免重启后
重复投递。改写后的副本留在 `archive_dir` 供检查。

---

## 3. 安装与首次配置

1. 安装 **Brother BR-Script3** 驱动，并确认真实队列存在
   （默认名 `Brother MFC-L3750CDW BR-Script3`）。
2. 建目录（默认）：`C:\ICCSpool`、`C:\ICCSpool\done`。
3. 安装依赖：`pip install -r requirements.txt`
   （Toga、pystray、Pillow、pywin32）。
4. 启动界面：双击 `IccSpooler.pyw`，或 `python -m icc_spooler`。
5. 「虚拟打印机」页 → 填队列名/驱动/端口文件 → **创建 / 更新**（会弹 UAC）。
6. 「虚拟打印机」页 → **设为默认**（日常打印都走这个队列）。
7. 「服务」页 → **启动服务**；需要可再开「开机自动启动」「启动时最小化到托盘」。

> 文件端口每个作业都覆盖同一个 `in.prn`，**只能顺序打印**（不要并发投递）。

---

## 4. 界面各页与选项

界面语言默认英文，可在「General」页的「Language」切换（内置 `en`、`zh`），
**重启程序生效**。语言文件在 `icc_spooler/lang/<code>.json`，键是源码里的英文原文。

### 4.1 Service（服务）

| 控件 | 作用 |
| --- | --- |
| Start / Stop Service | 在当前进程内启动 / 停止后台监视线程 |
| 状态与统计 | 当前状态、最近一次作业的 `stats`（`cmyk` / `rgb` / `gray` / `text` / `images` 改写次数） |
| 日志 | 实时日志；文件日志见 `log_file`（空则 `<spool_dir>\icc_spooler.log`） |

### 4.2 General（基本）

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| Enable color processing | 开 | 总开关。关闭后 PostScript 原样转发，不做任何改写 |
| Target printer queue | `Brother MFC-L3750CDW BR-Script3` | 真实打印机队列（RAW 转发的目标） |
| Watch folder (spool) | `C:\ICCSpool` | 监视目录，即虚拟打印机文件端口所在目录 |
| Archive folder (processed) | `C:\ICCSpool\done` | 改写后副本的归档目录（同名覆盖） |
| Watch filename pattern | `*.prn` | 监视的文件名通配符；文件端口固定写 `in.prn`，所以用 `*.prn` |
| Poll interval (seconds) | 1.0 | 轮询间隔；文件大小需连续两次相同才处理 |
| Language | `en` | 界面语言，改后需重启 |

### 4.3 Virtual Printer（虚拟打印机）

| 选项 | 作用 |
| --- | --- |
| Queue name | 虚拟队列名（默认 `Brother ICC Virtual`） |
| Driver (PostScript) | 从系统已安装驱动里选，必须是与真实打印机同系列的 BR-Script3 驱动 |
| Port file | 文件端口路径（默认 `C:\ICCSpool\in.prn`），端口即“打印到文件” |
| 同步监视目录 | 创建后把 `spool_dir` 自动改成端口文件所在目录 |
| Check / Create-Update / Delete / Set as default | 通过 Win32 API（`XcvData("AddPort")` + `AddPrinter` 等）直接管理队列；需要管理员权限，非管理员时自动以 UAC 重新拉起 `printer-admin` 子命令 |

### 4.4 Color Conversion（色彩转换）

顶部说明：**自动模式**——只要存在任一 CMYK 侧调整（K 减量、CMY 缩放、分色、
GCR/UCR、gamma、TAC、rich black、K 曲线），RGB 与灰色内容会先自动转成 CMYK
再调整，图片像素与预览同样如此。下面三个开关只用于“**没有任何 CMYK 调整时也强制转换**”。

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| Target ICC (CMYK) | `JapanColor2001Uncoated.icc` | RGB→CMYK 的目标配置文件；也是预览的软打样目标 |
| Input RGB ICC (optional) | 空 | 指定源 RGB 空间；空则按 sRGB 处理 |
| Rendering intent | 相对比色 | 感知 / 相对比色 / 饱和度 / 绝对比色 |
| Color engine (CMM) | `lcms` | `lcms` = Pillow 内置 littleCMS；`wcs` = Windows `mscms.dll`（GDI 原生数值）。两者都是 8 bit，只是结果数值不同；`wcs` 不可用时自动回退 `lcms` |
| Force RGB to CMYK | 关 | 无 CMYK 调整时也把 `setrgbcolor` / `rg` 等转到 CMYK |
| Force gray to K | 关 | 无 CMYK 调整时也把 `setgray` / `g` 转成 `0 0 0 1-g`（纯 K，不走 ICC） |
| Route neutral gray to K | 关 | 近中性 RGB 直接映射为纯 K（`K = 1 - 通道均值`），彩色内容仍走 ICC。对“中性灰/灰阶”最有效；见下 |
| └ Apply to vector content only | 开 | 只对矢量内容生效，图片像素仍走 ICC（照片不宜强行压成纯 K） |
| └ Neutral-gray threshold | 0.04 | 判定“中性”的通道最大差（0.04 ≈ 10/255） |
| └ Neutral-gray floor | 0.05 | 比这更暗的中性色保留 ICC 结果，避免暗部彩色（如深蓝黑）被压成灰 |
| Print all neutral vector text as single-channel K | 关 | 中性文字以 Separation(Black) 单通道 + 套印输出，复现 Acrobat 的网点，见下 |
| └ Text K gain | 1.20 | 文字浓度增益（0..3）：`K = clamp01(tone × gain)`，1.2 可对齐 Acrobat 的实测网点 |
| Process image pixels | 开 | 解码内嵌图像像素并逐像素转 CMYK / 调整；无 CMYK 调整时自动打开 |

**K Reduction（K 减量，可折叠）**

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| K-reduction strength (%) | 0 | 整体压低 K（`K' = K × (1 - r/100)`），用于总墨量过大、暗部糊版 |
| Enable dynamic K curve | 关 | 让减量随 K 值变化：`≤ 曲线起点` 不减，`≥ 曲线终点` 减到最多，中间按幂函数过渡 |
| Curve start K (%) | 0 | 曲线生效的起始 K |
| Curve end K (%) | 100 | 曲线达到最大减量的 K |
| Curve power | 1.0 | 过渡曲线幂次（>1 更晚减量，<1 更早减量） |
| Preserve pure black | 开 | K ≥ 阈值时完全不动，保证纯黑文字仍是实地 |
| Pure-black threshold K (%) | 95 | 上述“纯黑”的判定阈值 |

**Separation & Total Ink（分色与总量，可折叠）**

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| CMY channel scaling (C/M/Y) | 1.0 | 直接缩放 C/M/Y，用于整体校色或压某一色 |
| Channel Gamma (C/M/Y/K) | 1.0 | 逐通道 `v ** (1/gamma)`；>1 提亮该通道，<1 加深 |
| GCR | 0 | 灰成分替代：把 `min(C,M,Y)` 按比例挪到 K（总墨量不变，灰更稳） |
| UCR | 0 | 底色去除：同样把 `min(C,M,Y)` 挪到 K（常与 GCR 一起用于省墨/防透印） |
| Rich black | 0 | K 超过阈值后额外补 CMY（C 0.6 / M 0.4 / Y 0.4 比例），让大面积黑更厚实 |
| Rich-black threshold K | 0.9 | 开始补 CMY 的 K 阈值 |
| Total ink limit TAC (0 = off) | 0 | 总墨量上限（如 3.0 = 300%） |
| TAC mode | `scale_cmy` | `scale_cmy`：只等比压 CMY，尽量保住 K；`scale_all`：CMY K 一起等比压 |

**处理顺序**（`colorops.transform_cmyk`）：GCR → UCR → K 减量/K 曲线（含保留纯黑）
→ rich black → CMY 缩放 → 逐通道 gamma → TAC 限墨。

### 4.5 Preview（预览）

左「原始」右「按当前设置转换后」的软打样对比。可选内置色轮或「Choose Image…」
选一张图；支持拖拽平移与缩放。只要存在 CMYK 调整就能看到效果。

---

## 5. 颜色改写的实现细节

### 5.1 词法层

`pstokens.tokenize` 做**无损**切分（空白、注释、字符串、十六进制串、字典都保持
原样），只把“操作符 + 紧接着的操作数”作为改写单位。遇到无法识别的情况直接原样
返回全文（宁可不改，也不破坏打印）。

**内嵌图像数据必须跳过**：`colorimage` / `image` 的二进制或 ASCII85 数据里会出现
`(`、`%`、`<` 等字符，被词法器当成字符串/注释就会吞掉后续整页内容。
`imageops.image_data_spans` 先算出这些数据段的区间（ASCII85 找 `~>`、ASCIIHex 找
`>`、DCT 找 `FFD9`、RunLength 按长度、Flate 用 zlib 找到解压结束位置），
`tokenize(data, skip=spans)` 把这些区间作为不透明片段原样输出。

### 5.2 短别名

PScript5 / BR-Script3 驱动的序言里常写
`/rg/setrgbcolor load def`、`/g/setgray load def`、`/k/setcmykcolor load def`、
`/cs/setcolorspace load def`，正文只调 `rg`/`g`/`k`。引擎先扫描一遍别名表
（`engine._alias_map`），再按真实操作符处理；否则整份文档的颜色都改不到。

处理的操作符：`setcmykcolor`、`setrgbcolor`、`setgray`、`setcolor`、`sc`/`scn`/`SC`/`SCN`
（配合跟踪 `setcolorspace` 与 `[/Separation ...]` 等当前色彩空间）。

### 5.3 文字：单通道 K + 套印

只对文字绘制算子（`show` / `ashow` / `widthshow` / `awidthshow` / `kshow` /
`xshow` / `yshow` / `xyshow` / `cshow` / `glyphshow`）生效，图形与图像不受影响。

规则：

1. 取文字当前的**填充色**（文档原始值，未经我们的转换）判断是否中性
   （DeviceGray，或 RGB/CMYK 通道最大差 ≤ `neutral_threshold`）。
2. `tone = 1 - 亮度`（0 = 白，1 = 黑）；`K = min(1, tone × text_k_gain)`。
3. 已经是实地纯黑（`K=100` 且无 CMY）时跳过，不重复包裹。
4. 否则把绘制算子包成：

```postscript
gsave
[/Separation/Black/DeviceCMYK{0 0 0 4 -1 roll}]setcolorspace
<tint> setcolor
/show 操作符
/setoverprint where{pop true setoverprint}if   % 写在 setcolor 之后
grestore
```

为什么不用 `0 0 0 <tint> setcmykcolor`：那样会经打印机的 CMYK 特性化，变成四色
叠印的 rich black，笔画被四个网角打碎（“网状”）。Separation 直接落到 K 版，
只有一个网角，笔画实。套印则让文字压在图像上时不挖白边——这正是 Acrobat 的
PostScript 驱动做的事（`true sop … <tint> sep`）。

`text_k_gain` 补偿本机黑版的网点扩张；1.2 时实测网点与 Acrobat 基本一致。

### 5.4 图像像素

`imageops.rewrite_colorimages` / `rewrite_pscript_images` 解码
`colorimage` 与 `/DeviceRGB|DeviceGray|DeviceCMYK … >> image` 形式的图像，
用 Pillow + ICC（或 `wcs`）转 CMYK，再套用 `transform_cmyk` 的数值流水线，
最后按 `/DeviceCMYK` 字典形式回填。纯 Python，无 numpy；没有 CMYK 调整时只做
ICC 转换本身。

### 5.5 灰色内容

- `gray_to_k`：`g` → `0 0 0 (1-g)`，纯 K，不经 ICC（无色彩管理，但绝不偏色）。
- 近中性 RGB 由 `neutral_to_k` 处理（见 4.4），默认只作用于矢量。

---

## 6. 命令行

```powershell
python -m icc_spooler                       # 图形界面（默认入口）
python -m icc_spooler gui --config config.json [--minimized]

python -m icc_spooler watch --config config.json [--dry-run] [--printer "..."]
python -m icc_spooler process in.ps out.ps --config config.json
python -m icc_spooler sample out.ps          # 生成测试页
python -m icc_spooler init-config config.json [--force]
python -m icc_spooler install-help
python -m icc_spooler printer-admin --action check --name "Brother ICC Virtual" --port C:\ICCSpool\in.prn
```

`process` 会打印 `stats`，是验证改写是否命中的最快方式，例如：
`stats={'cmyk': 0, 'rgb': 74, 'gray': 3, 'text': 1978, 'images': 1}`。

---

## 7. 配置文件

默认 `config.json`（仓库根，界面改动约 0.8 秒后自动保存；服务运行中会自动重启以
应用新设置）。可用 `example-config.json` 作模板。

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `printer` | `Brother MFC-L3750CDW BR-Script3` | 真实队列（RAW 目标） |
| `virtual_printer` | `Brother ICC Virtual` | 虚拟队列名 |
| `virtual_port` | `C:\ICCSpool\in.prn` | 虚拟打印机的文件端口 |
| `spool_dir` | `C:\ICCSpool` | 监视目录 |
| `archive_dir` | `C:\ICCSpool\done` | 归档目录 |
| `spool_pattern` | `*.prn` | 监视文件名通配符 |
| `poll_seconds` | `1.0` | 轮询间隔 |
| `dry_run` | `false` | 只改写并归档，不投递 |
| `auto_start_service` | `true` | 打开界面时自动启动服务 |
| `autostart` | `false` | 开机自启动（HKCU `…\Run`） |
| `start_minimized` | `false` | 启动即最小化到托盘 |
| `minimize_to_tray` | `true` | 关窗口只隐藏到托盘，服务继续跑 |
| `log_file` | `""` | 日志文件；空则 `<spool_dir>\icc_spooler.log` |
| `lang` | `en` | 界面语言（`en` / `zh`），需重启 |

`color` 段：`enabled`、`k_reduction`、`k_curve_enabled` / `k_curve_min` /
`k_curve_max` / `k_curve_power`、`preserve_black`、`black_threshold`、
`cmy_scale[3]`、`c_gamma` / `m_gamma` / `y_gamma` / `k_gamma`、`gcr`、`ucr`、
`rich_black`、`rich_black_threshold`、`tac_limit`、`tac_mode`（`scale_cmy` /
`scale_all`）、`rgb_to_cmyk`、`gray_to_k`、`neutral_to_k`、`neutral_threshold`、
`neutral_min`、`neutral_vector_only`、`text_solid_black`、`text_k_gain`、
`process_images`、`target_icc`、`input_rgb_icc`、`intent`、`cmm`。

已废弃但仍能读入的字段（**不再生效**，仅为旧配置兼容）：
`text_dpi`、`text_small_size`、`text_solid_black_luma`。

---

## 8. 单实例与托盘

- 命名互斥体 `Local\IccSpooler_SingleInstance`：重复启动不会再起第二个托盘或第二个服务。
  新进程只写 `%LOCALAPPDATA%\IccSpooler\activate.signal` 后退出，已运行的实例
  收到信号后显示窗口（托盘线程已死时会自动重建）。
- 托盘菜单：显示窗口 / 启动·停止服务 / 退出。

---

## 9. 已知限制

- 只处理 **PostScript**；PDF 直打、XPS、PCL 都不在链路内。
- 驱动若输出**二进制** PostScript，词法器按字节透传，颜色可能改不到。
- 文件端口同名覆盖 → **必须顺序打印**。
- 无法从 PostScript 反解文字内容（字形名是数字），因此所有判定都基于颜色与
  绘制算子，不能按“这是标题/这是正文”区分。
- 文字规则只改字形笔画本身；`TJ` 字距、字体选择等不动。
- 无法在软件内验证真实 RIP 的网点效果；建议用 `process` + 归档文件与
  Acrobat 打印的 PS 对比来确认。

---

## 10. 组件

| 文件 | 作用 |
| --- | --- |
| `config.py` | `ColorSettings` / `AppConfig`，JSON 读写与校验 |
| `pstokens.py` | PostScript 无损词法器（含不透明数据段保护） |
| `colorops.py` | 颜色引擎：ICC（lcms/wcs）、K 减量/曲线、GCR/UCR、rich black、TAC、中性灰、文字色判定 |
| `engine.py` | 单遍改写颜色操作符与文字绘制，失败时原样返回 |
| `imageops.py` | 图像数据段定位与像素改写（`colorimage` / `image`） |
| `service.py` | 轮询 spool、改写、归档、RAW 转发 |
| `controller.py` | `ServiceController`：线程化服务 + 状态/统计/日志给界面 |
| `gui.py` | Toga 界面：服务、基本、虚拟打印机、色彩转换、预览 |
| `tray.py` / `autostart.py` / `single_instance.py` | 托盘、开机自启动、单实例唤醒 |
| `printer_admin.py` | Win32 管理虚拟打印机（`XcvData` / `AddPrinter` / `DeletePrinter`）+ UAC |
| `wcscmm.py` | `mscms.dll` 后端，供 `cmm=wcs` 使用 |
| `sample.py` | 生成 CMYK/RGB/gray 测试页 |
| `IccSpooler.pyw` | 双击 / 自启动入口 |

依赖见 `requirements.txt`：`toga`、`pystray`、`Pillow`、`pywin32`。
