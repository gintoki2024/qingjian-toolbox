"""Independent desktop notebook lifetime, due queue and reminder cards.

The controller belongs to QApplication (or no parent), never the toolbox's
window. It does not call QApplication.quit: the application integration decides
when other office work is finished. ``test_mode`` disables IPC, notifications,
sounds and the real tray; the clock and ``tick`` remain deterministic.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QLockFile, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .glass import apply_glass


def _store_path(store):
    value = getattr(store, "path", None) or getattr(store, "data_path", None)
    return Path(value).resolve() if value is not None else None


def _icon():
    canvas = QPixmap(64, 64)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#6257ed"))
    painter.drawRoundedRect(4, 4, 56, 56, 15, 15)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPixelSize(32)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, "记")
    painter.end()
    return QIcon(canvas)


class ReminderCard(QDialog):
    """One visible queued alert: closing never silently discards an alert."""

    complete_requested = Signal(str)
    snooze_requested = Signal(str, int)
    view_requested = Signal(str)

    def __init__(self, alert):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.alert = alert
        self.allow_close = False
        self.setObjectName("reminderCard")
        self.setWindowTitle("青简 · 事项提醒")
        self.setWindowIcon(_icon())
        self.setMinimumWidth(380)
        self.resize(420, 270)
        self.setModal(False)
        self.setStyleSheet("""
            QDialog#reminderCard {background:#f8f7ff;}
            QLabel {color:#34344c;background:transparent;}
            QLabel#reminderHeading {font-size:12px;color:#8177b3;}
            QLabel#reminderTitle {font-size:20px;font-weight:600;}
            QLabel#reminderError {color:#b74651;font-size:12px;}
            QPushButton {padding:8px 11px;border-radius:8px;background:white;
                         border:1px solid #e3dff4;color:#625c7d;}
            QPushButton#completeReminder {background:#6257ed;color:white;border:none;}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        self.heading = QLabel("事项已到提醒时间")
        self.heading.setObjectName("reminderHeading")
        layout.addWidget(self.heading)
        self.title = QLabel()
        self.title.setObjectName("reminderTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.error = QLabel()
        self.error.setObjectName("reminderError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        row = QHBoxLayout()
        self.snooze_ten = QPushButton("稍后 10 分钟")
        self.snooze_hour = QPushButton("稍后 1 小时")
        self.snooze_ten.clicked.connect(lambda: self.snooze_requested.emit(self.alert["id"], 10))
        self.snooze_hour.clicked.connect(lambda: self.snooze_requested.emit(self.alert["id"], 60))
        row.addWidget(self.snooze_ten)
        row.addWidget(self.snooze_hour)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.view = QPushButton("查看事项")
        self.complete = QPushButton()
        self.complete.setObjectName("completeReminder")
        self.view.clicked.connect(lambda: self.view_requested.emit(self.alert.get("task_id", "")))
        self.complete.clicked.connect(lambda: self.complete_requested.emit(self.alert["id"]))
        row.addWidget(self.view)
        row.addWidget(self.complete)
        layout.addLayout(row)
        self.update_alert(alert)

    def update_alert(self, alert, queue_count=1):
        self.alert = alert
        task = alert.get("task") or {}
        view_key = (str(task.get("title", "事项提醒"))[:200],
                    (task.get("repeat") or {}).get("kind", "once"),
                    task.get("priority"), alert.get("due_at"), queue_count)
        if view_key == getattr(self, "_view_key", None):
            return
        self._view_key = view_key
        self.title.setText(str(task.get("title", "事项提醒"))[:200])
        recurring = (task.get("repeat") or {}).get("kind", "once") != "once"
        self.complete.setText("完成本次" if recurring else "完成事项")
        color = {"low": "#4f9b8c", "normal": "#6e68b7", "important": "#bd842c", "urgent": "#c45367"}.get(task.get("priority"), "#6e68b7")
        self.heading.setStyleSheet(f"color:{color};font-weight:600;")
        due = str(alert.get("due_at", "")).replace("T", " ")[:16]
        self.detail.setText(f"预约时间：{due}" + (f"\n另有 {queue_count - 1} 项待提醒" if queue_count > 1 else ""))

    def show_error(self, message):
        self.error.setText(str(message))
        self.error.show()

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            self.show_error("请完成本次提醒，或选择稍后提醒。")


class NotebookController(QObject):
    stopped = Signal()
    error = Signal(str)
    store_changed = Signal(object)

    def __init__(self, store, open_toolbox=None, parent=None, *, clock=datetime.now,
                 enable_tray=True, test_mode=False):
        # A toolbox QWidget must never own and destroy this reminder lifetime.
        super().__init__(None if isinstance(parent, QWidget) else parent)
        self.store = store
        self.open_toolbox = open_toolbox
        self.clock = clock
        self.enable_tray = bool(enable_tray)
        self.test_mode = bool(test_mode)
        self.window = None
        self.active = False
        self.forwarded = False
        self.last_error = ""
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.tray = None
        self._menu = None
        self._card = None
        self._pending = {}
        self._announced = set()
        self._original_quit_policy = None
        self._lock = None
        self._server = None
        self._server_name = None
        self._clients = set()
        self._shutting_down = False
        self._last_ui_signature = None
        self._ui_dirty = True

    @property
    def card(self):
        return self._card

    def _report(self, message):
        self.last_error = str(message)
        self.error.emit(self.last_error)

    def _claim_instance(self):
        if self.test_mode:
            return True
        path = _store_path(self.store)
        if path is None:
            self._report("记事数据路径无效，暂未启动提醒。")
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._report(f"记事提醒无法打开数据目录：{exc}")
            return False
        identity = hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()[:32]
        self._server_name = f"QingjianNotebook-{identity}"
        lock = QLockFile(str(path.with_suffix(path.suffix + ".runtime.lock")))
        lock.setStaleLockTime(0)  # Never steal a lock merely because a reminder ran long.
        if not lock.tryLock(0):
            socket = QLocalSocket(self)
            socket.connectToServer(self._server_name)
            if socket.waitForConnected(500):
                socket.write(b"SHOW\n")
                socket.flush()
                socket.waitForBytesWritten(500)
                self.forwarded = True
            else:
                self._report("记事提醒已在运行，请从系统托盘打开；若仍在启动，请稍后再试。")
            socket.disconnectFromServer()
            socket.deleteLater()
            return False
        self._lock = lock
        # Only the process holding the data-path lock can remove a stale endpoint.
        QLocalServer.removeServer(self._server_name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._accept_connections)
        if not server.listen(self._server_name):
            lock.unlock()
            self._lock = None
            self._report("无法建立记事提醒的本地通信入口，请稍后重试。")
            server.deleteLater()
            return False
        self._server = server
        return True

    def _accept_connections(self):
        while self._server and self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            self._clients.add(client)
            client.readyRead.connect(lambda c=client: self._read_request(c))
            client.disconnected.connect(lambda c=client: self._forget_client(c))
            # A silent client cannot accumulate indefinitely or send arbitrary data.
            QTimer.singleShot(2000, client, client.abort)
            self._read_request(client)

    def _read_request(self, client):
        if client.bytesAvailable() > 64:
            client.abort()
            return
        if client.canReadLine():
            command = bytes(client.readLine(65)).strip()
            if command == b"SHOW":
                self.show_window()
            client.disconnectFromServer()

    def _forget_client(self, client):
        self._clients.discard(client)
        client.deleteLater()

    def _activate(self):
        if self.active:
            return True
        if not self._claim_instance():
            return False
        # Another toolbox can have saved data while this one was unopened.
        # Reload only after claiming ownership, before scheduling or editing.
        self._refresh_disk_store()
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("记事提醒需要桌面应用环境。")
        self._original_quit_policy = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        self.active = True
        self._create_tray()
        self.timer.start()
        self.tick()
        return True

    def _refresh_disk_store(self):
        from .notebook_store import NotebookStore
        path = _store_path(self.store)
        if isinstance(self.store, NotebookStore) and path is not None:
            self.store = NotebookStore(path)
            self.store_changed.emit(self.store)

    def _create_tray(self):
        if self.test_mode or not self.enable_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_icon(), self)
        self.tray.setToolTip("青简记事 · 到时提醒")
        self._menu = QMenu()
        for title, callback in (("打开记事小窗", self.show_window),
                                ("返回工具箱", self._return_toolbox),
                                ("退出记事提醒", self.shutdown)):
            action = QAction(title, self._menu)
            action.triggered.connect(callback)
            action.setEnabled(callback != self._return_toolbox or self.open_toolbox is not None)
            self._menu.addAction(action)
        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _return_toolbox(self):
        if self.open_toolbox:
            self.open_toolbox()

    def _make_window(self):
        from .notebook_ui import NotebookWindow
        window = NotebookWindow(self.store, clock=self.clock)
        window.close_requested.connect(self.hide_window)
        window.hide_requested.connect(self.hide_window)
        window.task_changed.connect(self._task_changed)
        if hasattr(window, "settings_changed"):
            window.settings_changed.connect(self._apply_settings)
        if hasattr(window, "layout_changed"):
            window.layout_changed.connect(lambda: self._apply_settings(self.store.settings))
        return window

    def _owned_windows(self):
        """UI owns its peers; controller governs the independent group lifetime."""
        if self.window is None:
            return []
        provider = getattr(self.window, "owned_windows", None)
        windows = provider() if callable(provider) else [self.window, *getattr(self.window, "companion_windows", [])]
        unique = []
        for window in windows:
            if window is not None and window not in unique:
                unique.append(window)
        return unique

    def _ui_signature(self, now=None):
        revision = getattr(self.store, "revision", None)
        return (id(self.store), revision, (now or self.clock()).date())

    def _refresh_ui(self, *, force=False, now=None):
        if self.window is None:
            return
        signature = self._ui_signature(now)
        if force or self._ui_dirty or signature != self._last_ui_signature:
            self.window.refresh()
            self._ui_dirty = False
            self._last_ui_signature = signature

    def show_window(self):
        if not self._activate():
            return False
        if self.window is None:
            self.window = self._make_window()
            self._ui_dirty = True
        self._refresh_ui()
        show_group = getattr(self.window, "show_windows", None)
        if callable(show_group):
            show_group()
        else:
            for window in self._owned_windows():
                window.showNormal()
        self._apply_settings(self.store.settings)
        self.window.raise_()
        self.window.activateWindow()
        # Persist that the user has deliberately enabled this separate component.
        path = _store_path(self.store)
        if path is not None and not path.exists():
            try:
                self.store.update_settings({"start_with_toolbox": bool(self.store.settings.get("start_with_toolbox", True))})
            except (OSError, ValueError, RuntimeError) as exc:
                self._report(f"记事设置未能保存：{exc}")
        return True

    def _apply_settings(self, settings):
        if self.window is None:
            return
        requested = bool(settings.get("always_on_top", False))
        for window in self._owned_windows():
            current = bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            if current != requested:
                was_visible = window.isVisible()
                window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, requested)
                if was_visible:
                    window.show()
            apply_glass(window, settings.get("opacity", 0.9))

    def start_if_enabled(self):
        if not self.active:
            self._refresh_disk_store()
        path = _store_path(self.store)
        if path is not None and path.is_file() and self.store.settings.get("start_with_toolbox", True):
            return self.show_window()
        return False

    def hide_window(self):
        if self.window is None:
            return
        if self.tray is not None and self.tray.isVisible():
            hide_group = getattr(self.window, "hide_windows", None)
            if callable(hide_group):
                hide_group()
            else:
                for window in self._owned_windows():
                    window.hide()
        else:
            # No tray means the taskbar is the user's only way back in.
            minimize_group = getattr(self.window, "minimize_windows", None)
            if callable(minimize_group):
                minimize_group()
            else:
                for window in self._owned_windows():
                    window.showMinimized()

    def _task_changed(self):
        self._ui_dirty = True
        self._refresh_ui()
        self.tick()

    def tick(self):
        if not self.active or self._shutting_down:
            return
        try:
            now = self.clock()
            alerts = self.store.collect_due(now)
        except (OSError, ValueError, RuntimeError) as exc:
            self._report(f"暂时无法检查提醒，稍后会重试：{exc}")
            return
        self._refresh_ui(now=now)
        self._pending = {str(alert["id"]): alert for alert in alerts}
        # Seen state only lasts while this occurrence remains pending. Snoozing,
        # editing or completing removes it; a due snooze can then notify again.
        self._announced.intersection_update(self._pending)
        if self._card is not None and self._card.alert["id"] not in self._pending:
            self._discard_card()
        if self._card is None and self._pending:
            self._show_next()
        elif self._card is not None:
            self._card.update_alert(self._pending[self._card.alert["id"]], len(self._pending))

    def _show_next(self):
        alert = next(iter(self._pending.values()))
        self._card = ReminderCard(alert)
        self._card.complete_requested.connect(self.complete_alert)
        self._card.snooze_requested.connect(self.snooze_alert)
        self._card.view_requested.connect(self.view_task)
        self._card.update_alert(alert, len(self._pending))
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self._card.move(area.right() - self._card.width() - 22,
                            area.bottom() - self._card.height() - 22)
        self._card.show()
        self._card.raise_()
        alert_id = str(alert["id"])
        if alert_id not in self._announced:
            self._announced.add(alert_id)
            if not self.test_mode and self.tray is not None:
                title = str((alert.get("task") or {}).get("title", "事项提醒"))
                self.tray.showMessage("青简记事提醒", title,
                                      QSystemTrayIcon.MessageIcon.Information, 10000)
            if not self.test_mode and self.store.settings.get("sound", False):
                QApplication.beep()

    def _discard_card(self):
        if self._card is not None:
            card, self._card = self._card, None
            card.allow_close = True
            card.close()
            card.deleteLater()

    def _mutate_alert(self, alert_id, operation):
        if str(alert_id) not in self._pending:
            return False
        try:
            result = operation()
            if result is False:
                raise RuntimeError(getattr(self.store, "last_error", "") or "修改未能保存。")
        except (OSError, ValueError, RuntimeError) as exc:
            message = f"未能保存，请重试：{exc}"
            self._report(message)
            if self._card is not None:
                self._card.show_error(message)
            return False
        self._pending.pop(str(alert_id), None)
        self._announced.discard(str(alert_id))
        self._discard_card()
        if self.window is not None:
            self._ui_dirty = True
            self._refresh_ui()
        self.tick()
        return True

    def complete_alert(self, alert_id):
        return self._mutate_alert(alert_id, lambda: self.store.acknowledge(alert_id, self.clock()))

    def snooze_alert(self, alert_id, minutes=10):
        if minutes not in (10, 60):
            return False
        return self._mutate_alert(alert_id, lambda: self.store.snooze(alert_id, minutes, self.clock()))

    def view_task(self, task_id):
        if self.show_window() and hasattr(self.window, "focus_task"):
            self.window.focus_task(task_id)

    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        was_active = self.active
        self.active = False
        self.timer.stop()
        self._discard_card()
        self._pending.clear()
        self._announced.clear()
        if self.window is not None:
            window, self.window = self.window, None
            close_group = getattr(window, "close_windows", None)
            if callable(close_group):
                close_group()
            else:
                peers = list(getattr(window, "companion_windows", []))
                for peer in [*peers, window]:
                    peer.allow_close = True
                    peer.close()
                    if peer is not window:
                        peer.deleteLater()
            window.deleteLater()
        self._ui_dirty = True
        self._last_ui_signature = None
        if self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
            self._menu = None
        for client in tuple(self._clients):
            client.abort()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
        app = QApplication.instance()
        if app is not None and self._original_quit_policy is not None:
            app.setQuitOnLastWindowClosed(self._original_quit_policy)
        self._original_quit_policy = None
        self._shutting_down = False
        if was_active:
            self.stopped.emit()
