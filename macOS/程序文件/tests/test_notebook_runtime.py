"""Reminder lifecycle and durable alert interactions without real notifications."""
from datetime import datetime, timedelta
import copy

import pytest
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QApplication, QWidget

from qingjian.notebook_runtime import NotebookController, ReminderCard


class FakeWindow(QWidget):
    close_requested = Signal()
    hide_requested = Signal()
    task_changed = Signal()
    settings_changed = Signal(dict)

    def __init__(self):
        super().__init__(None)
        self.refreshed = 0
        self.focused = None
        self.allow_close = False

    def refresh(self):
        self.refreshed += 1

    def focus_task(self, task_id):
        self.focused = task_id


class FakeStore:
    def __init__(self, path=None):
        self.path = path
        self.settings = {"opacity": .9, "always_on_top": False, "start_with_toolbox": True}
        self.alerts = []
        self.last_error = ""
        self.fail = False
        self.checks = 0

    def update_settings(self, values):
        self.settings.update(values)
        if self.path:
            self.path.write_text("{}", encoding="utf-8")
        return self.settings

    def collect_due(self, now):
        self.checks += 1
        if self.fail == "collect":
            raise OSError("磁盘不可用")
        return copy.deepcopy([item for item in self.alerts
                              if not item.get("snoozed_until") or datetime.fromisoformat(item["snoozed_until"]) <= now])

    def acknowledge(self, alert_id, now):
        if self.fail:
            raise OSError("磁盘不可用")
        self.alerts = [item for item in self.alerts if item["id"] != alert_id]
        return True

    def snooze(self, alert_id, minutes, now):
        if self.fail:
            raise OSError("磁盘不可用")
        item = next(item for item in self.alerts if item["id"] == alert_id)
        item["snoozed_until"] = (now + timedelta(minutes=minutes)).isoformat()
        return item


def alert(identifier="first", *, recurring=False):
    return {"id": identifier, "task_id": "task-" + identifier,
            "due_at": "2026-10-29T10:00:00", "snoozed_until": None,
            "task": {"title": "核对工作材料", "priority": "urgent",
                     "repeat": {"kind": "daily" if recurring else "once"}}}


@pytest.fixture
def controller(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "notebook.json")
    now = [datetime(2026, 10, 29, 10)]
    controller = NotebookController(store, clock=lambda: now[0], test_mode=True)
    monkeypatch.setattr(controller, "_make_window", FakeWindow)
    yield controller, store, now
    controller.shutdown()
    QApplication.processEvents()


def test_unused_reminder_has_no_background_lifetime(controller):
    obj, store, _ = controller
    policy = QApplication.instance().quitOnLastWindowClosed()
    assert not obj.start_if_enabled()
    assert not obj.active and not obj.timer.isActive()
    assert store.checks == 0 and not store.path.exists()
    assert QApplication.instance().quitOnLastWindowClosed() == policy


def test_explicit_open_persists_enablement_and_restores_exit_policy(controller):
    obj, store, _ = controller
    policy = QApplication.instance().quitOnLastWindowClosed()
    assert obj.show_window() is True
    assert store.path.exists() and obj.active and obj.timer.isActive()
    assert not QApplication.instance().quitOnLastWindowClosed()
    stopped = []
    obj.stopped.connect(lambda: stopped.append(True))
    obj.shutdown()
    assert stopped == [True] and obj.window is None and not obj.timer.isActive()
    assert QApplication.instance().quitOnLastWindowClosed() == policy
    obj.shutdown()
    assert stopped == [True]


def test_start_preference_and_existing_file_are_both_required(controller):
    obj, store, _ = controller
    store.update_settings({"start_with_toolbox": False})
    assert not obj.start_if_enabled()
    assert obj.show_window()
    assert store.settings["start_with_toolbox"] is False


def test_saved_enabled_reminder_starts(controller):
    obj, store, _ = controller
    store.update_settings({"start_with_toolbox": True})
    assert obj.start_if_enabled() and obj.active


def test_toolbox_window_cannot_own_controller(controller):
    _, store, _ = controller
    toolbox = QWidget()
    obj = NotebookController(store, parent=toolbox, test_mode=True)
    assert obj.parent() is None
    toolbox.close()
    obj.shutdown()
    toolbox.deleteLater()


def test_timer_reuses_single_nonblocking_card_without_duplicate_notifications(controller):
    obj, store, _ = controller
    store.alerts = [alert()]
    assert obj.show_window()
    card = obj.card
    assert card and not card.isModal() and card.isVisible()
    assert card.parent() is None
    for _ in range(5):
        obj.tick()
    assert obj.card is card and obj._announced == {"first"}
    assert card.complete.text() == "完成事项"


def test_complete_next_queued_alert_is_shown(controller):
    obj, store, _ = controller
    store.alerts = [alert(), alert("second", recurring=True)]
    obj.show_window()
    assert "另有 1 项" in obj.card.detail.text()
    assert obj.complete_alert("first")
    assert obj.card.alert["id"] == "second" and obj.card.complete.text() == "完成本次"
    assert obj.complete_alert("second")
    assert obj.card is None and not obj._pending


@pytest.mark.parametrize("minutes", [10, 60])
def test_snooze_reappears_after_wall_clock_advance(controller, minutes):
    obj, store, now = controller
    store.alerts = [alert()]
    obj.show_window()
    assert obj.snooze_alert("first", minutes)
    assert obj.card is None
    now[0] += timedelta(minutes=minutes - 1)
    obj.tick()
    assert obj.card is None
    now[0] += timedelta(minutes=2)
    obj.tick()
    assert obj.card and obj.card.alert["id"] == "first"


@pytest.mark.parametrize("action", ["complete", "snooze"])
def test_failed_persistence_keeps_visible_alert(controller, action):
    obj, store, _ = controller
    store.alerts = [alert()]
    obj.show_window()
    card = obj.card
    store.fail = True
    result = obj.complete_alert("first") if action == "complete" else obj.snooze_alert("first", 10)
    assert result is False and obj.card is card and card.isVisible()
    assert "未能保存" in card.error.text() and store.alerts


def test_collect_failure_does_not_remove_existing_card(controller):
    obj, store, _ = controller
    store.alerts = [alert()]
    obj.show_window()
    card = obj.card
    store.fail = "collect"
    obj.tick()
    assert obj.card is card and "稍后会重试" in obj.last_error


def test_deleted_or_completed_task_closes_stale_card(controller):
    obj, store, _ = controller
    store.alerts = [alert()]
    obj.show_window()
    store.alerts = []
    obj.tick()
    assert obj.card is None


def test_no_tray_keeps_taskbar_entry_and_view_locates_task(controller):
    obj, _, _ = controller
    obj.show_window()
    obj.hide_window()
    assert obj.window.isVisible() and obj.window.isMinimized()
    obj.view_task("selected-task")
    assert obj.window.isVisible() and not obj.window.isMinimized()
    assert obj.window.focused == "selected-task"


def test_closing_card_cannot_silently_discard_reminder(controller):
    obj, store, _ = controller
    store.alerts = [alert()]
    obj.show_window()
    obj.card.close()
    assert obj.card.isVisible() and "稍后提醒" in obj.card.error.text()


def test_tray_hides_notebook_and_return_toolbox_callback(controller):
    obj, _, _ = controller
    called = []
    obj.open_toolbox = lambda: called.append(True)
    obj.show_window()
    class FakeTray:
        def isVisible(self): return True
    obj.tray = FakeTray()
    obj.hide_window()
    assert not obj.window.isVisible()
    obj._return_toolbox()
    assert called == [True]
    obj.tray = None


def test_plaintext_title_and_importance_are_preserved():
    value = alert()
    value["task"]["title"] = "<b>原样标题</b>"
    card = ReminderCard(value)
    try:
        assert card.title.textFormat() == Qt.TextFormat.PlainText
        assert card.title.text() == "<b>原样标题</b>"
        assert "#c45367" in card.heading.styleSheet()
    finally:
        card.allow_close = True
        card.close()
        card.deleteLater()


def test_real_store_pending_reappears_after_controller_restart(tmp_path, monkeypatch):
    from qingjian.notebook_store import NotebookStore
    now = datetime(2026, 10, 29, 10)
    path = tmp_path / "durable.json"
    store = NotebookStore(path)
    store.upsert_task({"title": "到期后必须保留", "remind_at": "2026-10-29T09:00:00"}, now=now)
    first = NotebookController(store, clock=lambda: now, test_mode=True)
    monkeypatch.setattr(first, "_make_window", FakeWindow)
    try:
        first.show_window()
        identifier = first.card.alert["id"]
    finally:
        first.shutdown()
    second = NotebookController(NotebookStore(path), clock=lambda: now, test_mode=True)
    monkeypatch.setattr(second, "_make_window", FakeWindow)
    try:
        second.show_window()
        assert second.card.alert["id"] == identifier
        assert second.complete_alert(identifier)
        assert second.store.tasks[0]["completed"] is True
    finally:
        second.shutdown()


def test_ownership_reload_preserves_items_saved_after_toolbox_open(tmp_path, monkeypatch):
    from qingjian.notebook_store import NotebookStore
    now = datetime(2026, 10, 29, 10)
    path = tmp_path / "stale.json"
    stale = NotebookStore(path)
    first = NotebookStore(path)
    saved = first.upsert_task({"title": "另一窗口新建事项"}, now=now)
    obj = NotebookController(stale, clock=lambda: now, test_mode=True)
    monkeypatch.setattr(obj, "_make_window", FakeWindow)
    try:
        assert not stale.tasks
        assert obj.show_window()
        assert obj.store.find_task(saved["id"])["title"] == "另一窗口新建事项"
        obj.store.update_settings({"always_on_top": True})
        assert NotebookStore(path).find_task(saved["id"]) is not None
    finally:
        obj.shutdown()


def test_real_local_single_instance_forwards_show_and_never_checks_twice(tmp_path, monkeypatch):
    path = tmp_path / "single.json"
    first_store, second_store = FakeStore(path), FakeStore(path)
    first = NotebookController(first_store, enable_tray=False)
    second = NotebookController(second_store, enable_tray=False)
    monkeypatch.setattr(first, "_make_window", FakeWindow)
    monkeypatch.setattr(second, "_make_window", FakeWindow)
    try:
        assert first.show_window()
        first.window.hide()
        assert not second.show_window() and second.forwarded
        assert not second.active and second_store.checks == 0
        from PySide6.QtTest import QTest
        QTest.qWait(150)
        assert first.window.isVisible()
        assert first.active and first._lock.isLocked()
    finally:
        second.shutdown()
        first.shutdown()


def test_real_notebook_window_signals_work_with_independent_runtime():
    from qingjian.notebook_store import NotebookStore
    now = datetime(2026, 10, 29, 10)
    store = NotebookStore(None)
    obj = NotebookController(store, clock=lambda: now, test_mode=True)
    try:
        assert obj.show_window()
        assert obj.window.parent() is None
        value = store.upsert_task({"title": "运行时与独立窗口联动",
                                   "remind_at": "2026-10-29T10:00:00"}, now=now)
        obj.window.task_changed.emit()
        assert obj.card and obj.card.alert["task_id"] == value["id"]
        obj.window.close_requested.emit()
        assert obj.window.isVisible() and obj.window.isMinimized()
        obj.view_task(value["id"])
        assert not obj.window.isMinimized()
        assert obj.complete_alert(obj.card.alert["id"])
        assert store.find_task(value["id"])["completed"]
    finally:
        obj.shutdown()


class FakeGroupWindow(FakeWindow):
    layout_changed = Signal()

    def __init__(self):
        super().__init__()
        self.companion_windows = [FakeWindow()]
        self.group_closed = False

    def owned_windows(self):
        return [self, *self.companion_windows]

    def show_windows(self):
        for window in self.owned_windows():
            window.showNormal()

    def hide_windows(self):
        for window in self.owned_windows():
            window.hide()

    def minimize_windows(self):
        for window in self.owned_windows():
            window.showMinimized()

    def close_windows(self):
        self.group_closed = True
        for window in self.owned_windows():
            window.allow_close = True
            window.close()
            if window is not self:
                window.deleteLater()


def test_split_float_group_lifecycle_and_glass_apply(controller, monkeypatch):
    from qingjian import notebook_runtime
    obj, store, _ = controller
    monkeypatch.setattr(obj, "_make_window", FakeGroupWindow)
    effects = []
    monkeypatch.setattr(notebook_runtime, "apply_glass", lambda window, opacity: effects.append(window))
    assert obj.show_window()
    group = obj.window
    peers = group.owned_windows()
    assert len(peers) == 2 and all(peer.isVisible() for peer in peers)
    assert effects == peers
    obj.hide_window()
    assert all(peer.isVisible() and peer.isMinimized() for peer in peers)
    obj.show_window()
    assert all(peer.isVisible() and not peer.isMinimized() for peer in peers)
    class FakeTray:
        def isVisible(self): return True
    obj.tray = FakeTray()
    obj.hide_window()
    assert not any(peer.isVisible() for peer in peers)
    obj.tray = None
    obj._apply_settings({**store.settings, "always_on_top": True})
    assert all(peer.windowFlags() & Qt.WindowType.WindowStaysOnTopHint for peer in peers)
    obj.shutdown()
    assert group.group_closed and not obj.active


def test_unchanged_show_and_timer_do_not_refresh_list_or_restyle_card(controller, monkeypatch):
    obj, store, _ = controller
    store.revision = 1
    store.alerts = [alert()]
    obj.show_window()
    refreshes = obj.window.refreshed
    calls = []
    original = obj.card.heading.setStyleSheet
    monkeypatch.setattr(obj.card.heading, "setStyleSheet", lambda value: (calls.append(value), original(value)))
    for _ in range(10):
        obj.tick()
        obj.show_window()
    assert obj.window.refreshed == refreshes and calls == []
    store.revision += 1
    obj.tick()
    assert obj.window.refreshed == refreshes + 1


def test_midnight_refreshes_calendar_once_without_delaying_reminders(controller):
    obj, store, now = controller
    now[0] = datetime(2026, 10, 29, 23, 59, 59)
    obj.show_window()
    refreshes = obj.window.refreshed
    now[0] += timedelta(seconds=2)
    store.alerts = [alert()]
    obj.tick()
    assert obj.window.refreshed == refreshes + 1
    assert obj.card is not None
    obj.tick()
    assert obj.window.refreshed == refreshes + 1

