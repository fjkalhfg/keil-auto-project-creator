from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from keil_creator.discovery import (
    DiscoveryError,
    discover_devices,
    discover_sdk_families,
    resolve_build_config,
)
from keil_creator.generator import (
    GenerationError,
    _patch_hal_configuration,
    generate_project,
)


ROOT = Path(__file__).resolve().parents[1]


class ProjectCreatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.sdk = self.base / "STM32CubeF1"
        self.pack_root = self.base / "packs"
        self._create_fake_sdk()
        self._create_fake_pack()
        self.family = discover_sdk_families(self.sdk)[0]
        self.device = discover_devices(self.family, [self.pack_root])[0]
        self.config = resolve_build_config(self.family, self.device)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_fake_sdk(self) -> None:
        device_root = (
            self.sdk
            / "Drivers"
            / "CMSIS"
            / "Device"
            / "ST"
            / "STM32F1xx"
        )
        include = device_root / "Include"
        startup = device_root / "Source" / "Templates" / "arm"
        (self.sdk / "Drivers" / "CMSIS" / "Include").mkdir(parents=True)
        include.mkdir(parents=True)
        startup.mkdir(parents=True)
        (include / "stm32f1xx.h").write_text(
            '#if defined(STM32F103xB)\n#include "stm32f103xb.h"\n#endif\n',
            encoding="utf-8",
        )
        (include / "stm32f103xb.h").write_text(
            '#include "core_cm3.h"\n', encoding="utf-8"
        )
        (self.sdk / "Drivers" / "CMSIS" / "Include" / "core_cm3.h").write_text(
            "", encoding="utf-8"
        )
        (device_root / "Source" / "Templates" / "system_stm32f1xx.c").write_text(
            "", encoding="utf-8"
        )
        (startup / "startup_stm32f103xb.s").write_text("", encoding="utf-8")

        hal = self.sdk / "Drivers" / "STM32F1xx_HAL_Driver"
        (hal / "Inc").mkdir(parents=True)
        (hal / "Src").mkdir()
        (hal / "Inc" / "stm32f1xx_hal_conf_template.h").write_text(
            "\n".join(
                f"/* #define {module} */"
                for module in (
                    "HAL_CORTEX_MODULE_ENABLED",
                    "HAL_DMA_MODULE_ENABLED",
                    "HAL_FLASH_MODULE_ENABLED",
                    "HAL_GPIO_MODULE_ENABLED",
                    "HAL_PWR_MODULE_ENABLED",
                    "HAL_RCC_MODULE_ENABLED",
                    "HAL_UART_MODULE_ENABLED",
                    "HAL_USART_MODULE_ENABLED",
                    "HAL_ADC_MODULE_ENABLED",
                )
            ),
            encoding="utf-8",
        )
        for source in (
            "hal.c",
            "hal_cortex.c",
            "hal_dma.c",
            "hal_gpio.c",
            "hal_gpio_ex.c",
            "hal_pwr.c",
            "hal_rcc.c",
            "hal_rcc_ex.c",
            "hal_uart.c",
            "hal_usart.c",
        ):
            (hal / "Src" / f"stm32f1xx_{source}").write_text("", encoding="utf-8")
        (hal / "Inc" / "stm32f1xx_hal.h").write_text("", encoding="utf-8")

    def _create_fake_pack(self) -> None:
        pack = self.pack_root / "Keil" / "STM32F1xx_DFP" / "2.4.1"
        (pack / "Flash").mkdir(parents=True)
        (pack / "SVD").mkdir()
        (pack / "Flash" / "stm32f1.flm").write_bytes(b"test")
        (pack / "SVD" / "STM32F103xx.svd").write_text("<device/>", encoding="utf-8")
        (pack / "Keil.STM32F1xx_DFP.pdsc").write_text(
            """<?xml version="1.0"?>
<package>
  <vendor>Keil</vendor>
  <name>STM32F1xx_DFP</name>
  <devices>
    <family Dfamily="STM32F1 Series">
      <subFamily DsubFamily="STM32F103">
        <device Dname="STM32F103C8">
          <processor Dcore="Cortex-M3"/>
          <debug svd="SVD/STM32F103xx.svd"/>
          <memory id="IROM1" start="0x08000000" size="0x00010000"/>
          <memory id="IRAM1" start="0x20000000" size="0x00005000"/>
          <algorithm name="Flash/stm32f1.flm" start="0x08000000" size="0x00020000"/>
        </device>
      </subFamily>
    </family>
  </devices>
</package>
""",
            encoding="utf-8",
        )

    def test_discovers_family_and_installed_device(self) -> None:
        self.assertEqual(self.family.name, "STM32F1xx")
        self.assertEqual(self.device.name, "STM32F103C8")
        self.assertEqual(self.device.pack_id, "Keil.STM32F1xx_DFP.2.4.1")
        self.assertEqual(self.device.flash_kb, 64)
        self.assertEqual(self.device.ram_kb, 20)

    def test_infers_cmsis_startup_and_core(self) -> None:
        self.assertEqual(self.config.define, "STM32F103xB")
        self.assertEqual(self.config.startup_file, "startup_stm32f103xb.s")
        self.assertEqual(self.config.core, "Cortex-M3")

    def test_enables_plain_sdk_hal_macros_and_disables_other_modules(self) -> None:
        source = self.sdk / "Drivers" / "STM32F1xx_HAL_Driver" / "Inc" / "stm32f1xx_hal_conf_template.h"
        enabled = (
            "HAL_CORTEX_MODULE_ENABLED",
            "HAL_DMA_MODULE_ENABLED",
            "HAL_FLASH_MODULE_ENABLED",
            "HAL_GPIO_MODULE_ENABLED",
            "HAL_PWR_MODULE_ENABLED",
            "HAL_RCC_MODULE_ENABLED",
            "HAL_UART_MODULE_ENABLED",
            "HAL_USART_MODULE_ENABLED",
        )
        source.write_text(
            "\n".join(f"#define {name}" for name in enabled)
            + "\n/* #define HAL_ADC_MODULE_ENABLED */\n",
            encoding="utf-8",
        )
        destination = self.base / "hal_conf.h"
        _patch_hal_configuration(source, destination)
        result = destination.read_text(encoding="utf-8")
        for name in enabled:
            self.assertIn(f"#define {name}", result)
        self.assertIn("/* #define HAL_ADC_MODULE_ENABLED */", result)

    def test_generates_and_validates_keil_project(self) -> None:
        destination = self.base / "generated"
        project_path, _ = generate_project(
            family=self.family,
            config=self.config,
            destination=destination,
            project_name="atk_f103",
            template_dir=ROOT / "assets",
        )
        self.assertTrue(project_path.is_file())
        self.assertTrue((destination / "Output" / "atk_f103.sct").is_file())
        settings = json.loads(
            (destination / ".vscode" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(settings["files.encoding"], "gbk")
        self.assertEqual(settings["[markdown]"]["files.encoding"], "utf8")
        self.assertTrue((destination / "Application").is_dir())
        self.assertTrue((destination / "Users" / "main.c").is_file())
        xml = ET.parse(project_path).getroot()
        target = xml.find("./Targets/Target")
        assert target is not None
        self.assertEqual(target.findtext("TargetName"), "atk_f103")
        common = target.find("./TargetOption/TargetCommonOption")
        assert common is not None
        self.assertEqual(common.findtext("Device"), "STM32F103C8")
        self.assertEqual(common.findtext("PackID"), self.device.pack_id)
        self.assertEqual(common.findtext("CreateHexFile"), "0")
        self.assertEqual(
            target.findtext("./TargetOption/TargetArmAds/LDads/useFile"),
            "1",
        )

    def test_refuses_to_overwrite_nonempty_destination(self) -> None:
        destination = self.base / "not_empty"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(GenerationError):
            generate_project(
                family=self.family,
                config=self.config,
                destination=destination,
                project_name="atk_f103",
                template_dir=ROOT / "assets",
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_rejects_missing_flash_algorithm_file(self) -> None:
        (self.device.pack_root / "Flash" / "stm32f1.flm").unlink()
        with self.assertRaises(DiscoveryError):
            discover_devices(self.family, [self.pack_root])


if __name__ == "__main__":
    unittest.main()
