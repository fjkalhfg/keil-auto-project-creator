from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


HAL_SOURCES = (
    "hal.c",
    "hal_cortex.c",
    "hal_dma.c",
    "hal_gpio.c",
    "hal_pwr.c",
    "hal_rcc.c",
    "hal_rcc_ex.c",
    "hal_uart.c",
    "hal_usart.c",
)

# 部分系列（G0/G4/L4+ 等）将 *Ex 函数合并进了对应的基础源文件；
# 另一些系列中基础源文件（如 pwr.c/uart.c）又会调用 *Ex 函数。
# 这些文件存在才加入工程，不存在则跳过。
OPTIONAL_HAL_SOURCES = (
    "hal_dma_ex.c",
    "hal_gpio_ex.c",
    "hal_pwr_ex.c",
    "hal_uart_ex.c",
    "hal_usart_ex.c",
)

_DENSITY_KB = {
    "6": 32,
    "8": 64,
    "B": 128,
    "C": 256,
    "D": 384,
    "E": 512,
    "F": 768,
    "G": 1024,
    "H": 2048,
    "I": 4096,
}


class DiscoveryError(RuntimeError):
    """Raised when SDK or device-pack metadata cannot be verified."""


@dataclass(frozen=True)
class SdkFamily:
    name: str
    root: Path

    @property
    def hal_dir_name(self) -> str:
        return f"{self.name}_HAL_Driver"


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    family: str
    pack_id: str
    pack_root: Path
    pdsc_path: Path
    flash_base: int
    flash_size: int
    ram_base: int
    ram_size: int
    svd_relative: str
    algorithm_relative: str

    @property
    def flash_kb(self) -> int:
        return self.flash_size // 1024

    @property
    def ram_kb(self) -> int:
        return self.ram_size // 1024

    @property
    def display_name(self) -> str:
        return (
            f"{self.name}  —  Flash {self.flash_kb} KB"
            f"  /  RAM {self.ram_kb} KB"
        )


@dataclass(frozen=True)
class DeviceBuildConfig:
    device: DeviceInfo
    define: str
    startup_file: str
    core: str
    system_file: str
    interrupt_file: str
    hal_sources: tuple[str, ...]


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _element_chain(
    element: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
) -> list[ET.Element]:
    """器件节点及其 family/subFamily 祖先链（由近到远）。"""
    chain = [element]
    node = element
    while True:
        node = parent_map.get(node)
        if node is None:
            return chain
        chain.append(node)


def _memory_is_flash(memory: ET.Element) -> bool:
    memory_id = memory.get("id", "")
    if memory_id:
        return memory_id.startswith("IROM")
    access = memory.get("access", "")
    return "x" in access and "w" not in access


def _memory_is_ram(memory: ET.Element) -> bool:
    memory_id = memory.get("id", "")
    if memory_id:
        return memory_id.startswith("IRAM")
    access = memory.get("access", "")
    return "w" in access


def _read_pdsc(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise DiscoveryError(f"无法读取设备包元数据：{path}\n{exc}") from exc


def _hex_attribute(element: ET.Element, attribute: str, context: str) -> int:
    value = element.get(attribute, "")
    try:
        return int(value, 0)
    except ValueError as exc:
        raise DiscoveryError(f"{context} 中的 {attribute} 地址无效：{value!r}") from exc


def find_uv4() -> Path | None:
    candidates = [
        shutil.which("UV4.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Keil_v5", "UV4", "UV4.exe"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", ""),
            "Keil_v5",
            "UV4",
            "UV4.exe",
        ),
        r"C:\Keil_v5\UV4\UV4.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def discover_pack_roots(uv4_path: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Arm" / "Packs")

    for base in (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
        Path("C:/Keil_v5"),
    ):
        roots.append(base / "Keil_v5" / "ARM" / "PACK")
        roots.append(base / "ARM" / "PACK")

    uv4_path = uv4_path or find_uv4()
    if uv4_path:
        keil_root = uv4_path.parent.parent
        roots.extend(
            (
                keil_root / "ARM" / "PACK",
                keil_root / "packs",
                keil_root.parent / "packs",
            )
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root.is_dir():
            resolved = root.resolve()
            key = os.path.normcase(str(resolved))
            if key not in seen:
                unique.append(resolved)
                seen.add(key)
    return unique


def discover_sdk_families(sdk_root: Path) -> list[SdkFamily]:
    sdk_root = sdk_root.expanduser().resolve()
    cmsis_device_root = sdk_root / "Drivers" / "CMSIS" / "Device" / "ST"
    if not cmsis_device_root.is_dir():
        raise DiscoveryError(
            f"此目录不是标准 STM32Cube SDK（缺少 {cmsis_device_root}）。"
        )

    families: list[SdkFamily] = []
    for family_path in sorted(cmsis_device_root.iterdir()):
        if not family_path.is_dir() or not re.fullmatch(r"STM32[A-Z0-9]+xx", family_path.name):
            continue
        hal_dir = sdk_root / "Drivers" / f"{family_path.name}_HAL_Driver"
        if not hal_dir.is_dir():
            continue
        families.append(SdkFamily(family_path.name, sdk_root))
    if not families:
        raise DiscoveryError("在该 SDK 中未找到同时包含 CMSIS 与 HAL 的 STM32 系列。")
    return families


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers) or (0,)


def _valid_device_info(
    device_node: ET.Element,
    family: str,
    pdsc_root: ET.Element,
    pdsc_path: Path,
    parent_map: dict[ET.Element, ET.Element],
) -> DeviceInfo | None:
    device_name = device_node.get("Dname", "")
    if not device_name:
        return None

    # 新版 pdsc 会把 memory/algorithm/debug 等定义在 family/subFamily 层级，
    # 由器件节点继承；按由近到远逐级查找，器件自身定义优先。
    chain = _element_chain(device_node, parent_map)

    flash_regions: list[ET.Element] = []
    ram_regions: list[ET.Element] = []
    for level in chain:
        level_flash = [m for m in level.findall("memory") if _memory_is_flash(m)]
        level_ram = [m for m in level.findall("memory") if _memory_is_ram(m)]
        if level_flash and not flash_regions:
            flash_regions = level_flash
        if level_ram and not ram_regions:
            ram_regions = level_ram

    if len(flash_regions) != 1 or len(ram_regions) != 1:
        return None
    flash = flash_regions[0]
    ram = ram_regions[0]

    flash_base = _hex_attribute(flash, "start", device_name)
    flash_size = _hex_attribute(flash, "size", device_name)
    ram_base = _hex_attribute(ram, "start", device_name)
    ram_size = _hex_attribute(ram, "size", device_name)
    if flash_size <= 0 or ram_size <= 0:
        return None
    flash_end = flash_base + flash_size - 1

    pack_root = pdsc_path.parent
    algorithm_name = ""
    for level in chain:
        for algorithm in level.findall("algorithm"):
            start = _hex_attribute(algorithm, "start", device_name)
            size = _hex_attribute(algorithm, "size", device_name)
            if start <= flash_base and start + size > flash_end:
                relative = algorithm.get("name", "").replace("/", os.sep)
                if relative and (pack_root / relative).is_file():
                    algorithm_name = algorithm.get("name", "")
                    break
        if algorithm_name:
            break
    if not algorithm_name:
        return None

    svd_relative = ""
    for level in chain:
        debug = level.find("debug")
        if debug is not None and debug.get("svd"):
            svd_relative = debug.get("svd", "")
            break
    if svd_relative:
        svd_path = pack_root / Path(svd_relative.replace("/", os.sep))
        if not svd_path.is_file():
            return None

    vendor = pdsc_root.findtext("vendor", default="")
    pack_name = pdsc_root.findtext("name", default="")
    version = pdsc_path.parent.name
    if not vendor or not pack_name:
        return None

    return DeviceInfo(
        name=device_name,
        family=family,
        pack_id=f"{vendor}.{pack_name}.{version}",
        pack_root=pack_root,
        pdsc_path=pdsc_path,
        flash_base=flash_base,
        flash_size=flash_size,
        ram_base=ram_base,
        ram_size=ram_size,
        svd_relative=svd_relative,
        algorithm_relative=algorithm_name,
    )


def discover_devices(
    family: SdkFamily,
    pack_roots: list[Path] | None = None,
) -> list[DeviceInfo]:
    roots = pack_roots if pack_roots is not None else discover_pack_roots()
    pack_name = f"{family.name}_DFP"
    found: dict[str, DeviceInfo] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for vendor_dir in root.iterdir():
            if not vendor_dir.is_dir():
                continue
            pack_dir = vendor_dir / pack_name
            if not pack_dir.is_dir():
                continue
            for version_dir in pack_dir.iterdir():
                if not version_dir.is_dir():
                    continue
                pdsc_path = version_dir / f"{vendor_dir.name}.{pack_name}.pdsc"
                if not pdsc_path.is_file():
                    continue
                pdsc_root = _read_pdsc(pdsc_path)
                devices_node = pdsc_root.find("devices")
                if devices_node is None:
                    continue
                parent_map = _parent_map(pdsc_root)
                for device_node in devices_node.iter("device"):
                    info = _valid_device_info(
                        device_node, family.name, pdsc_root, pdsc_path, parent_map
                    )
                    if info is None:
                        continue
                    current = found.get(info.name)
                    if current is None or _version_key(info.pack_id.rsplit(".", 1)[-1]) > _version_key(
                        current.pack_id.rsplit(".", 1)[-1]
                    ):
                        found[info.name] = info
    if not found:
        raise DiscoveryError(
            f"未找到已完整安装、包含 {family.name} 器件及 Flash 算法的 Keil DFP。"
            "请用 Keil Pack Installer 安装对应系列设备包。"
        )
    return sorted(found.values(), key=lambda item: item.name)


def _family_header(family: SdkFamily) -> Path:
    return (
        family.root
        / "Drivers"
        / "CMSIS"
        / "Device"
        / "ST"
        / family.name
        / "Include"
        / f"{family.name.lower()}.h"
    )


def _variant_headers(family: SdkFamily) -> dict[str, tuple[str, Path]]:
    header_path = _family_header(family)
    if not header_path.is_file():
        raise DiscoveryError(f"找不到系列 CMSIS 头文件：{header_path}")
    text = header_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    variants: dict[str, tuple[str, Path]] = {}
    active_define: str | None = None
    for line in lines:
        condition = re.search(
            r"#\s*(?:if|elif)\s*(?:\(\s*)?defined\s*\(?\s*(STM32[A-Za-z0-9]+)\s*\)?",
            line,
        )
        if condition:
            active_define = condition.group(1)
            continue
        include = re.search(r'#\s*include\s+"([^"]+\.h)"', line)
        if include and active_define:
            include_path = header_path.parent / include.group(1)
            if include_path.is_file():
                variants[active_define] = (include.group(1), include_path)
            active_define = None
    return variants


def _infer_device_define(family: SdkFamily, device: DeviceInfo) -> tuple[str, Path]:
    variants = _variant_headers(family)
    model_prefix = re.match(r"(STM32[A-Z]\d{3,4})", device.name.upper())
    if not model_prefix:
        raise DiscoveryError(
            f"无法从器件名 {device.name} 自动匹配 CMSIS 宏，请确认该系列 SDK 的器件命名规则。"
        )
    prefix = model_prefix.group(1).lower()
    candidates = [
        (define, header_name, header_path)
        for define, (header_name, header_path) in variants.items()
        if Path(header_name).stem.lower().startswith(prefix)
    ]
    if not candidates:
        raise DiscoveryError(f"在 {family.name} CMSIS 头文件中找不到 {device.name} 对应的器件宏。")

    viable: list[tuple[int, str, Path]] = []
    for define, header_name, header_path in candidates:
        density = re.search(r"x([0-9a-z])$", Path(header_name).stem.lower())
        capacity = _DENSITY_KB.get(density.group(1).upper()) if density else None
        if capacity is not None and capacity >= device.flash_kb:
            viable.append((capacity, define, header_path))
    if viable:
        _, define, header_path = min(viable, key=lambda item: item[0])
        return define, header_path
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][2]
    choices = ", ".join(candidate[0] for candidate in candidates)
    raise DiscoveryError(
        f"无法根据 DFP Flash 大小唯一确定 {device.name} 的 CMSIS 宏（候选：{choices}）。"
    )


def _infer_core(family: SdkFamily, device: DeviceInfo, variant_header: Path) -> str:
    node = _read_pdsc(device.pdsc_path)
    device_node = next(
        (
            item
            for item in node.iter("device")
            if item.get("Dname") == device.name
        ),
        None,
    )
    if device_node is not None:
        for level in _element_chain(device_node, _parent_map(node)):
            processor = level.find("processor")
            if processor is not None:
                processor_name = processor.get("Dcore") or processor.get("Pname", "")
                if processor_name.startswith("Cortex-M"):
                    return processor_name

    text = variant_header.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r'#\s*include\s+"core_cm(0plus|0|3|4|7|23|33|35p|55)\.h"',
        text,
        re.I,
    )
    if match:
        core_part = match.group(1).lower()
        suffix = {"0plus": "0+"}.get(core_part, core_part)
        return f"Cortex-M{suffix}"
    revision = re.search(
        r"#\s*define\s+__CM(0|3|4|7|23|33|55)_REV\b",
        text,
    )
    if revision:
        return f"Cortex-M{revision.group(1)}"
    raise DiscoveryError(f"无法从 DFP/CMSIS 确认 {device.name} 的 Cortex-M 核心型号。")


def resolve_build_config(family: SdkFamily, device: DeviceInfo) -> DeviceBuildConfig:
    define, variant_header = _infer_device_define(family, device)
    startup_dir = (
        family.root
        / "Drivers"
        / "CMSIS"
        / "Device"
        / "ST"
        / family.name
        / "Source"
        / "Templates"
        / "arm"
    )
    startup_file = f"startup_{variant_header.stem}.s"
    startup_path = startup_dir / startup_file
    if not startup_path.is_file():
        raise DiscoveryError(f"找不到与器件宏匹配的启动文件：{startup_path}")

    system_file = f"system_{family.name.lower()}.c"
    system_path = (
        family.root
        / "Drivers"
        / "CMSIS"
        / "Device"
        / "ST"
        / family.name
        / "Source"
        / "Templates"
        / system_file
    )
    if not system_path.is_file():
        raise DiscoveryError(f"找不到 CMSIS 系统文件：{system_path}")

    hal_dir = family.root / "Drivers" / family.hal_dir_name
    prefix = family.name.lower()
    hal_sources = tuple(f"{prefix}_{source}" for source in HAL_SOURCES)
    hal_sources += tuple(
        f"{prefix}_{source}"
        for source in OPTIONAL_HAL_SOURCES
        if (hal_dir / "Src" / f"{prefix}_{source}").is_file()
    )
    missing = [name for name in hal_sources if not (hal_dir / "Src" / name).is_file()]
    if missing:
        raise DiscoveryError(
            "该系列 HAL 不包含标准基础工程所需源文件："
            + ", ".join(missing)
        )
    configuration = hal_dir / "Inc" / f"{family.name.lower()}_hal_conf_template.h"
    if not configuration.is_file():
        raise DiscoveryError(f"找不到 HAL 配置模板：{configuration}")

    return DeviceBuildConfig(
        device=device,
        define=define,
        startup_file=startup_file,
        core=_infer_core(family, device, variant_header),
        system_file=system_file,
        interrupt_file=f"{family.name.lower()}_it.c",
        hal_sources=hal_sources,
    )
