"""Two-zone home routes and reminder refresh across notebook store reloads."""
from datetime import datetime

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel

from qingjian.app import Window
from qingjian.notebook_runtime import NotebookController
from qingjian.notebook_store import NotebookStore
from qingjian.workbench import TOOL_ITEMS


def pump():
    for _ in range(4):
        QApplication.processEvents()


def visible_reminder_text(panel):
    values = []
    for index in range(panel.reminder_rows.count()):
        widget = panel.reminder_rows.itemAt(index).widget()
        if isinstance(widget, QLabel):
            values.append(widget.text())
        values.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(values)


def test_home_all_tools_resets_previous_category_and_search(tmp_path):
    window = Window(start_home=True, data_path=tmp_path / "profile.json")
    try:
        window.show()
        assert window.home_page.content.itemAt(0).widget() is window.home_page.group_section
        assert window.home_page.content.itemAt(1).widget() is window.home_page.personal_panel
        window.open_tool_group("table")
        window.catalog_page.search_edit.setText("核查")
        assert len(window.catalog_page.visible_modes) < len(TOOL_ITEMS)
        window.show_surface("home")
        window.home_page.catalog_button.click()
        pump()
        assert window.content_stack.currentWidget() is window.catalog_page
        assert window.catalog_page.current_group == "all"
        assert window.catalog_page.search_edit.text() == ""
        assert set(window.catalog_page.visible_modes) == set(TOOL_ITEMS)
        assert window.worker is None
    finally:
        window.close()
        window.deleteLater()
        pump()


def test_notebook_store_reload_rebinds_home_reminders_without_stale_items(tmp_path):
    path = tmp_path / "notebook.json"
    stale = NotebookStore(path)
    window = Window(start_home=True, data_path=tmp_path / "profile.json")
    controller = NotebookController(stale, test_mode=True, enable_tray=False)
    panel = window.home_page.personal_panel
    panel.set_notebook_store(stale)
    refreshed = []
    controller.store_changed.connect(refreshed.append)
    controller.store_changed.connect(panel.set_notebook_store)
    now = datetime.now().replace(microsecond=0)
    try:
        window.show()
        pump()
        assert "暂无" in visible_reminder_text(panel)
        external = NotebookStore(path)
        task = external.upsert_task({"title": "从记事小窗新增的事项", "priority": "important"}, now=now)
        assert not stale.tasks

        controller._refresh_disk_store()
        pump()
        assert len(refreshed) == 1 and refreshed[0] is controller.store
        assert controller.store is not stale
        assert panel._notebook_store is controller.store
        assert "从记事小窗新增的事项" in visible_reminder_text(panel)
        assert "暂无" not in visible_reminder_text(panel)

        external.complete_task(task["id"], now=now)
        controller._refresh_disk_store()
        pump()
        assert len(refreshed) == 2 and refreshed[-1] is controller.store
        assert panel._notebook_store is controller.store
        assert "从记事小窗新增的事项" not in visible_reminder_text(panel)
        assert "暂无" in visible_reminder_text(panel)
        assert NotebookStore(path).find_task(task["id"])["completed"]
    finally:
        controller.shutdown()
        controller.deleteLater()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        pump()
