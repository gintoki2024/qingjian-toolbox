"""Real Qt editing, recurrence selection, independent lifecycle and small layout."""
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QEvent, QPoint, QRect, QRectF, QTime, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDateTimeEdit, QDialog, QFrame, QLabel, QMessageBox, QScrollArea, QToolButton, QWidget

from qingjian.notebook_store import NotebookStore
from qingjian.notebook_ui import NotebookWindow, TaskEditorDialog, PRIORITIES, _fit_notebook_geometry


NOW = datetime(2026, 10, 29, 9, 0, 0)


@pytest.fixture
def store():
    return NotebookStore()


@pytest.fixture
def notebook(store):
    window = NotebookWindow(store, clock=lambda: NOW)
    window.show()
    QApplication.processEvents()
    yield window
    window.allow_close = True
    window.close()
    window.deleteLater()
    QApplication.processEvents()


def add(store, title="准备会议资料", day="2026-10-29", **fields):
    return store.upsert_task({"title": title, "date": day, **fields}, now=NOW)


def ids(window):
    return [window.task_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(window.task_list.count())]


def editor(store, **kwargs):
    dialog = TaskEditorDialog(store, clock=lambda: NOW, **kwargs)
    return dialog


def dispose(dialog):
    dialog.close()
    dialog.deleteLater()
    QApplication.processEvents()


def test_top_level_window_has_no_toolbox_owner_and_does_not_change_application_exit_policy(store):
    owner = QWidget()
    app = QApplication.instance()
    original = app.quitOnLastWindowClosed()
    window = NotebookWindow(store, parent=owner, clock=lambda: NOW)
    assert window.parent() is None
    assert window.isWindow()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert app.quitOnLastWindowClosed() == original
    window.allow_close = True
    window.close()
    window.deleteLater()
    owner.deleteLater()


def test_calendar_selects_today_has_full_range_and_can_jump_year_month(notebook):
    assert notebook.calendar.minimumDate() == QDate(1900, 1, 1)
    assert notebook.calendar.maximumDate() == QDate(2100, 12, 31)
    assert notebook.calendar.selectedDate() == QDate(2026, 10, 29)
    notebook.year_spin.setValue(2032)
    notebook.month_combo.setCurrentIndex(1)
    assert (notebook.calendar.yearShown(), notebook.calendar.monthShown()) == (2032, 2)
    notebook.select_date(date(2032, 2, 29))
    assert notebook.calendar.selectedDate() == QDate(2032, 2, 29)
    assert "2月29日" in notebook.date_headline.text()
    assert notebook.date_lunar.text()


def test_day_view_and_month_marks_follow_future_recurrences(notebook, store):
    daily = add(store, "每日核对", remind_at="2026-10-29T10:00:00", repeat={"kind": "daily"})
    one = add(store, "只在今天记事")
    notebook.refresh()
    assert set(ids(notebook)) == {daily["id"], one["id"]}
    notebook.select_date(date(2026, 10, 30))
    assert ids(notebook) == [daily["id"]]
    notebook.calendar.grab()
    assert notebook.calendar._marks_cache[date(2026, 10, 30)] == ("normal",)
    notebook.select_date(date(2026, 10, 28))
    assert ids(notebook) == []


def test_search_body_all_view_and_completed_toggle(notebook, store):
    first = add(store, "当天会议", body="地址：三号会议室")
    later = add(store, "下周提交", day="2026-11-03")
    notebook.refresh()
    notebook.search_edit.setText("三号")
    assert ids(notebook) == [first["id"]]
    notebook.search_edit.clear()
    notebook.view_buttons["all"].click()
    assert set(ids(notebook)) == {first["id"], later["id"]}
    notebook._complete_task(first["id"])
    assert ids(notebook) == [later["id"]]
    notebook.completed_check.setChecked(True)
    assert set(ids(notebook)) == {first["id"], later["id"]}
    assert store.find_task(first["id"])["completed"] is True
    notebook._complete_task(first["id"], False)
    assert store.find_task(first["id"])["completed"] is False


def test_upcoming_limits_future_window_keeps_overdue_and_ignores_plain_notes(notebook, store):
    overdue = add(store, "待处理预约", remind_at="2026-10-28T10:00:00")
    near = add(store, "下周提醒", remind_at="2026-11-03T10:00:00")
    add(store, "两个月后", remind_at="2026-12-29T10:00:00")
    add(store, "没有提醒的笔记")
    notebook.refresh()
    notebook.view_buttons["upcoming"].click()
    assert set(ids(notebook)) == {overdue["id"], near["id"]}


def test_all_four_priority_markers_render_in_real_cards(notebook, store):
    for priority in ("low", "normal", "important", "urgent"):
        add(store, title=priority, priority=priority)
    notebook.refresh()
    assert notebook.task_list.count() == 4
    for index in range(notebook.task_list.count()):
        item = notebook.task_list.item(index)
        notebook.task_list.scrollToItem(item)
        QApplication.processEvents()
        rect = notebook.task_list.visualItemRect(item)
        rendered = notebook.task_list.viewport().grab().toImage()
        task = notebook._tasks_by_id[item.data(Qt.ItemDataRole.UserRole)]
        assert rendered.pixelColor(rect.left() + 2, rect.top() + 20) == QColor(PRIORITIES[task["priority"]][1])
        assert notebook.task_list.itemWidget(item) is None


def test_complete_button_writes_store_and_emits_change(notebook, store):
    task = add(store)
    notebook.refresh()
    spy = QSignalSpy(notebook.task_changed)
    QApplication.processEvents()
    action = notebook.task_action_rect(0, "complete")
    assert action.width() >= 22 and action.height() >= 22
    QTest.mouseClick(notebook.task_list.viewport(), Qt.MouseButton.LeftButton, pos=action.center())
    assert store.find_task(task["id"])["completed"]
    assert spy.count() == 1
    assert notebook.task_list.count() == 0


def test_delete_requires_confirmation_and_cancels_all_future_schedule(notebook, store, monkeypatch):
    task = add(store, remind_at="2026-10-29T10:00:00", repeat={"kind": "daily"})
    notebook.refresh()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    notebook._remove_task(task["id"])
    assert store.find_task(task["id"])
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    notebook._remove_task(task["id"])
    assert store.find_task(task["id"]) is None
    assert notebook.task_list.count() == 0


def test_close_and_hide_delegate_lifecycle_but_force_close_is_available(notebook, store):
    closed, hidden = QSignalSpy(notebook.close_requested), QSignalSpy(notebook.hide_requested)
    notebook.close()
    assert closed.count() == 1
    assert notebook.isVisible()
    notebook._request_hide()
    assert hidden.count() == 1
    assert store.settings["calendar_geometry"][2:] == [notebook.width(), notebook.height()]
    assert store.settings["tasks_geometry"][2:] == [notebook.tasks_window.width(), notebook.tasks_window.height()]
    notebook.allow_close = True
    notebook.close()
    assert not notebook.isVisible()


def test_opacity_keeps_text_opaque_and_top_toggle_persists(notebook, store):
    notebook.set_glass_opacity(.65)
    assert notebook.panel.opacity == .65
    assert notebook.windowOpacity() == 1
    notebook.pin_button.click()
    assert store.settings["always_on_top"] is True
    assert notebook.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    notebook.pin_button.click()
    assert store.settings["always_on_top"] is False


def test_focus_task_opens_all_and_clears_filters(notebook, store):
    task = add(store, day="2027-03-04", completed=True)
    notebook.search_edit.setText("不匹配")
    assert notebook.focus_task(task["id"])
    assert notebook._view == "all"
    assert notebook.search_edit.text() == ""
    assert notebook.task_list.currentItem().data(Qt.ItemDataRole.UserRole) == task["id"]
    assert notebook.focus_task("missing") is False


@pytest.mark.parametrize("size", [(440, 700), (380, 580)])
def test_window_calendar_controls_list_and_empty_state_do_not_overlap(notebook, size):
    notebook.set_layout_mode("combined")
    notebook.resize(*size)
    QApplication.processEvents()
    # These adjacent surfaces must stay separated even at minimum size.
    assert notebook.calendar.mapTo(notebook, QPoint(0, notebook.calendar.height())).y() < notebook.list_heading.mapTo(notebook, QPoint()).y()
    assert notebook.task_list.height() >= 75
    assert notebook.empty_label.geometry() == notebook.task_list.viewport().rect()
    assert notebook.task_list.mapTo(notebook, QPoint(0, notebook.task_list.height())).y() < notebook.height() - 15
    assert notebook.minimumSizeHint().height() <= notebook.height()
    assert not notebook.grab().isNull()


def test_new_editor_prefills_selected_date_and_saves_plain_note(store):
    dialog = editor(store, selected_date=date(2027, 2, 3))
    dialog.title_edit.setText("  准备培训  ")
    dialog.body_edit.setPlainText("名单\n材料\n会议室")
    dialog.priority_combo.setCurrentIndex(dialog.priority_combo.findData("important"))
    assert dialog.date_edit.date() == QDate(2027, 2, 3)
    assert dialog.remind_edit.date() == QDate(2027, 2, 3)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    saved = dialog.saved_task
    assert saved["title"] == "准备培训"
    assert saved["date"] == "2027-02-03"
    assert saved["remind_at"] is None
    assert saved["priority"] == "important"
    assert saved["body"] == "名单\n材料\n会议室"
    dispose(dialog)


@pytest.mark.parametrize("kind", ["once", "daily", "weekdays", "weekly", "monthly", "yearly", "interval"])
def test_editor_saves_every_recurrence_kind_and_lead_time(store, kind):
    dialog = editor(store)
    dialog.title_edit.setText("预约提交资料")
    dialog.reminder_check.setChecked(True)
    dialog.remind_edit.setDateTime(QDateTime(datetime(2026, 11, 2, 10, 15)))
    dialog.repeat_combo.setCurrentIndex(dialog.repeat_combo.findData(kind))
    dialog.lead_spin.setValue(30)
    if kind == "weekly":
        for index, checkbox in enumerate(dialog.weekday_checks):
            checkbox.setChecked(index in (0, 2, 4))
    if kind == "interval":
        dialog.interval_spin.setValue(45)
        dialog.interval_unit.setCurrentIndex(dialog.interval_unit.findData("minutes"))
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    saved = dialog.saved_task
    assert saved["remind_at"] == "2026-11-02T10:15:00"
    assert saved["lead_minutes"] == 30
    assert saved["repeat"]["kind"] == kind
    if kind == "weekly":
        assert saved["repeat"]["weekdays"] == [0, 2, 4]
    if kind == "interval":
        assert saved["repeat"]["interval"] == 45
        assert saved["repeat"]["unit"] == "minutes"
    dispose(dialog)


def test_reminder_hour_minute_is_a_visible_independent_keyboard_input_and_reopens(tmp_path):
    path = tmp_path / "notes.json"
    store = NotebookStore(path)
    dialog = editor(store, selected_date=date(2026, 11, 2))
    try:
        dialog.resize(380, 500)
        dialog.show()
        dialog.title_edit.setText("下午提交材料")
        dialog.reminder_check.setChecked(True)
        dialog.findChild(QScrollArea).ensureWidgetVisible(dialog.remind_time_edit)
        QApplication.processEvents()
        assert dialog.remind_edit.isVisible()
        assert dialog.remind_time_edit.isVisible()
        assert dialog.remind_edit.mapTo(dialog, QPoint()).x() < dialog.remind_time_edit.mapTo(dialog, QPoint()).x()
        assert any(label.text() == "提醒时刻（24小时制）" for label in dialog.findChildren(QLabel))
        dialog.remind_time_edit.setFocus()
        dialog.remind_time_edit.setSelectedSection(QDateTimeEdit.Section.HourSection)
        QTest.keyClicks(dialog.remind_time_edit, "14")
        dialog.remind_time_edit.setSelectedSection(QDateTimeEdit.Section.MinuteSection)
        QTest.keyClicks(dialog.remind_time_edit, "37")
        QTest.keyClick(dialog.remind_time_edit, Qt.Key.Key_Tab)
        assert dialog.remind_time_edit.text() == "14:37"
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        task_id = dialog.saved_task["id"]
    finally:
        dispose(dialog)
    reopened = NotebookStore(path)
    task = reopened.find_task(task_id)
    assert task["remind_at"] == task["next_due"] == "2026-11-02T14:37:00"
    another = editor(reopened, task=task)
    try:
        assert another.remind_edit.date() == QDate(2026, 11, 2)
        assert another.remind_time_edit.time() == QTime(14, 37)
        another.body_edit.setPlainText("二次编辑不改变预约时间")
        another.save()
        assert reopened.find_task(task_id)["remind_at"] == task["remind_at"]
    finally:
        dispose(another)


@pytest.mark.parametrize("kind,next_due", [
    ("once", None), ("daily", datetime(2026, 11, 3, 14, 37)),
    ("interval", datetime(2026, 11, 2, 15, 22)),
])
def test_separate_time_input_controls_exact_minute_alert_and_recurrence(store, kind, next_due):
    dialog = editor(store, selected_date=date(2026, 11, 2))
    try:
        dialog.title_edit.setText("精确到分钟的预约")
        dialog.reminder_check.setChecked(True)
        dialog.remind_time_edit.setTime(QTime(14, 37))
        dialog.repeat_combo.setCurrentIndex(dialog.repeat_combo.findData(kind))
        dialog.lead_spin.setValue(5)
        if kind == "interval":
            dialog.interval_spin.setValue(45)
            dialog.interval_unit.setCurrentIndex(dialog.interval_unit.findData("minutes"))
        dialog.save()
        task_id = dialog.saved_task["id"]
    finally:
        dispose(dialog)
    assert store.collect_due(datetime(2026, 11, 2, 14, 31, 59)) == []
    alerts = store.collect_due(datetime(2026, 11, 2, 14, 32))
    assert len(alerts) == 1
    assert alerts[0]["due_at"] == "2026-11-02T14:37:00"
    store.acknowledge(alerts[0]["id"], datetime(2026, 11, 2, 14, 32))
    if next_due is None:
        assert store.find_task(task_id)["completed"]
        assert store.collect_due(datetime(2026, 11, 3, 14, 37)) == []
    else:
        assert store.find_task(task_id)["next_due"] == next_due.isoformat()
        assert store.collect_due(next_due - timedelta(minutes=5, seconds=1)) == []
        assert store.collect_due(next_due - timedelta(minutes=5))[0]["due_at"] == next_due.isoformat()


@pytest.mark.parametrize("hour,minute", [(0, 0), (23, 59)])
def test_separate_time_can_select_midnight_and_last_minute_without_changing_date(store, hour, minute):
    dialog = editor(store, selected_date=date(2026, 11, 2))
    try:
        dialog.title_edit.setText("日期边界预约")
        dialog.reminder_check.setChecked(True)
        dialog.remind_time_edit.setTime(QTime(hour, minute))
        dialog.save()
        assert dialog.saved_task["remind_at"] == f"2026-11-02T{hour:02d}:{minute:02d}:00"
    finally:
        dispose(dialog)


def test_changing_explicit_time_resets_old_seconds_and_next_occurrence(store):
    task = add(store, remind_at="2026-10-29T10:00:37", repeat={"kind": "daily", "interval": 2})
    store.collect_due(datetime(2026, 10, 29, 11))
    dialog = editor(store, task=store.find_task(task["id"]))
    try:
        assert dialog.remind_time_edit.text() == "10:00"
        dialog.remind_time_edit.setTime(QTime(14, 37))
        dialog.save()
        saved = store.find_task(task["id"])
        assert saved["remind_at"] == saved["next_due"] == "2026-10-29T14:37:00"
        assert saved["repeat"] == task["repeat"]
        assert store.pending_alerts == []
    finally:
        dispose(dialog)


def test_editor_requires_title_and_weekly_selection_without_closing(store):
    dialog = editor(store)
    dialog.show()
    dialog.save()
    assert dialog.isVisible()
    assert "名称" in dialog.error_label.text()
    dialog.title_edit.setText("每周核查")
    dialog.reminder_check.setChecked(True)
    dialog.repeat_combo.setCurrentIndex(dialog.repeat_combo.findData("weekly"))
    for checkbox in dialog.weekday_checks:
        checkbox.setChecked(False)
    dialog.save()
    assert dialog.isVisible()
    assert "至少" in dialog.error_label.text()
    assert store.tasks == []
    dispose(dialog)


def test_editor_write_failure_retains_values_and_dialog(store, monkeypatch):
    dialog = editor(store)
    dialog.show()
    dialog.title_edit.setText("不可丢失的待办")
    monkeypatch.setattr(store, "upsert_task", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("数据目录暂时不可写")))
    dialog.save()
    assert dialog.isVisible()
    assert dialog.title_edit.text() == "不可丢失的待办"
    assert "不可写" in dialog.error_label.text()
    assert dialog.saved_task is None
    dispose(dialog)


def test_editing_body_does_not_replay_seconds_or_custom_repeat_interval(store):
    task = add(store, remind_at="2026-10-29T10:00:37", repeat={"kind": "daily", "interval": 2})
    # Simulate a previous occurrence already passing before a body-only edit.
    store.collect_due(datetime(2026, 10, 29, 11))
    before = store.find_task(task["id"])
    dialog = editor(store, task=before)
    dialog.body_edit.setPlainText("补充资料清单")
    dialog.save()
    after = store.find_task(task["id"])
    assert after["remind_at"] == before["remind_at"]
    assert after["repeat"] == before["repeat"]
    assert after["next_due"] == before["next_due"]
    assert len(store.tasks) == 1
    dispose(dialog)


def test_editor_small_height_scrolls_and_leaves_save_visible(store):
    dialog = editor(store)
    dialog.resize(380, 500)
    dialog.show()
    dialog.reminder_check.setChecked(True)
    dialog.repeat_combo.setCurrentIndex(dialog.repeat_combo.findData("weekly"))
    QApplication.processEvents()
    point = dialog.save_button.mapTo(dialog, QPoint(0, 0))
    assert point.y() + dialog.save_button.height() < dialog.height()
    assert dialog.minimumSizeHint().height() <= dialog.height()
    assert not dialog.grab().isNull()
    dispose(dialog)


def test_new_reminder_at_month_end_keeps_selected_day(store):
    dialog = TaskEditorDialog(store, selected_date=date(2027, 1, 31), clock=lambda: datetime(2027, 1, 31, 23, 45))
    assert dialog.remind_edit.date() == QDate(2027, 1, 31)
    dispose(dialog)


def test_parented_editor_renders_opaque_surface_and_filled_controls_with_literal_note_text(notebook, store):
    # A parented dialog inherits ancestor style sheets: this is the real edit
    # path, unlike a standalone editor that can conceal a selector regression.
    title = "<b>事项原文</b>"
    body = '<a href="https://example.invalid">笔记原文</a>'
    task = add(store, title=title, body=body)
    notebook.refresh()
    item = notebook.task_list.item(0)
    assert item.text() == title  # The delegate paints it with QPainter.drawText.
    assert notebook.task_list.itemWidget(item) is None
    assert "&lt;b&gt;" in item.toolTip() and "&lt;a href=" in item.toolTip()
    dialog = editor(store, task=task, parent=notebook)
    try:
        dialog.reminder_check.setChecked(True)
        dialog.show()
        QApplication.processEvents()
        rendered = dialog.grab().toImage()
        background = rendered.pixelColor(4, 4)
        assert background.alpha() == 255
        assert min(background.red(), background.green(), background.blue()) >= 235
        # Sample input padding rather than text, borders, or native arrows.
        # A transparent combo/spin would exactly expose the lavender surface.
        for control in (dialog.priority_combo, dialog.repeat_combo, dialog.lead_spin):
            point = control.mapTo(dialog, QPoint(4, control.height() // 2))
            fill = rendered.pixelColor(point)
            assert fill.alpha() == 255
            assert fill.red() >= background.red() + 2
            assert fill.green() >= background.green() + 4
        assert dialog.title_edit.text() == title
        assert dialog.body_edit.toPlainText() == body
    finally:
        dispose(dialog)


@pytest.mark.parametrize("available,requested,expected", [
    (QRect(0, 0, 1280, 680), QRect(20, 20, 440, 700), QRect(20, 20, 440, 640)),
    (QRect(0, 0, 1920, 1040), QRect(20, 20, 440, 700), QRect(20, 20, 440, 700)),
    (QRect(0, 40, 1280, 640), QRect(20, 60, 440, 700), QRect(20, 60, 440, 600)),
    (QRect(0, 0, 400, 600), QRect(20, 20, 440, 700), QRect(10, 10, 380, 580)),
    (QRect(-1920, 0, 1920, 1080), QRect(-1900, 40, 440, 700), QRect(-1900, 40, 440, 700)),
])
def test_geometry_uses_logical_available_rect_and_keeps_minimum_without_crossing_taskbar(available, requested, expected):
    fitted = _fit_notebook_geometry(requested, [available])
    assert fitted == expected
    assert available.contains(fitted)
    assert fitted.width() >= 380
    assert fitted.height() >= 580


@pytest.mark.parametrize("saved", [None, [1200, 630, 1000, 1400], [4200, -1800, 440, 700]])
def test_initial_and_restored_window_fully_fit_680_high_work_area_and_use_compact_layout(store, monkeypatch, saved):
    from qingjian import notebook_ui
    available = QRect(0, 0, 1280, 680)
    monkeypatch.setattr(notebook_ui, "_available_work_areas", lambda: [QRect(available)])
    if saved:
        store.update_settings({"geometry": saved})
    store.update_settings({"layout_mode": "combined"})
    window = NotebookWindow(store, clock=lambda: NOW)
    try:
        window.show()
        QApplication.processEvents()
        assert available.adjusted(20, 20, -20, -20).contains(window.frameGeometry())
        assert window.height() == 640
        assert window.calendar.height() == 174
        assert not window.date_lunar.isVisible()
        assert window.minimumWidth() == 340
        assert window.minimumHeight() == 580
        assert window.calendar.mapTo(window, QPoint(0, window.calendar.height())).y() < window.list_heading.mapTo(window, QPoint()).y()
        assert window.task_list.mapTo(window, QPoint(0, window.task_list.height())).y() < window.height() - 15
    finally:
        window.allow_close = True
        window.close()
        window.deleteLater()


def test_nearly_offscreen_restore_is_clamped_to_best_matching_secondary_monitor(store, monkeypatch):
    from qingjian import notebook_ui
    primary = QRect(0, 0, 1280, 680)
    secondary = QRect(1280, -100, 1600, 900)
    monkeypatch.setattr(notebook_ui, "_available_work_areas", lambda: [QRect(primary), QRect(secondary)])
    store.update_settings({"layout_mode": "combined", "geometry": [2855, 790, 1000, 1400]})
    window = NotebookWindow(store, clock=lambda: NOW)
    try:
        window.show()
        QApplication.processEvents()
        assert secondary.adjusted(20, 20, -20, -20).contains(window.frameGeometry())
        assert not primary.contains(window.frameGeometry())
        assert window.size().width() == 1000
        assert window.size().height() == 860
    finally:
        window.allow_close = True
        window.close()
        window.deleteLater()


def test_default_is_two_small_independent_modern_floaters(notebook):
    assert notebook.layout_mode == "split"
    assert notebook.size().width() == 340 and notebook.size().height() == 370
    peer = notebook.tasks_window
    assert peer.size().width() == 360 and peer.size().height() == 350
    assert notebook.owned_windows() == [notebook, peer]
    assert notebook.companion_windows == [peer]
    assert peer.parent() is None and peer.isWindow() and peer.isVisible()
    assert peer.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert peer.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert peer.isAncestorOf(notebook.task_list)
    assert not notebook.isAncestorOf(notebook.task_list)
    assert notebook.mode_button.text() == peer.controller.tasks_mode_button.text() == "合并"


def test_merge_split_reuses_controls_filters_and_each_saved_position(notebook, store):
    task = add(store, "保存布局与筛选")
    notebook.refresh()
    calendar, listing = notebook.calendar, notebook.task_list
    notebook.search_edit.setText("布局")
    notebook.move(45, 52)
    notebook.tasks_window.move(410, 62)
    before_calendar, before_tasks = notebook.geometry(), notebook.tasks_window.geometry()
    spy = QSignalSpy(notebook.layout_changed)
    notebook.mode_button.click()
    QApplication.processEvents()
    assert notebook.layout_mode == store.settings["layout_mode"] == "combined"
    assert notebook.owned_windows() == [notebook]
    assert not notebook.tasks_window.isVisible()
    assert notebook.isAncestorOf(listing)
    assert notebook.calendar is calendar and notebook.task_list is listing
    assert notebook.search_edit.text() == "布局" and ids(notebook) == [task["id"]]
    notebook.move(90, 85)
    notebook.mode_button.click()
    QApplication.processEvents()
    assert notebook.layout_mode == store.settings["layout_mode"] == "split"
    assert notebook.geometry() == before_calendar
    assert notebook.tasks_window.geometry() == before_tasks
    assert notebook.tasks_window.isVisible()
    assert spy.count() == 2
    assert store.settings["geometry"][:2] == [90, 85]
    assert store.settings["calendar_geometry"][:2] == [45, 52]
    assert store.settings["tasks_geometry"][:2] == [410, 62]


def test_selected_future_date_synchronizes_tasks_without_moving_either_floater(notebook, store):
    tomorrow = add(store, "明天交材料", day="2026-10-30")
    notebook.refresh()
    positions = [window.pos() for window in notebook.owned_windows()]
    notebook.select_date(date(2026, 10, 30))
    assert ids(notebook) == [tomorrow["id"]]
    assert "10月30日" in notebook.list_heading.text()
    assert "2026年10月30日" in notebook.tasks_window.date_caption.text()
    assert [window.pos() for window in notebook.owned_windows()] == positions


def test_group_show_hide_minimize_and_force_close_deletes_ownerless_peer(notebook):
    from shiboken6 import isValid
    peer = notebook.tasks_window
    notebook.hide_windows()
    assert all(not window.isVisible() for window in notebook.owned_windows())
    notebook.show_windows()
    assert all(window.isVisible() and not window.isMinimized() for window in notebook.owned_windows())
    notebook.minimize_windows()
    assert all(window.isMinimized() for window in notebook.owned_windows())
    notebook.show_windows()
    notebook.close_windows()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(peer)
    assert not notebook.isVisible()


@pytest.mark.parametrize("size", [(340, 370, 360, 350), (300, 340, 320, 280)])
def test_split_controls_fit_default_and_minimum_sizes(notebook, size):
    width, height, peer_width, peer_height = size
    notebook.resize(width, height)
    notebook.tasks_window.resize(peer_width, peer_height)
    QApplication.processEvents()
    assert notebook.minimumSizeHint().height() <= height
    assert notebook.tasks_window.minimumSizeHint().height() <= peer_height
    assert notebook.calendar.mapTo(notebook, QPoint(0, notebook.calendar.height())).y() < height - 10
    assert notebook.task_list.height() >= 75
    assert notebook.task_list.mapTo(notebook.tasks_window, QPoint(0, notebook.task_list.height())).y() < peer_height - 15
    assert not notebook.grab().isNull() and not notebook.tasks_window.grab().isNull()


def test_restored_split_geometry_is_fully_visible_on_high_dpi_work_area(store, monkeypatch):
    from qingjian import notebook_ui
    available = QRect(0, 0, 1280, 680)
    monkeypatch.setattr(notebook_ui, "_available_work_areas", lambda: [QRect(available)])
    store.update_settings({"layout_mode": "split", "calendar_geometry": [4000, -200, 500, 800],
                           "tasks_geometry": [-3000, 2000, 600, 1000]})
    window = NotebookWindow(store, clock=lambda: NOW)
    try:
        window.show_windows()
        QApplication.processEvents()
        assert all(available.adjusted(20, 20, -20, -20).contains(peer.frameGeometry()) for peer in window.owned_windows())
    finally:
        window.allow_close = True
        window.close()
        window.deleteLater()


def test_delegate_edit_completion_and_delete_support_mouse_and_keyboard(notebook, store, monkeypatch):
    task = add(store)
    notebook.refresh()
    called = []
    monkeypatch.setattr(notebook, "edit_task", lambda key=None: called.append(key))
    QApplication.processEvents()
    QTest.mouseClick(notebook.task_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=notebook.task_action_rect(0, "edit").center())
    assert called == [task["id"]]
    notebook.task_list.setCurrentRow(0)
    QTest.keyClick(notebook.task_list, Qt.Key.Key_Return)
    assert called == [task["id"], task["id"]]
    QTest.keyClick(notebook.task_list, Qt.Key.Key_Space)
    assert store.find_task(task["id"])["completed"]
    notebook.completed_check.setChecked(True)
    notebook.task_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    QTest.keyClick(notebook.task_list, Qt.Key.Key_Delete)
    assert store.find_task(task["id"]) is None


def test_repeated_refresh_and_paint_reuse_revision_snapshot_items_and_calendar_marks(notebook, store, monkeypatch):
    task = add(store, remind_at="2026-10-29T10:00:00", repeat={"kind": "daily"})
    notebook.refresh()
    original = notebook.task_list.item(0)
    stats = dict(notebook.render_stats)
    page_builds = notebook.calendar.page_builds
    from qingjian import notebook_ui
    monkeypatch.setattr(notebook, "_visible_tasks", lambda: pytest.fail("unchanged filter rescanned the task list"))
    monkeypatch.setattr(notebook_ui, "_occurs_on", lambda *_args: pytest.fail("painting recalculated recurrence"))
    monkeypatch.setattr(notebook_ui, "_calendar_info", lambda *_args: pytest.fail("painting recalculated lunar date"))
    for _ in range(5):
        notebook.refresh()
        notebook.calendar.grab()
        notebook.tasks_window.grab()
    assert notebook.render_stats == stats
    assert notebook.calendar.page_builds == page_builds
    assert notebook.task_list.item(0) is original
    assert ids(notebook) == [task["id"]]


def test_large_task_list_has_accessible_titles_and_no_per_row_widget_tree():
    import copy
    class SnapshotStore(NotebookStore):
        @property
        def tasks(self):
            return copy.deepcopy(self.records)
    store = SnapshotStore()
    store.records = [dict(id=f"task-{index}", title=f"事项 {index}", body="材料清单", priority="normal",
                          date="2026-10-29", remind_at=None, repeat={"kind": "once"}, completed=False,
                          next_due=None, created=NOW.isoformat(), updated=NOW.isoformat()) for index in range(2000)]
    window = NotebookWindow(store, clock=lambda: NOW)
    try:
        window.show_windows()
        QApplication.processEvents()
        assert window.task_list.count() == 2000
        assert window.task_list.item(0).text().startswith("事项 ")
        assert len(window.task_list.findChildren(QWidget)) < 20
        assert all(window.task_list.itemWidget(window.task_list.item(index)) is None for index in (0, 999, 1999))
        before = dict(window.render_stats)
        window.refresh()
        assert window.render_stats == before
    finally:
        window.allow_close = True
        window.close()
        window.deleteLater()


def test_failed_layout_save_keeps_current_controls_and_split_mode(notebook, monkeypatch):
    listing = notebook.task_list
    monkeypatch.setattr(notebook.store, "update_settings", lambda *_args: (_ for _ in ()).throw(OSError("数据目录不可写")))
    assert notebook.set_layout_mode("combined") is False
    assert notebook.layout_mode == "split"
    assert notebook.tasks_window.isAncestorOf(listing)
    assert "不可写" in notebook.error_label.text()


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("height", [20, 24, 28])
def test_compact_calendar_cells_keep_two_text_lines_clear_of_priority_dots_at_scaled_dpi(notebook, scale, height):
    rect = QRect(0, 0, 42, height)
    for label in ("廿一", "万圣节前"):
        for selected in (False, True):
            rendered = QImage(round(rect.width() * scale), round(rect.height() * scale), QImage.Format.Format_ARGB32_Premultiplied)
            rendered.setDevicePixelRatio(scale)
            rendered.fill(Qt.GlobalColor.transparent)
            painter = QPainter(rendered)
            try:
                content = notebook.calendar._cell_content_layout(painter, rect, "30", label, selected)
                assert QRectF(rect).contains(content["number_ink"])
                assert QRectF(rect).contains(content["label_ink"])
                assert QRectF(rect).contains(content["dot_row"])
                assert content["number_ink"].bottom() + .5 <= content["label_ink"].top()
                assert content["label_ink"].bottom() + .5 <= content["dot_row"].top()
                day = date(2026, 10, 30)
                notebook.calendar._info_cache[day] = {"label": label}
                notebook.calendar._marks_cache[day] = ("urgent", "important", "normal")
                notebook.calendar.setSelectedDate(QDate(2026, 10, 30 if selected else 29))
                notebook.calendar.paintCell(painter, rect, QDate(day))
            finally:
                painter.end()
            assert not rendered.isNull()
            assert rendered.pixelColor(round(rect.center().x() * scale), round(content["dot_y"] * scale)).alpha() > 0
