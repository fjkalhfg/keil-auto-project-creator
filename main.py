from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from keil_creator.discovery import (
    DeviceBuildConfig,
    DeviceInfo,
    DiscoveryError,
    SdkFamily,
    discover_devices,
    discover_pack_roots,
    discover_sdk_families,
    find_uv4,
    resolve_build_config,
)
from keil_creator.generator import GenerationError, generate_project
from keil_creator import __version__


class OperationWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation):
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        try:
            self.succeeded.emit(self._operation())
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("STM32Tools", "KeilProjectCreator")
        self.family_devices: dict[str, tuple[SdkFamily, list[DeviceInfo]]] = {}
        self.worker: OperationWorker | None = None
        self.setWindowTitle("STM32 Keil 基础工程生成器")
        self.setMinimumSize(760, 560)
        self._build_ui()
        self._restore_settings()
        self._refresh_keil_status()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        title_row = QHBoxLayout()
        title = QLabel("STM32 Keil 基础工程生成器")
        title.setObjectName("title")
        version = QLabel(f"版本 {__version__}")
        version.setObjectName("version")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(version)
        subtitle = QLabel("选择本机 STM32Cube SDK 和芯片型号，自动配置并创建最小 HAL 工程。")
        subtitle.setObjectName("subtitle")
        root.addLayout(title_row)
        root.addWidget(subtitle)

        setup_group = QGroupBox("工程设置")
        form = QFormLayout(setup_group)
        form.setVerticalSpacing(12)
        self.sdk_edit = QLineEdit()
        self.sdk_edit.setPlaceholderText("选择已解压的 STM32Cube 系列 SDK 根目录")
        self.sdk_button = QPushButton("浏览…")
        self.sdk_button.clicked.connect(self._browse_sdk)
        form.addRow("STM32Cube SDK", self._with_button(self.sdk_edit, self.sdk_button))

        self.family_combo = QComboBox()
        self.family_combo.setPlaceholderText("先选择 SDK 并扫描")
        form.addRow("芯片系列", self.family_combo)
        self.device_combo = QComboBox()
        self.device_combo.setPlaceholderText("扫描后选择具体芯片")
        form.addRow("芯片型号", self.device_combo)

        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("新工程放置目录（必须为空或不存在）")
        self.destination_button = QPushButton("浏览…")
        self.destination_button.clicked.connect(self._browse_destination)
        form.addRow("工程目录", self._with_button(self.destination_edit, self.destination_button))

        self.project_name_edit = QLineEdit()
        self.project_name_edit.setPlaceholderText("留空时按芯片型号自动命名")
        form.addRow("工程名称", self.project_name_edit)
        root.addWidget(setup_group)

        action_row = QHBoxLayout()
        self.scan_button = QPushButton("扫描 SDK / 芯片")
        self.scan_button.clicked.connect(self._scan)
        self.create_button = QPushButton("创建 Keil 工程")
        self.create_button.setObjectName("primary")
        self.create_button.clicked.connect(self._create)
        self.build_checkbox = QCheckBox("创建后尝试调用 Keil 编译")
        self.build_checkbox.setChecked(find_uv4() is not None)
        action_row.addWidget(self.scan_button)
        action_row.addWidget(self.create_button)
        action_row.addWidget(self.build_checkbox)
        action_row.addStretch(1)
        root.addLayout(action_row)

        details_group = QGroupBox("自动识别结果")
        details_layout = QVBoxLayout(details_group)
        self.details_label = QLabel("选择 SDK 并扫描后，这里会显示器件包、启动文件和内存信息。")
        self.details_label.setWordWrap(True)
        self.details_label.setObjectName("details")
        details_layout.addWidget(self.details_label)
        root.addWidget(details_group)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        root.addWidget(self.progress)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("运行状态和错误信息会显示在这里。")
        root.addWidget(self.log_output, 1)

        self.family_combo.currentIndexChanged.connect(self._family_changed)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        self.sdk_edit.editingFinished.connect(self._sdk_path_edited)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f7fb; color: #172033; font-size: 13px; }
            QLabel#title { font-size: 22px; font-weight: 700; color: #102a43; }
            QLabel#version { color: #64758b; font-size: 12px; padding: 5px 9px;
                             background: #e8eef6; border-radius: 10px; }
            QLabel#subtitle { color: #526579; }
            QGroupBox {
                background: white; border: 1px solid #dce4ef; border-radius: 8px;
                margin-top: 10px; padding: 14px; font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QLineEdit, QComboBox, QPlainTextEdit {
                background: white; border: 1px solid #ccd7e5; border-radius: 5px;
                padding: 7px; selection-background-color: #3478c8;
            }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #3478c8; }
            QPushButton {
                background: #e8eef6; border: 1px solid #d1dce9; border-radius: 5px;
                padding: 8px 14px;
            }
            QPushButton:hover { background: #dce8f6; }
            QPushButton#primary { background: #1769aa; color: white; border-color: #1769aa; font-weight: 600; }
            QPushButton#primary:hover { background: #0e558f; }
            QPushButton:disabled { color: #8290a0; }
            QLabel#details { color: #3b5068; font-weight: 400; }
            QProgressBar { border: 0; background: #e3eaf2; border-radius: 2px; max-height: 4px; }
            QProgressBar::chunk { background: #2383cf; }
            """
        )

    @staticmethod
    def _with_button(edit: QLineEdit, button: QPushButton) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return container

    def _restore_settings(self) -> None:
        self.sdk_edit.setText(self.settings.value("sdk_path", "", str))
        self.destination_edit.setText(self.settings.value("destination", "", str))
        if self.sdk_edit.text().strip():
            self._scan()

    def _browse_sdk(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "选择 STM32Cube SDK 根目录",
            self.sdk_edit.text() or str(Path.home()),
        )
        if path:
            self.sdk_edit.setText(path)
            self._scan()

    def _browse_destination(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "选择工程存放位置",
            self.destination_edit.text() or str(Path.home()),
        )
        if path:
            self.destination_edit.setText(path)

    def _sdk_path_edited(self) -> None:
        if self.sdk_edit.text().strip():
            self._scan()

    def _scan(self) -> None:
        sdk_path = Path(self.sdk_edit.text().strip())
        if not sdk_path.is_dir():
            self._show_error("请选择有效的 STM32Cube SDK 根目录。")
            return
        self._set_busy(True, "正在扫描 SDK 和本机已安装的 Keil 设备包…")

        def scan() -> list[tuple[SdkFamily, list[DeviceInfo]]]:
            families = discover_sdk_families(sdk_path)
            pack_roots = discover_pack_roots()
            results: list[tuple[SdkFamily, list[DeviceInfo]]] = []
            failures: list[str] = []
            for family in families:
                try:
                    devices = discover_devices(family, pack_roots)
                    results.append((family, devices))
                except DiscoveryError as exc:
                    failures.append(f"{family.name}: {exc}")
            if not results:
                detail = "\n".join(failures)
                raise DiscoveryError(
                    "找到了 SDK，但没有找到包含该系列的完整 Keil DFP。"
                    + (f"\n{detail}" if detail else "")
                )
            return results

        self._run_worker(scan, self._scan_finished)

    def _scan_finished(self, results: list[tuple[SdkFamily, list[DeviceInfo]]]) -> None:
        self.family_devices = {family.name: (family, devices) for family, devices in results}
        self.family_combo.blockSignals(True)
        self.family_combo.clear()
        for family, devices in results:
            self.family_combo.addItem(f"{family.name}（{len(devices)} 个芯片）", family.name)
        self.family_combo.blockSignals(False)
        if self.family_combo.count():
            self.family_combo.setCurrentIndex(0)
        self._family_changed()
        self._append_log(
            "扫描完成：" + "；".join(f"{family.name} {len(devices)} 款" for family, devices in results)
        )
        self.settings.setValue("sdk_path", self.sdk_edit.text().strip())
        self._set_busy(False)

    def _family_changed(self, *_args) -> None:
        family_name = self.family_combo.currentData()
        if not family_name or family_name not in self.family_devices:
            return
        _, devices = self.family_devices[family_name]
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.display_name, device.name)
        self.device_combo.blockSignals(False)
        if self.device_combo.count():
            self.device_combo.setCurrentIndex(0)
        self._device_changed()

    def _selected(self) -> tuple[SdkFamily, DeviceInfo, DeviceBuildConfig] | None:
        family_name = self.family_combo.currentData()
        device_name = self.device_combo.currentData()
        if not family_name or not device_name:
            return None
        family, devices = self.family_devices[family_name]
        device = next((item for item in devices if item.name == device_name), None)
        if device is None:
            return None
        config = resolve_build_config(family, device)
        return family, device, config

    def _device_changed(self, *_args) -> None:
        try:
            selected = self._selected()
        except DiscoveryError as exc:
            self.details_label.setText(f"无法自动识别该芯片：{exc}")
            return
        if selected is None:
            return
        family, device, config = selected
        self.project_name_edit.setText(self._default_project_name(device.name))
        self.details_label.setText(
            f"DFP：{device.pack_id}\n"
            f"CMSIS 宏：{config.define}    启动文件：{config.startup_file}\n"
            f"核心：{config.core}\n"
            f"Flash：0x{device.flash_base:X}，{device.flash_kb} KB    "
            f"RAM：0x{device.ram_base:X}，{device.ram_kb} KB"
        )

    @staticmethod
    def _default_project_name(device_name: str) -> str:
        if device_name.upper().startswith("STM32F103C8") or device_name.upper().startswith("STM32F103T8"):
            return "atk_f103"
        suffix = device_name.upper().removeprefix("STM32").lower()
        return f"atk_{suffix}"

    def _create(self) -> None:
        try:
            selected = self._selected()
        except DiscoveryError as exc:
            self._show_error(str(exc))
            return
        if selected is None:
            self._show_error("请先扫描 SDK，并选择芯片型号。")
            return
        family, _, config = selected
        destination_text = self.destination_edit.text().strip()
        if not destination_text:
            self._show_error("请选择新工程的存放目录。")
            return
        project_name = self.project_name_edit.text().strip()
        if not project_name:
            project_name = self._default_project_name(config.device.name)
            self.project_name_edit.setText(project_name)
        if Path(destination_text).exists() and any(Path(destination_text).iterdir()):
            self._show_error("目标目录不是空目录。请选择一个新的或空的目录，避免覆盖已有文件。")
            return

        sdk_root = Path(self.sdk_edit.text().strip())
        destination = Path(destination_text)
        self.settings.setValue("destination", destination_text)
        should_build = self.build_checkbox.isChecked()
        template_dir = Path(__file__).resolve().parent / "assets"
        self._set_busy(True, "正在复制 SDK 文件、配置 Keil 工程并校验引用…")

        def create() -> tuple[Path, str]:
            return generate_project(
                family=family,
                config=config,
                destination=destination,
                project_name=project_name,
                template_dir=template_dir,
                build_after_generation=should_build,
            )

        self._run_worker(create, self._generation_finished)

    def _generation_finished(self, result: tuple[Path, str]) -> None:
        project_path, message = result
        self._append_log(message)
        self._set_busy(False)
        QMessageBox.information(
            self,
            "工程创建完成",
            f"Keil 工程已生成：\n{project_path}",
        )

    def _run_worker(self, operation, on_success) -> None:
        self.worker = OperationWorker(operation)
        self.worker.succeeded.connect(on_success)
        self.worker.failed.connect(self._worker_failed)
        self.worker.start()

    def _worker_failed(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(message)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for widget in (
            self.sdk_button,
            self.destination_button,
            self.scan_button,
            self.create_button,
            self.family_combo,
            self.device_combo,
        ):
            widget.setEnabled(not busy)
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(0)
        if message:
            self._append_log(message)
        QApplication.processEvents()

    def _refresh_keil_status(self) -> None:
        uv4 = find_uv4()
        if uv4:
            self._append_log(f"已检测到 Keil uVision：{uv4}")
        else:
            self._append_log("未检测到 Keil uVision；仍可生成工程，之后可在 Keil 中编译。")

    def _append_log(self, text: str) -> None:
        self.log_output.appendPlainText(text)

    def _show_error(self, message: str) -> None:
        self._append_log(f"错误：{message}")
        QMessageBox.warning(self, "操作未完成", message)


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("STM32Tools")
    app.setApplicationName("KeilProjectCreator")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
