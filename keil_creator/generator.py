from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .discovery import DeviceBuildConfig, DiscoveryError, SdkFamily


_ENABLED_MODULES = {
    "HAL_CORTEX_MODULE_ENABLED",
    "HAL_DMA_MODULE_ENABLED",
    "HAL_FLASH_MODULE_ENABLED",
    "HAL_GPIO_MODULE_ENABLED",
    "HAL_PWR_MODULE_ENABLED",
    "HAL_RCC_MODULE_ENABLED",
    "HAL_UART_MODULE_ENABLED",
    "HAL_USART_MODULE_ENABLED",
}


class GenerationError(RuntimeError):
    """Raised when a project cannot be generated or validated."""


def _find_uv4() -> Path | None:
    from .discovery import find_uv4

    return find_uv4()


def _find_armclang(uv4: Path | None) -> tuple[str, str] | None:
    if not uv4:
        return None
    keil_root = uv4.parent.parent
    candidates = (
        keil_root / "ARM" / "ARMCLANG" / "bin" / "armclang.exe",
        keil_root / "ARM" / "ARMCLANG" / "Bin" / "armclang.exe",
    )
    for compiler in candidates:
        if not compiler.is_file():
            continue
        try:
            result = subprocess.run(
                [str(compiler), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"Arm Compiler for Embedded\s+(\d+)\.(\d+)", text)
        if match:
            major, minor = map(int, match.groups())
            version_id = major * 1_000_000 + minor * 10_000
            return f"{version_id}::V{major}.{minor}::ARMCLANG", f"{major}.{minor}"
    return None


def _hex(value: int) -> str:
    return f"0x{value:X}"


def _windows_relative(*parts: str) -> str:
    return "\\".join(parts)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise GenerationError(f"SDK 目录不存在：{source}")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _required_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise GenerationError(f"{description}不存在：{path}")
    return path


def _append_project_file(
    files_element: ET.Element,
    name: str,
    file_type: int,
    relative_path: str,
) -> None:
    file_node = ET.SubElement(files_element, "File")
    ET.SubElement(file_node, "FileName").text = name
    ET.SubElement(file_node, "FileType").text = str(file_type)
    ET.SubElement(file_node, "FilePath").text = relative_path


def _patch_hal_configuration(source: Path, destination: Path) -> None:
    text = source.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r"^\s*(?:/\*\s*)?#define\s+(HAL_[A-Z0-9_]+_MODULE_ENABLED)\s*(?:\*/)?\s*$",
        re.MULTILINE,
    )
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        module = match.group(1)
        found.add(module)
        return f"#define {module}" if module in _ENABLED_MODULES else f"/* #define {module} */"

    text = pattern.sub(replace, text)
    missing = _ENABLED_MODULES - found
    if missing:
        raise GenerationError(
            "该 HAL 配置模板缺少必需模块宏：" + ", ".join(sorted(missing))
        )
    destination.write_text(text, encoding="utf-8", newline="")


def _copy_sdk_files(
    sdk_root: Path,
    project_root: Path,
    family: SdkFamily,
    config: DeviceBuildConfig,
) -> None:
    device_root = (
        sdk_root
        / "Drivers"
        / "CMSIS"
        / "Device"
        / "ST"
        / family.name
    )
    cmsis_source = sdk_root / "Drivers" / "CMSIS"
    hal_source = sdk_root / "Drivers" / family.hal_dir_name
    family_destination = project_root / "Drivers" / "Platform" / "CMSIS" / "Device" / "ST" / family.name
    cmsis_destination = project_root / "Drivers" / "Platform" / "CMSIS"
    hal_destination = project_root / "Drivers" / "Platform" / family.hal_dir_name

    (project_root / "Drivers" / "BSP").mkdir(parents=True, exist_ok=True)
    (project_root / "Drivers" / "SYSTEM").mkdir(parents=True, exist_ok=True)
    (project_root / "Application").mkdir(parents=True, exist_ok=True)
    (project_root / "Middlewares").mkdir(parents=True, exist_ok=True)
    (project_root / "Utilities").mkdir(parents=True, exist_ok=True)
    (project_root / "Users").mkdir(parents=True, exist_ok=True)
    (project_root / "Output").mkdir(parents=True, exist_ok=True)
    (project_root / "Project" / "MDK-ARM").mkdir(parents=True, exist_ok=True)

    _copy_tree(cmsis_source / "Include", cmsis_destination / "Include")
    _copy_tree(device_root / "Include", family_destination / "Include")
    _copy_tree(
        device_root / "Source" / "Templates" / "arm",
        family_destination / "Source" / "Templates" / "arm",
    )
    _copy_tree(hal_source / "Inc", hal_destination / "Inc")
    _copy_tree(hal_source / "Src", hal_destination / "Src")

    system_source = _required_file(
        device_root / "Source" / "Templates" / config.system_file,
        "CMSIS system 源文件",
    )
    shutil.copy2(
        system_source,
        family_destination / "Source" / "Templates" / config.system_file,
    )
    shutil.copy2(
        _required_file(
            device_root / "Source" / "Templates" / "arm" / config.startup_file,
            "Keil 启动文件",
        ),
        family_destination / "Source" / "Templates" / "arm" / config.startup_file,
    )
    hal_configuration = _required_file(
        hal_source / "Inc" / f"{family.name.lower()}_hal_conf_template.h",
        "HAL 配置模板",
    )
    _patch_hal_configuration(
        hal_configuration,
        project_root / "Users" / f"{family.name.lower()}_hal_conf.h",
    )


def _create_keil_project(
    project_root: Path,
    family: SdkFamily,
    config: DeviceBuildConfig,
    project_name: str,
) -> Path:
    project_path = project_root / "Project" / "MDK-ARM" / f"{project_name}.uvprojx"
    device = config.device
    project = ET.Element("Project")
    ET.SubElement(project, "SchemaVersion").text = "2.1"
    ET.SubElement(project, "Header").text = "STM32 Keil project generated by Keil Auto Project Creator"
    targets = ET.SubElement(project, "Targets")
    target = ET.SubElement(targets, "Target")
    ET.SubElement(target, "TargetName").text = project_name
    ET.SubElement(target, "ToolsetNumber").text = "0x4"
    ET.SubElement(target, "ToolsetName").text = "ARM-ADS"
    ET.SubElement(target, "pArmCC").text = ""
    ET.SubElement(target, "pCCUsed").text = ""
    ET.SubElement(target, "uAC6").text = "1"
    target_option = ET.SubElement(target, "TargetOption")
    common = ET.SubElement(target_option, "TargetCommonOption")
    for tag, value in (
        ("Device", device.name),
        ("Vendor", "STMicroelectronics"),
        ("PackID", device.pack_id),
        ("PackURL", "https://www.keil.com/pack/"),
        ("FlashUtilSpec", ""),
        ("StartupFile", ""),
        ("FlashDriverDll", ""),
        ("DeviceId", ""),
        ("RegisterFile", ""),
        ("MemoryEnv", ""),
        ("Cmp", ""),
        ("Asm", ""),
        ("Linker", ""),
        ("OHString", ""),
        ("InfinionOptionDll", ""),
        ("SLE66CMisc", ""),
        ("SLE66AMisc", ""),
        ("SLE66LinkerMisc", ""),
        ("Cpu", (
            f'IRAM({_hex(device.ram_base)}-{_hex(device.ram_base + device.ram_size - 1)}) '
            f'IROM({_hex(device.flash_base)}-{_hex(device.flash_base + device.flash_size - 1)}) '
            f'CPUTYPE("{config.core}")'
        )),
        ("SFDFile", ""),
        ("bCustSvd", "0"),
        ("UseEnv", "0"),
        ("IncludePath", ""),
        ("LibPath", ""),
        ("RegisterFilePath", ""),
        ("DBRegisterFilePath", ""),
        ("OutputDirectory", "..\\..\\Output\\"),
        ("OutputName", project_name),
        ("CreateExecutable", "1"),
        ("CreateLib", "0"),
        ("CreateHexFile", "0"),
        ("DebugInformation", "1"),
        ("BrowseInformation", "1"),
        ("ListingPath", "..\\..\\Output\\"),
        ("HexFormatSelection", "1"),
        ("Merge32K", "0"),
        ("CreateBatchFile", "0"),
    ):
        ET.SubElement(common, tag).text = value
    for step, prefix in (
        ("BeforeCompile", "U"),
        ("BeforeMake", "B"),
        ("AfterMake", "A"),
    ):
        action = ET.SubElement(common, step)
        ET.SubElement(action, "RunUserProg1").text = "0"
        ET.SubElement(action, "RunUserProg2").text = "0"
        ET.SubElement(action, "UserProg1Name").text = ""
        ET.SubElement(action, "UserProg2Name").text = ""
        ET.SubElement(action, "UserProg1Dos16Mode").text = "0"
        ET.SubElement(action, "UserProg2Dos16Mode").text = "0"
        ET.SubElement(action, f"nStop{prefix}1X").text = "0"
        ET.SubElement(action, f"nStop{prefix}2X").text = "0"
    target_status = ET.SubElement(common, "TargetStatus")
    ET.SubElement(target_status, "InvalidFlash").text = "0"
    svd_suffix = device.svd_relative.removeprefix("SVD/").replace("/", "\\")
    common.find("SFDFile").text = (
        f"$$Device:{device.name}$SVD\\{svd_suffix}" if device.svd_relative else ""
    )
    target_arm_ads = ET.SubElement(target_option, "TargetArmAds")
    arm_misc = ET.SubElement(target_arm_ads, "ArmAdsMisc")
    for tag, value in (
        ("GenerateListings", "0"),
        ("asHll", "1"),
        ("asAsm", "1"),
        ("asMacX", "1"),
        ("asSyms", "1"),
        ("asFals", "1"),
        ("asDbgD", "1"),
        ("asForm", "1"),
        ("ldLst", "0"),
        ("ldmm", "1"),
        ("ldXref", "1"),
        ("BigEnd", "0"),
        ("AdsALst", "1"),
        ("AdsACrf", "1"),
        ("AdsANop", "0"),
        ("AdsANot", "0"),
        ("AdsLLst", "1"),
        ("AdsLmap", "1"),
        ("AdsLcgr", "1"),
        ("AdsLsym", "1"),
        ("AdsLszi", "1"),
        ("AdsLtoi", "1"),
        ("AdsLsun", "1"),
        ("AdsLven", "1"),
        ("AdsLsxf", "1"),
        ("RvctClst", "0"),
        ("GenPPlst", "0"),
        ("AdsCpuType", f'"{config.core}"'),
        ("RvctDeviceName", ""),
        ("mOS", "0"),
        ("uocRom", "0"),
        ("uocRam", "0"),
        ("hadIROM", "1"),
        ("hadIRAM", "1"),
        ("hadXRAM", "0"),
        ("uocXRam", "0"),
        ("RvdsVP", "0"),
        ("RvdsMve", "0"),
        ("RvdsCdeCp", "0"),
        ("nBranchProt", "0"),
        ("hadIRAM2", "0"),
        ("hadIROM2", "0"),
        ("StupSel", "8"),
        ("useUlib", "0"),
        ("EndSel", "0"),
        ("uLtcg", "0"),
        ("nSecure", "0"),
        ("RoSelD", "3"),
        ("RwSelD", "3"),
        ("CodeSel", "0"),
        ("OptFeed", "0"),
        ("NoZi1", "0"),
        ("NoZi2", "0"),
        ("NoZi3", "0"),
        ("NoZi4", "0"),
        ("NoZi5", "0"),
        ("Ro1Chk", "0"),
        ("Ro2Chk", "0"),
        ("Ro3Chk", "0"),
        ("Ir1Chk", "1"),
        ("Ir2Chk", "0"),
        ("Ra1Chk", "0"),
        ("Ra2Chk", "0"),
        ("Ra3Chk", "0"),
        ("Im1Chk", "1"),
        ("Im2Chk", "0"),
    ):
        ET.SubElement(arm_misc, tag).text = value
    memories = ET.SubElement(arm_misc, "OnChipMemories")
    memory_defaults = [
        (f"Ocm{index}", 0, 0, 0) for index in range(1, 7)
    ] + [
        ("IRAM", device.ram_base, device.ram_size, 0),
        ("IROM", device.flash_base, device.flash_size, 1),
        ("XRAM", 0, 0, 0),
        *[
            (
                f"OCR_RVCT{index}",
                device.flash_base if index == 4 else (
                    device.ram_base if index == 9 else 0
                ),
                device.flash_size if index == 4 else (
                    device.ram_size if index == 9 else 0
                ),
                1 if index <= 5 else 0,
            )
            for index in range(1, 11)
        ],
    ]
    for name, start, size, memory_type in memory_defaults:
        memory = ET.SubElement(memories, name)
        ET.SubElement(memory, "Type").text = str(memory_type)
        ET.SubElement(memory, "StartAddress").text = _hex(start)
        ET.SubElement(memory, "Size").text = _hex(size)

    cads = ET.SubElement(target_arm_ads, "Cads")
    for tag, value in (
        ("interw", "1"),
        ("Optim", "2"),
        ("oTime", "0"),
        ("uC99", "1"),
        ("v6Lang", "3"),
    ):
        ET.SubElement(cads, tag).text = value
    for tag, value in (
        ("SplitLS", "0"),
        ("OneElfS", "1"),
        ("Strict", "0"),
        ("EnumInt", "0"),
        ("PlainCh", "0"),
        ("Ropi", "0"),
        ("Rwpi", "0"),
        ("wLevel", "3"),
        ("uThumb", "0"),
        ("uSurpInc", "0"),
        ("uGnu", "0"),
        ("useXO", "0"),
        ("v6LangP", "3"),
        ("vShortEn", "1"),
        ("vShortWch", "1"),
        ("v6Lto", "0"),
        ("v6WtE", "0"),
        ("v6Rtti", "0"),
    ):
        ET.SubElement(cads, tag).text = value
    various = ET.SubElement(cads, "VariousControls")
    ET.SubElement(various, "MiscControls").text = ""
    ET.SubElement(various, "Define").text = f"USE_HAL_DRIVER,{config.define}"
    ET.SubElement(various, "Undefine").text = ""
    includes = [
        rf"..\..\Drivers\Platform\CMSIS\Device\ST\{family.name}\Include",
        r"..\..\Drivers\Platform\CMSIS\Include",
        rf"..\..\Drivers\Platform\{family.hal_dir_name}\Inc",
        r"..\..\Users",
    ]
    ET.SubElement(various, "IncludePath").text = ";".join(includes)
    aads = ET.SubElement(target_arm_ads, "Aads")
    for tag, value in (
        ("interw", "1"),
        ("Ropi", "0"),
        ("Rwpi", "0"),
        ("thumb", "0"),
        ("SplitLS", "0"),
        ("SwStkChk", "0"),
        ("NoWarn", "0"),
        ("uSurpInc", "0"),
        ("useXO", "0"),
        ("ClangAsOpt", "4"),
    ):
        ET.SubElement(aads, tag).text = value
    assembler_controls = ET.SubElement(aads, "VariousControls")
    for tag in ("MiscControls", "Define", "Undefine", "IncludePath"):
        ET.SubElement(assembler_controls, tag).text = ""
    ldads = ET.SubElement(target_arm_ads, "LDads")
    for tag, value in (
        ("umfTarg", "1"),
        ("Ropi", "0"),
        ("Rwpi", "0"),
        ("noStLib", "0"),
        ("RepFail", "1"),
        ("useFile", "1"),
        ("TextAddressRange", _hex(device.flash_base)),
        ("DataAddressRange", _hex(device.ram_base)),
        ("ScatterFile", rf"..\..\Output\{project_name}.sct"),
        ("pXoBase", ""),
        ("Misc", "--diag_suppress=L6329"),
        ("IncludeLibs", ""),
        ("IncludeLibsPath", ""),
        ("LinkerInputFile", ""),
        ("DisabledWarnings", ""),
    ):
        ET.SubElement(ldads, tag).text = value

    compiler = _find_armclang(_find_uv4())
    if compiler:
        target.find("pArmCC").text = compiler[0]
        target.find("pCCUsed").text = compiler[0]

    groups = ET.SubElement(target, "Groups")
    def add_group(name: str) -> ET.Element:
        group = ET.SubElement(groups, "Group")
        ET.SubElement(group, "GroupName").text = name
        return ET.SubElement(group, "Files")

    startup_files = add_group("Startup")
    user_files = add_group("User")
    hal_files = add_group(f"Drivers/{family.hal_dir_name}")
    readme_files = add_group("Readme")

    _append_project_file(
        startup_files,
        config.startup_file,
        2,
        _windows_relative(
            "..",
            "..",
            "Drivers",
            "Platform",
            "CMSIS",
            "Device",
            "ST",
            family.name,
            "Source",
            "Templates",
            "arm",
            config.startup_file,
        ),
    )
    _append_project_file(
        user_files,
        config.interrupt_file,
        1,
        rf"..\..\Users\{config.interrupt_file}",
    )
    _append_project_file(user_files, "main.c", 1, r"..\..\Users\main.c")
    _append_project_file(
        user_files,
        config.system_file,
        1,
        _windows_relative(
            "..",
            "..",
            "Drivers",
            "Platform",
            "CMSIS",
            "Device",
            "ST",
            family.name,
            "Source",
            "Templates",
            config.system_file,
        ),
    )
    for source_name in config.hal_sources:
        _append_project_file(
            hal_files,
            source_name,
            1,
            rf"..\..\Drivers\Platform\{family.hal_dir_name}\Src\{source_name}",
        )
    _append_project_file(readme_files, "readme.txt", 5, r"..\..\readme.txt")

    project_path.parent.mkdir(parents=True, exist_ok=True)
    scatter_path = project_root / "Output" / f"{project_name}.sct"
    scatter_path.parent.mkdir(parents=True, exist_ok=True)
    scatter_path.write_text(
        f"""LR_IROM1 { _hex(device.flash_base) } { _hex(device.flash_size) } {{
    ER_IROM1 { _hex(device.flash_base) } { _hex(device.flash_size) } {{
        *.o (RESET, +First)
        *(InRoot$$Sections)
        .ANY (+RO)
        .ANY (+XO)
    }}
    RW_IRAM1 { _hex(device.ram_base) } { _hex(device.ram_size) } {{
        .ANY (+RW +ZI)
    }}
}}
""",
        encoding="ascii",
        newline="",
    )
    ET.ElementTree(project).write(
        project_path,
        encoding="UTF-8",
        xml_declaration=True,
    )
    return project_path


def _validate_project(project_path: Path, config: DeviceBuildConfig) -> None:
    try:
        root = ET.parse(project_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GenerationError(f"生成的 Keil 工程 XML 无法解析：{exc}") from exc
    target = root.find("./Targets/Target")
    if target is None:
        raise GenerationError("生成的 Keil 工程缺少 Target。")
    missing: list[str] = []
    for file_node in target.findall("./Groups/Group/Files/File"):
        relative = file_node.findtext("FilePath", default="")
        resolved = (project_path.parent / Path(relative.replace("\\", os.sep))).resolve()
        if not resolved.is_file():
            missing.append(str(resolved))
    if missing:
        raise GenerationError("工程引用的文件缺失：\n" + "\n".join(missing))
    common = target.find("./TargetOption/TargetCommonOption")
    if common is None or common.findtext("Device") != config.device.name:
        raise GenerationError("生成工程的芯片型号校验失败。")
    if common.findtext("PackID") != config.device.pack_id:
        raise GenerationError("生成工程的 DFP Pack ID 校验失败。")
    arm_ads = target.find("./TargetOption/TargetArmAds")
    if arm_ads is None:
        raise GenerationError("生成工程缺少编译器设置。")
    cads = arm_ads.find("Cads")
    arm_misc = arm_ads.find("ArmAdsMisc")
    if (
        common.findtext("CreateExecutable") != "1"
        or common.findtext("CreateHexFile") != "0"
        or common.findtext("DebugInformation") != "1"
        or common.findtext("BrowseInformation") != "1"
        or cads is None
        or cads.findtext("Optim") != "2"
        or cads.findtext("oTime") != "0"
        or arm_misc is None
        or arm_misc.findtext("useUlib") != "0"
    ):
        raise GenerationError("Keil 工程选项未通过默认配置校验。")


def generate_project(
    *,
    family: SdkFamily,
    config: DeviceBuildConfig,
    destination: Path,
    project_name: str,
    template_dir: Path,
    build_after_generation: bool = False,
) -> tuple[Path, str]:
    sdk_root = family.root.resolve()
    destination = destination.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project_name):
        raise GenerationError("工程名仅支持英文字母、数字、下划线和连字符。")
    if destination == sdk_root or sdk_root in destination.parents:
        raise GenerationError("工程目录不能是 SDK 目录本身或其子目录。")
    if destination.exists() and any(destination.iterdir()):
        raise GenerationError(f"目标目录不是空目录，拒绝覆盖：{destination}")
    if not destination.parent.is_dir():
        destination.parent.mkdir(parents=True, exist_ok=True)

    main_template = _required_file(template_dir / "main.c", "main.c 模板")
    interrupt_template = _required_file(template_dir / "stm32xx_it.c", "中断文件模板")
    readme_template = _required_file(template_dir / "readme.txt", "README 模板")

    stage_path = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.creating-", dir=destination.parent)
    )
    # mkdtemp creates the stage directory; all generation happens there so a
    # failed operation never leaves a half-built destination tree.
    try:
        _copy_sdk_files(sdk_root, stage_path, family, config)
        family_lower = family.name.lower()
        (stage_path / "Users" / "main.c").write_text(
            main_template.read_text(encoding="utf-8").replace(
                "{{HAL_HEADER}}", f"{family_lower}_hal.h"
            ),
            encoding="ascii",
            newline="",
        )
        (stage_path / "Users" / config.interrupt_file).write_text(
            interrupt_template.read_text(encoding="utf-8").replace(
                "{{HAL_HEADER}}", f"{family_lower}_hal.h"
            ),
            encoding="ascii",
            newline="",
        )
        readme = readme_template.read_text(encoding="utf-8")
        readme = (
            readme.replace("{{PROJECT_NAME}}", project_name)
            .replace("{{DEVICE}}", config.device.name)
            .replace("{{DEVICE_FAMILY}}", family.name)
            .replace("{{DEVICE_DEFINE}}", config.define)
            .replace("{{STARTUP_FILE}}", config.startup_file)
        )
        (stage_path / "readme.txt").write_text(readme, encoding="utf-8", newline="")
        vscode_dir = stage_path / ".vscode"
        vscode_dir.mkdir()
        settings = {
            "files.encoding": "gbk",
            "[markdown]": {"files.encoding": "utf8"},
        }
        (vscode_dir / "settings.json").write_text(
            json.dumps(settings, indent=4) + "\n",
            encoding="utf-8",
            newline="",
        )

        project_path = _create_keil_project(stage_path, family, config, project_name)
        _validate_project(project_path, config)
        if destination.exists():
            destination.rmdir()
        stage_path.replace(destination)
        project_path = destination / "Project" / "MDK-ARM" / f"{project_name}.uvprojx"
    except Exception:
        shutil.rmtree(stage_path, ignore_errors=True)
        raise

    message = (
        f"工程已创建：{destination}\n"
        f"芯片：{config.device.name}\n"
        f"系列：{family.name}\n"
        f"CMSIS 宏：{config.define}\n"
        f"启动文件：{config.startup_file}\n"
        f"核心：{config.core}\n"
        f"DFP：{config.device.pack_id}\n"
        f"Flash：{_hex(config.device.flash_base)}，{config.device.flash_kb} KB\n"
        f"RAM：{_hex(config.device.ram_base)}，{config.device.ram_kb} KB\n"
        f"Keil 工程：{project_path}"
    )
    if build_after_generation:
        uv4 = _find_uv4()
        if uv4 is None:
            message += "\n未找到 Keil uVision，暂未编译。"
        else:
            result = subprocess.run(
                [str(uv4), "-b", str(project_path)],
                cwd=str(project_path.parent),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            build_log = destination / "Output" / f"{project_name}.build_log.htm"
            log_summary = ""
            if build_log.is_file():
                html = build_log.read_text(encoding="utf-8", errors="replace")
                match = re.search(r"(\d+) Error\(s\),\s*(\d+) Warning\(s\)\.", html)
                if match:
                    log_summary = f"{match.group(1)} 个错误，{match.group(2)} 个警告"
            if result.returncode != 0 or (log_summary and not log_summary.startswith("0 个错误")):
                details = (result.stdout + "\n" + result.stderr).strip()
                message += f"\nKeil 编译失败，请检查 {build_log}\n{details[-3000:]}"
                raise GenerationError(message)
            message += f"\nKeil 编译成功：{log_summary or '请检查生成日志确认详细结果'}"
    return project_path, message
