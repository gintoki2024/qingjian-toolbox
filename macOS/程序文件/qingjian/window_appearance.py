"""Application-local appearance preferences; no system settings are changed."""
from __future__ import annotations

import json
import math
import os
import tempfile
import weakref
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .icons import bind_window_icon


def opacity_value(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return 1.0
    return min(1.0, max(.5, float(value))) if math.isfinite(value) else 1.0


class AppearanceController(QObject):
    changed = Signal(dict)
    save_error = Signal(str)

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = Path(path) if path else None
        self.opacity = 1.0
        self.snap_enabled = True
        self.last_error = ""
        self._windows = weakref.WeakSet()
        self._closed = False
        self._load()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.flush)
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("请先创建界面应用。")
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.shutdown)

    @property
    def settings(self):
        return {"opacity": self.opacity, "snap_enabled": self.snap_enabled}

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 16384:
                raise ValueError("设置文件超过大小限制")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("设置版本无法识别")
            self.opacity = opacity_value(data.get("opacity", 1.0))
            self.snap_enabled = data.get("snap_enabled", True)
            if type(self.snap_enabled) is not bool:
                self.snap_enabled = True
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self.last_error = "外观设置未能读取，当前使用默认外观：" + str(exc)

    def register(self, window):
        if self._closed or not isValid(window):
            return
        self._windows.add(window)
        if abs(window.windowOpacity() - self.opacity) > .003:
            window.setWindowOpacity(self.opacity)

    def eventFilter(self, watched, event):
        if (not self._closed and event.type() == QEvent.Type.Show
                and isinstance(watched, QWidget) and watched.isWindow()
                and watched.windowType() not in {Qt.WindowType.ToolTip, Qt.WindowType.Popup}):
            self.register(watched)
        return False

    def update_settings(self, *, opacity=None, snap_enabled=None):
        if self._closed:
            return
        new_opacity = self.opacity if opacity is None else opacity_value(opacity)
        new_snap = self.snap_enabled if snap_enabled is None else bool(snap_enabled)
        if new_opacity == self.opacity and new_snap == self.snap_enabled:
            return
        self.opacity, self.snap_enabled = new_opacity, new_snap
        for window in list(self._windows):
            if isValid(window):
                self.register(window)
        self.changed.emit(self.settings)
        self._save_timer.start()

    def flush(self):
        self._save_timer.stop()
        if self.path is None:
            return True
        staged = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema_version": 1, **self.settings}
            descriptor, name = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=self.path.parent)
            staged = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path)
            self.last_error = ""
            return True
        except (OSError, ValueError) as exc:
            self.last_error = "外观设置未能保存，本次调整仍然有效：" + str(exc)
            self.save_error.emit(self.last_error)
            return False
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass

    def shutdown(self):
        if self._closed:
            return
        if self._save_timer.isActive():
            self.flush()
        self._closed = True
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._windows.clear()


class AppearanceDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self.setWindowTitle("外观与窗口吸附")
        bind_window_icon(self, "toolbox")
        self.setObjectName("workspace")
        self.resize(430, 330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        heading = QLabel("让窗口更适合你的桌面")
        heading.setObjectName("cardTitle")
        layout.addWidget(heading)
        self.value_label = QLabel()
        layout.addWidget(self.value_label)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(50, 100)
        self.opacity_slider.setSingleStep(1)
        self.opacity_slider.setPageStep(5)
        self.opacity_slider.setAccessibleName("所有窗口的不透明度，50至100百分比")
        self.opacity_slider.valueChanged.connect(lambda value: controller.update_settings(opacity=value / 100))
        layout.addWidget(self.opacity_slider)
        hint = QLabel("100% 完全不透明；数值越低，越能透出后方内容。\n主窗口、转换、记事及结果窗口统一调整，并记住设置。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.snap_check = QCheckBox("启用窗口自动吸附")
        self.snap_check.setAccessibleName("启用窗口自动吸附")
        self.snap_check.toggled.connect(lambda value: controller.update_settings(snap_enabled=value))
        layout.addWidget(self.snap_check)
        hint = QLabel("拖近屏幕边缘或其他工具小窗即可对齐；按住 Alt 可临时自由拖动。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error_label = QLabel(controller.last_error)
        self.error_label.setObjectName("notice")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(controller.last_error))
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        actions = QHBoxLayout()
        reset = QPushButton("恢复默认")
        reset.clicked.connect(lambda: controller.update_settings(opacity=1.0, snap_enabled=True))
        actions.addWidget(reset)
        actions.addStretch(1)
        done = QPushButton("完成")
        done.setObjectName("primary")
        done.clicked.connect(self.accept)
        actions.addWidget(done)
        layout.addLayout(actions)
        controller.changed.connect(self._sync)
        controller.save_error.connect(self._show_error)
        self.finished.connect(lambda _: controller.flush())
        self._sync(controller.settings)

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _sync(self, settings):
        value = round(settings["opacity"] * 100)
        self.value_label.setText(f"窗口不透明度  {value}%")
        with QSignalBlocker(self.opacity_slider):
            self.opacity_slider.setValue(value)
        with QSignalBlocker(self.snap_check):
            self.snap_check.setChecked(settings["snap_enabled"])
