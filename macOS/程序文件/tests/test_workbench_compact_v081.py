"""The personal area and every group remain usable in narrow windows."""
from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qingjian.workbench import HomePage, TOOL_GROUPS, TOOL_ITEMS, TaskCard, ToolCatalogPage


class CompactWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.pump()

    def pump(self):
        for _ in range(4):
            self.app.processEvents()
            QTest.qWait(1)

    def show(self, widget, width=410, height=480):
        self.widgets.append(widget)
        widget.resize(width, height)
        widget.show()
        self.pump()
        return widget

    def assert_in_viewport(self, page, widget):
        viewport = page.scroll.viewport()
        bounds = QRect(widget.mapTo(viewport, QPoint()), widget.size())
        self.assertTrue(widget.isVisible(), widget.accessibleName())
        self.assertTrue(viewport.rect().contains(bounds), (widget.accessibleName(), bounds, viewport.rect()))

    def test_standalone_home_has_exactly_two_content_sections_and_all_groups(self):
        page = self.show(HomePage(), 1080, 1000)
        sections = [page.content.itemAt(index).widget() for index in range(page.content.count())
                    if page.content.itemAt(index).widget() is not None]
        self.assertEqual(sections, [page.group_section, page.personal_panel])
        self.assertEqual(set(page.group_cards), {group["id"] for group in TOOL_GROUPS})
        self.assertEqual(len(page.group_grid.items), 5)
        self.assertTrue(page.personal_panel.isVisible())
        self.assertFalse(hasattr(page, "quick_panel"))
        for card in page.group_cards.values():
            self.assertTrue(card.isVisible())
            self.assertGreaterEqual(card.height(), 70)

    def test_small_home_scroll_reaches_all_five_groups_without_horizontal_overflow(self):
        page = HomePage()
        page.set_compact(True)
        # This is the client width of a 1024-logical-pixel desktop's one-third
        # outer frame once ordinary Windows side decorations are reserved.
        self.show(page, 325, 480)
        self.assertEqual(page.group_grid.columns, 1)
        self.assertEqual(page.group_grid.items, list(page.group_cards.values()))
        for card in page.group_cards.values():
            page.scroll.ensureWidgetVisible(card)
            self.pump()
            self.assert_in_viewport(page, card)
            self.assertTrue(card.isEnabled())
            self.assertLessEqual(card.height(), 70)
        page.scroll.ensureWidgetVisible(page.catalog_button)
        self.pump()
        self.assert_in_viewport(page, page.catalog_button)
        self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(page.canvas.width(), page.scroll.viewport().width())

    def test_compact_home_routes_every_group_and_catalog_buttons(self):
        page = HomePage()
        page.set_compact(True)
        self.show(page)
        groups, catalogs = [], []
        page.group_requested.connect(groups.append)
        page.catalog_requested.connect(lambda: catalogs.append(True))
        for key, card in page.group_cards.items():
            page.scroll.ensureWidgetVisible(card)
            self.pump()
            QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        QTest.mouseClick(page.catalog_button, Qt.MouseButton.LeftButton)
        self.assertEqual(groups, [group["id"] for group in TOOL_GROUPS])
        self.assertEqual(catalogs, [True])

    def test_home_resizes_without_recreating_personal_area_or_group_cards(self):
        page = self.show(HomePage(), 1080, 1100)
        original_cards = dict(page.group_cards)
        original_personal = page.personal_panel
        for _ in range(2):
            page.set_compact(True)
            self.pump()
            page.set_compact(False)
            self.pump()
        self.assertEqual(page.group_cards, original_cards)
        self.assertIs(page.personal_panel, original_personal)
        self.assertEqual(page.group_grid.items, list(original_cards.values()))
        self.assertEqual(page.group_grid.columns, 2)
        for widget in (page.personal_panel, page.group_section, page.catalog_button):
            self.assertTrue(widget.isVisible())
        for card in page.group_cards.values():
            self.assertTrue(card.isVisible())
            self.assertTrue(card.description_label.isVisible())
            self.assertGreaterEqual(card.height(), 70)

    def test_task_card_keeps_accessible_action_and_full_description_when_compact(self):
        card = self.show(TaskCard("材料完整性核查", "完整的任务说明", tag="核查"), 180, 170)
        clicked = []
        card.clicked.connect(lambda: clicked.append(True))
        card.set_compact(True)
        self.pump()
        self.assertEqual(card.accessibleName(), "材料完整性核查")
        self.assertEqual(card.toolTip(), "完整的任务说明")
        self.assertFalse(card.description_label.isVisible())
        self.assertTrue(card.title_label.isVisible())
        self.assertLessEqual(card.height(), 48)
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(clicked, [True])
        card.set_compact(False)
        self.pump()
        self.assertTrue(card.description_label.isVisible())
        self.assertGreaterEqual(card.height(), 145)

    def test_compact_catalog_keeps_all_tools_and_native_search(self):
        page = ToolCatalogPage()
        page.set_compact(True)
        self.show(page)
        self.assertEqual(len(page.tool_cards), 27)
        self.assertEqual(set(page.visible_modes), set(TOOL_ITEMS))
        self.assertEqual(page.tool_grid.columns, 2)
        self.assertEqual(page.filters.columns, 3)
        self.assertTrue(page.subtitle_label.isHidden())
        self.assert_in_viewport(page, page.search_edit)
        self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
        for mode, item in TOOL_ITEMS.items():
            with self.subTest(mode=mode):
                page.search_edit.setText(mode)
                self.pump()
                self.assertIn(mode, page.visible_modes)
                self.assertFalse(page.tool_cards[mode].isHidden())

    def test_catalog_presentation_switch_preserves_filter_search_and_action(self):
        page = self.show(ToolCatalogPage(), 1080, 900)
        page.set_group("doc")
        page.search_edit.setText("Word")
        modes = list(page.visible_modes)
        cards = dict(page.tool_cards)
        page.set_compact(True)
        self.pump()
        self.assertEqual(page.current_group, "doc")
        self.assertEqual(page.search_edit.text(), "Word")
        self.assertEqual(page.visible_modes, modes)
        self.assertEqual(page.tool_cards, cards)
        selected = []
        page.tool_requested.connect(selected.append)
        page.tool_cards["word"].click()
        self.assertEqual(selected, ["word"])
        page.set_compact(False)
        self.pump()
        self.assertEqual(page.visible_modes, modes)
        self.assertFalse(page.subtitle_label.isHidden())
        self.assertEqual(page.filters.maximum_columns, 6)
        for card in page.tool_cards.values():
            self.assertGreaterEqual(card.minimumHeight(), 145)


if __name__ == "__main__":
    unittest.main()
