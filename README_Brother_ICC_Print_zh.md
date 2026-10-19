# Brother ICC Print（PyQt 原型）

一个面向 Windows 的 PySide6 ICC 打印工具，使用现有 Brother BR-Script 打印队列，以 RAW PostScript 方式提交打印任务。

## 功能
- 支持 JPG、JPEG、PNG、TIFF、BMP、WebP 和 PDF。
- 图片路径：读取内嵌 ICC（若无则按 sRGB），转换到目标 CMYK ICC，可单独减少 K 通道。矢量 PDF 模式启用 K 减少时，先用 Ghostscript 将 PDF 颜色转换到目标 CMYK，再用 `pikepdf` 解析并修改 PDF 内容流中的 `k`/`K`、DeviceCMYK 色彩空间下的 `sc`/`SC`，以及简单的 8-bit DeviceCMYK 图像数据，最后输出 PostScript。PDF 阶段会直接处理解析后的颜色指令，不再对输出 PS 做第二次 K 减少，避免同一颜色被减少两次。
- PDF 提供两种模式：
  - **矢量优先（Ghostscript → PS）**：通过 `subprocess` 直接调用 Ghostscript 命令行程序 `gswin64c.exe` 的 `ps2write` 设备，将 PDF 转为 PostScript；文字和矢量尽可能保留。可选目标 CMYK ICC，并设置 CMYK 转换渲染意图。
  - **栅格 ICC（兼容旧方式）**：用 PyMuPDF 按指定 DPI 渲染 PDF 页面，再进行 ICC 转换和 K 通道调整。此模式会栅格化文字和矢量。
- 纸张尺寸、方向、缩放基准、10%–200% 缩放百分比、额外安全边距、介质类型、进纸来源、PDF 页码范围；Ghostscript 超时可在“集成与设置”中设置为 5、10、30 或 60 分钟。
- 可尝试请求 600×600 或 2400×600，但打印机可能忽略通用 PostScript 的 HWResolution 请求。
- 可在程序中添加/移除当前 Windows 用户的图片/PDF 右键菜单。

## 安装与运行
1. 安装 64 位 Python（建议 Python 3.11 或 3.12）。
2. 安装 **Ghostscript for Windows 64-bit**。程序直接调用 Ghostscript 命令行程序，不需要 Python `ghostscript` 绑定。建议把 Ghostscript 的 `bin` 目录加入 Windows `PATH`。
3. 将 `brother_icc_print_app.py`、`requirements.txt` 放在同一目录。
4. 在该目录打开 PowerShell：
   ```powershell
   py -m pip install -r requirements.txt
   py .\brother_icc_print_app.py
   ```
5. 选择目标 CMYK ICC 和 Brother BR-Script 队列，先用一份简单 PDF 测试。

程序会先从 Windows `PATH` 查找 `gswin64c.exe`，也会尝试常见安装目录。如果没有找到，请将 Ghostscript 的 `bin` 目录加入 `PATH`。

## 重要限制
- **矢量优先模式的 K 减少使用 PDF 对象级后处理（pikepdf），但仍不是对所有 PDF 对象的完整覆盖保证。** 启用 K 减少时，Ghostscript 先将输入 PDF 转换到目标 CMYK 输出空间；随后 pikepdf 解析 PDF 内容流，修改 `k`/`K`、DeviceCMYK 色彩空间下的 `sc`/`SC`，并尝试处理简单的 8-bit DeviceCMYK 图像 XObject。压缩的 PDF 内容流由 pikepdf 解码后解析，不依赖在最终 PS 文件中搜索明文 `k/K`。之后 Ghostscript 输出 PostScript 时使用 `LeaveColorUnchanged`，避免再执行一次输出 ICC 分色而改变 C/M/Y；最终 PS 阶段不重复修改 K，以免双重减少。该流程不主动把整页栅格化，通常能保留大部分矢量对象。
- 目前不能保证覆盖 ICCBased、Separation/DeviceN 专色、Indexed 色彩空间、带复杂 Decode/DecodeParms 的图像、1/2/4/16-bit 图像、Shading、Pattern、透明度合成及特殊嵌套对象。非 DeviceCMYK 的图像若在第一阶段被转换成 ICCBased CMYK，也可能不被图像处理器修改。PDF 里的 K/k 操作数按 CMYK 数值乘以对应比例（例如减少 30% 时 K 乘以 0.7）；此操作不保持视觉颜色不变，因为 C/M/Y 不变。程序会在没有确认修改到任何 K 值或受支持图像时显示警告，但这不代表打印任务一定失败。请先保存 PS，用包含 CMYK 矢量色块、文字、嵌入 CMYK 图片、渐变、图案和透明度的测试 PDF 验证。
- Ghostscript `ps2write` 使用 `-dNoOutputFonts`，将文字转换为矢量轮廓/必要时的位图，以规避某些字体在 PostScript 输出或打印机解释时消失的问题。文字通常不再是可搜索/可编辑的字体对象，文件可能变大、转换变慢；这仍不是所有 PDF 都能保证成功的修复。透明度、复杂颜色空间、专色和部分 PDF 特性可能被转换或扁平化。
- Ghostscript 的目标 ICC 转换并不保证对所有 PDF 颜色对象产生相同结果。嵌入的 CMYK、专色、透明度和 PDF Output Intent 都可能影响结果；请用有代表性的文件做对比测试。
- 矢量优先模式先使用 Ghostscript 的 `-dPDFFitPage` 将 PDF 页面适配到所选纸张，然后在生成的 PostScript 每页内容上执行居中缩放。界面中的「缩放比例」与「额外安全边距」现在会实际作用于 RAW PS 输出。程序固定按打印机四边各 4 mm 不可打印区计算；额外安全边距默认 0 mm。如果某份 PS 的页面结构不符合预期，程序会报错而不会静默跳过变换。纸张类型/纸盒等参数仍可能受 PDF 内部 PostScript 页面设置影响。
- 这是原型版，尚未在用户的 Windows 电脑与 Brother 打印机上实际验证 Ghostscript DLL 加载、PDF 转换和最终打印效果。
- 已经是 CMYK 的图片会保留现有 CMYK 通道，不会再次应用目标 ICC。
- 目标 ICC 应是适合 CMYK 输出的配置文件。通用印刷 ICC 不等同于 Brother MFC-L3750CDW 对特定纸张建立的设备 ICC。
- `/HWResolution` 仅为实验性请求，不能保证启用 Brother 驱动中的专有 2400×600 增强质量模式。
- 如果将程序或 Ghostscript 打包后再分发，需要检查 Python 绑定及 Ghostscript 的 GPL/AGPL 或商业许可要求。

## 右键菜单
在“集成与设置”中勾选后点击“保存设置”，即可尝试添加右键菜单。取消勾选并保存即可移除此程序创建的菜单项。菜单只对当前 Windows 用户生效。若使用源码运行，右键命令依赖 Python 路径和脚本位置；打包为 EXE 后应先放在固定目录，再启用菜单。

## 打包 EXE（可选）
```powershell
py -m pip install pyinstaller
py -m PyInstaller --noconsole --onefile --name BrotherICCPrint brother_icc_print_app.py
```
生成文件位于 `dist\BrotherICCPrint.exe`。这条命令不会自动替你处理 Ghostscript 原生 DLL 和许可问题。

## 新增：纸张方向与版面预览

- 「纸张方向」支持自动（按内容）、纵向、横向。
- 预览区会跟随方向选项旋转页面内容。纸张四周灰色区域代表固定的 4 mm 不可打印区，虚线框表示可打印边界；额外安全边距会进一步缩小内容区域。选择 PDF 时会预览所选页码范围中的第一页。
- 图片和栅格 PDF 模式会按照预览方向生成相应纸张尺寸的 PostScript 页面。
- PDF 矢量优先模式会请求 Ghostscript `ps2write` 使用所选纸张尺寸和方向；但源 PDF 的页面尺寸、旋转标记、裁切框及复杂内容会影响结果。该模式的预览是排版近似图，不是对最终 PostScript 的逐像素模拟。第一次使用横向/纵向强制方向时，建议先打印一页测试。
- 预览不是 ICC 软打样，显示颜色不代表打印机最终颜色。

- 「缩放比例」控制（10%–200%）会应用到图片、栅格 PDF 和矢量 PDF 的实际输出。矢量 PDF 在 Ghostscript 转换后，对每页内容执行居中缩放；固定的 4 mm 不可打印区与额外安全边距都会参与缩放计算。预览是排版近似图，不是对最终 PostScript 的逐像素模拟。


### PostScript 文件保存位置

在「纸张与打印」标签页中可以设置「PS 保存位置」：留空时默认保存到每个源文件所在的文件夹；指定目录后，生成的 PS 文件统一保存到该目录。点击「默认」可清空自定义路径并恢复保存到源文件旁边。取消勾选「保存生成的 PostScript 文件」后，PS 只会临时生成用于打印，提交后删除。


### 高级：手动调整打印机不可打印区域

在“纸张与打印”标签页中展开“高级选项：手动调整打印机不可打印区域”，可以分别设置左、上、右、下的不可打印宽度（毫米）。默认四边均为 4 mm。设置会用于预览、可打印区域缩放计算，以及最终生成的 PostScript；“额外安全边距”会在四边设定值的基础上继续增加。修改后请点击“保存设置”。


### K 通道纯黑保护

在“颜色管理”中可勾选“保留纯黑/近纯黑”，并设置“K 纯黑保护阈值”。例如阈值为 95% 时，K≥95% 的颜色保持不变；低于 95% 的 K 才按“K 通道减少”设置缩减。该规则用于保护接近纯黑的 CMYK 颜色，避免将 100% K 黑色改成灰黑。取消勾选后，所有可识别的 K 值都按设定比例减少。

注意：该阈值针对 CMYK 的第四个分量 K，不是判断 C/M/Y/K 是否组合成中性黑；复杂颜色空间和特殊对象仍可能不受此规则控制。

## 动态 K 曲线（按原始 K 的比例减少）

在“颜色管理”中可以勾选“显示 K 减少比例曲线图”来展开实时图表，再次取消勾选即可隐藏。图表以**原始 K（%）为 X 轴**、实际减少比例（%）为 Y 轴；调整最大减少比例、曲线范围、曲线指数或纯黑保护选项时，曲线会立即更新。图表会把纯黑保护阈值体现为对应 K 区域减少比例降为 0。

在“颜色管理”中勾选“启用动态 K 曲线”后，程序仍然按**原始 K 的比例**减少，而不是减去固定百分点。公式为：

`最终 K = 原始 K × (1 − 实际减少比例)`

- “K 通道减少”是曲线低端的最大比例减少值。例如设置 30%，表示最多把原始 K 减少 30%，不是减去 30 个百分点。
- 原始 K 小于或等于“曲线范围：K 最小值”：使用最大比例减少值。
- 原始 K 位于最小值和最大值之间：比例减少值随 K 增大而逐渐下降。
- 原始 K 大于或等于“曲线范围：K 最大值”：曲线减少比例为 0。
- “曲线指数”控制曲线形状：1.0 为线性；小于 1 时减少比例更早下降；大于 1 时减少比例在较高 K 区域下降得更慢。
- “保留纯黑/近纯黑”仍优先生效；达到纯黑保护阈值的 K 不修改。关闭动态曲线时，使用固定比例减少。
- 为避免把非零 K 完全减为 0，最大减少比例限制为 99.9%；在 8 位栅格图像中，非零 K 还会至少保留 1/255。原始 K=0 仍保持为 0。

例如：最大减少比例 30%、曲线范围 0–100% K、指数 1.0，且关闭纯黑保护时：K=20% 时减少比例为 24%，最终 K=15.2%；K=40% 时减少比例为 18%，最终 K=32.8%；K=60% 时减少比例为 12%，最终 K=52.8%；K=80% 时减少比例为 6%，最终 K=75.2%；K=100% 时曲线减少比例为 0，最终 K=100%。

注意：该曲线只影响已被当前处理流程识别到的 K 分量；它不会自动覆盖所有 ICCBased、专色、复杂图像编码、Pattern/Shading 或透明度路径。


## 本次修正说明

- 中文/非 ASCII 文件名会在 PostScript `%%Title` 中转换为八进制转义，避免 ASCII 编码异常。图片数据使用 ASCII85 而非十六进制，减少 PS 文件体积；在 `colorimage` 前显式设置 `/DeviceCMYK`。
- 修复页码范围解析：`abc`、`1-`、空页码段等会提示“页码格式无效”，不会显示裸 `ValueError`。矢量模式在未启用 K 减少时，会把选定页码传给最终 Ghostscript 阶段；启用 K 减少时，页码范围只在生成中间 PDF 时应用，避免二次筛选造成页码错位。
- `pikepdf` 现在也会处理 PDF 内容流中的 `k`/`K` 运算符；图片 K 映射使用查找表，图片转换也使用 256 项 LUT，减少逐像素 Python 调用。移除了不再使用的 PS K 减少包装器和 PS 文本流补丁，避免双重减少。
- 统一使用 `import pymupdf`，预览和栅格转换使用上下文管理器关闭 PDF 文件。Ghostscript 单阶段超时现在可在“集成与设置”中选择 5、10、30 或 60 分钟。右键菜单状态会检查全部支持的扩展名。动态 K 曲线最小值/最大值保持有效范围。已对 CMYK 输入增加提示，提醒先验证由 Pillow 导出的 CMYK TIFF 通道约定。
- 本次完成 Python 语法检查，并用 Ghostscript 10.05.1 对含中文标题、ASCII85 图像数据及 `/DeviceCMYK setcolorspace` 的生成 PS 做了解析测试；页码范围输入也做了最小测试。当前执行环境没有安装 `pikepdf` 和 PySide6，因此尚未实测 pikepdf 颜色修改及完整 GUI/打印流程。
