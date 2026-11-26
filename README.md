# STM32 Keil 基础工程生成器

一个在本机运行的 Windows 桌面工具，用于从已安装的 STM32Cube SDK 和 Keil Device Family Pack（DFP）生成最小 STM32 HAL Keil 工程。工程 XML 和 scatter 文件由程序生成，不需要或附带 Keil 工程模板。

## 功能

- 扫描标准 STM32Cube SDK 中可识别的 STM32 HAL 系列。
- 扫描本机已安装的 Keil DFP，并列出器件、SVD 和 Flash 算法文件齐全的芯片。
- 根据 SDK、DFP 和启动模板识别器件宏、启动文件、CPU 核心及存储器信息。
- 创建包含 CMSIS、HAL、用户文件、Keil 工程和输出目录的基础工程。
- 生成 Keil 工程配置及 linker scatter 文件，并校验工程引用的文件。
- 可选调用本机 Keil uVision 编译。
- 默认生成 VS Code GBK 编码设置，便于查看 Keil 工程中的中文源文件。
- 不覆盖非空目标目录；生成失败时清理临时工程。

## 环境要求

- Windows
- Python 3.10 或更高版本（从源码运行或构建时需要）
- 对应芯片的 STM32Cube SDK 和 Keil DFP
- Keil MDK/uVision（仅在需要编译时需要）

SDK、DFP 和 Keil 编译器由用户本机提供，本项目不包含或重新分发这些文件。

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python main.py
```

## 构建 Windows 可执行文件

```powershell
.\build_windows.ps1
```

脚本会创建本地虚拟环境、安装构建依赖，并生成 `dist\STM32KeilProjectCreator.exe`。

## 使用

1. 选择已解压的 STM32Cube SDK 根目录并扫描。
2. 选择识别出的芯片系列和具体型号。
3. 选择一个新建或空的目标目录，并设置工程名称。
4. 创建工程；如本机已安装 Keil，可选择创建后调用 uVision 编译。

DFP 通常从 Keil 安装目录或常见 Packs 目录自动发现。未安装完整 DFP 时，先通过 Keil Pack Installer 安装对应器件包，再重新扫描。

## 生成工程的目录分层

```text
ProjectRoot/
├── Drivers/
│   ├── BSP/                         # 板级支持代码预留目录
│   ├── SYSTEM/                      # 系统代码预留目录
│   └── Platform/
│       ├── CMSIS/
│       │   ├── Include/             # CMSIS 核心头文件
│       │   └── Device/ST/<Family>/
│       │       ├── Include/         # 芯片寄存器及设备头文件
│       │       └── Source/Templates/ # system_<family>.c 和启动文件
│       └── <Family>_HAL_Driver/
│           ├── Inc/                 # HAL 头文件
│           └── Src/                 # HAL 源文件
├── Application/                     # 应用层代码预留目录
├── Middlewares/                     # 中间件预留目录
├── Utilities/                       # 工具代码预留目录
├── Users/
│   ├── main.c
│   ├── <family>_hal_conf.h
│   └── <family>_it.c
├── Project/
│   └── MDK-ARM/
│       └── <project>.uvprojx
├── Output/                          # scatter 文件和编译输出
│   └── <project>.sct
├── .vscode/
│   └── settings.json                # 默认 GBK 源文件编码
└── readme.txt
```

`BSP`、`SYSTEM`、`Application`、`Middlewares` 和 `Utilities` 当前是供后续扩展的空目录，不代表生成器已经为这些层提供驱动或应用实现。Keil 工程按 `Startup`、`User`、`Drivers/<Family>_HAL_Driver` 和 `Readme` 分组；分组是工程视图，不一定与磁盘目录逐项对应。

## 兼容性说明

芯片能否列出取决于本机 SDK 与 DFP 是否符合工具识别的布局。当前生成器按标准 STM32Cube HAL 目录布局处理，并要求器件具有单一连续 Flash 区域和主 RAM 区域；多存储区域或非标准布局会被拒绝。

能够被扫描或生成工程，不代表该芯片已在所有板卡和时钟配置上经过实测。生成结果仍需使用目标芯片对应的 SDK/DFP 编译，并在实际硬件上验证。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 许可证

本项目源码以 [MIT License](LICENSE) 发布。STM32Cube SDK、Keil DFP、Keil MDK 及其他第三方组件各自适用其原有许可证。
