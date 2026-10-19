# AGENTS.md

## 提交约定（每次改动都必须遵守）
- 完成任何改动后都要 `git commit`；一次改动一个提交，不要攒着。
- 提交信息必须写清 **改了什么** 和 **为什么改**（原因/动机）。
- 未明确要求时不要 `--amend`、不要 `--force-push`，也不要新建空提交。
- 不要提交 `__pycache__/`、`*.pyc`、`.loopx/`（已在 `.gitignore` 中忽略）。

## 项目速览
- `icc_spooler/`：Python 包，包含 GUI（Toga）、后台服务、颜色引擎、虚拟打印机管理。
- `IccSpooler.pyw`：GUI 双击入口；命令行用 `python -m icc_spooler`。
- 界面语言：源码里的英文即翻译 key；其他语言放 `icc_spooler/lang/<code>.json`，默认 `en`。
- 配置：`config.json`（GUI 改动会自动保存）。
- 目标 ICC 配置文件：`JapanColor2001Uncoated.icc`。
