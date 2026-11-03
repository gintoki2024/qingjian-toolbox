"""Geometry contracts for a compact window, independent of UI arrangements."""
from __future__ import annotations

import unittest

from PySide6.QtCore import QMargins, QRect

from qingjian.window_layout import compact_window_geometry, compact_window_minimum_size


class CompactWindowGeometryTests(unittest.TestCase):
    def outer_rect(self, client, margins):
        return client.adjusted(-margins.left(), -margins.top(),
                               margins.right(), margins.bottom())

    def assert_screen_contract(self, screen, margins):
        original_screen = QRect(screen)
        original_margins = QMargins(margins)
        client = compact_window_geometry(screen, margins)
        outer = self.outer_rect(client, margins)
        self.assertTrue(client.isValid())
        self.assertTrue(screen.contains(outer), (screen, client, outer))
        self.assertLessEqual(outer.width(), screen.width() / 3)
        self.assertLessEqual(outer.width(), 540)
        self.assertLessEqual(outer.height(), screen.height() * .9)
        self.assertLessEqual(outer.height(), 780)
        self.assertLessEqual(outer.width() * outer.height(),
                             screen.width() * screen.height() / 3)
        # Keep the application to one side, leaving useful desktop drop space.
        self.assertGreaterEqual(outer.center().x(), screen.center().x())
        minimum = compact_window_minimum_size(screen, margins)
        self.assertGreater(minimum.width(), 0)
        self.assertGreater(minimum.height(), 0)
        self.assertLessEqual(minimum.width(), client.width())
        self.assertLessEqual(minimum.height(), client.height())
        self.assertLessEqual(minimum.width(), 360)
        self.assertLessEqual(minimum.height(), 480)
        self.assertEqual(screen, original_screen)
        self.assertEqual(margins, original_margins)
        return client, outer

    def test_common_available_desktops_fit_with_native_decorations(self):
        for width, height in ((1280, 680), (1366, 728), (1920, 1040),
                              (2560, 1400), (3840, 2080), (1024, 720),
                              (960, 500), (800, 450), (720, 500)):
            for margins in (QMargins(), QMargins(8, 31, 8, 8),
                            QMargins(10, 40, 10, 10)):
                with self.subTest(width=width, height=height, margins=margins):
                    self.assert_screen_contract(QRect(0, 0, width, height), margins)

    def test_left_and_above_primary_monitors_preserve_coordinates(self):
        for screen in (QRect(-1920, 0, 1920, 1040),
                       QRect(-1366, -728, 1366, 728),
                       QRect(1920, -200, 1280, 680),
                       QRect(40, 50, 1024, 720)):
            with self.subTest(screen=screen):
                self.assert_screen_contract(screen, QMargins(8, 31, 8, 8))

    def test_translation_of_a_monitor_translates_the_same_window(self):
        first = compact_window_geometry(QRect(0, 0, 1920, 1040))
        shifted = compact_window_geometry(QRect(-1920, -1040, 1920, 1040))
        self.assertEqual(first.size(), shifted.size())
        self.assertEqual(shifted, first.translated(-1920, -1040))

    def test_high_dpi_uses_the_supplied_logical_geometry_without_inflation(self):
        # A 3840 x 2160 monitor at 200% exposes roughly this logical work area.
        client, outer = self.assert_screen_contract(
            QRect(0, 0, 1920, 1040), QMargins(8, 31, 8, 8))
        self.assertLessEqual(outer.width(), 540)
        self.assertGreater(client.width(), 360)
        self.assertGreater(client.height(), 480)

    def test_default_frame_fallback_reserves_title_bar_and_borders(self):
        screen = QRect(0, 0, 1280, 680)
        default = compact_window_geometry(screen)
        explicit = compact_window_geometry(screen, QMargins(8, 31, 8, 8))
        undecorated = compact_window_geometry(screen, QMargins())
        self.assertEqual(default, explicit)
        self.assertGreater(undecorated.width(), default.width())
        self.assertGreater(undecorated.height(), default.height())
        self.assertEqual(self.outer_rect(default, QMargins(8, 31, 8, 8)), undecorated)

    def test_small_monitor_minimum_does_not_force_the_window_past_its_budget(self):
        screen = QRect(0, 0, 1024, 600)
        margins = QMargins(8, 31, 8, 8)
        minimum = compact_window_minimum_size(screen, margins)
        self.assertGreaterEqual(minimum.width(), 320)
        self.assertLess(minimum.width(), 360)
        self.assertLessEqual(minimum.width() + margins.left() + margins.right(),
                             screen.width() / 3)

    def test_invalid_screen_uses_a_positive_bounded_fallback(self):
        for screen in (QRect(), QRect(5, 7, 0, 700), QRect(-5, -7, 1200, -2)):
            with self.subTest(screen=screen):
                client = compact_window_geometry(screen)
                outer = self.outer_rect(client, QMargins(8, 31, 8, 8))
                self.assertTrue(client.isValid())
                self.assertTrue(QRect(0, 0, 1280, 720).contains(outer))
                self.assertLessEqual(outer.width() * outer.height(), 1280 * 720 / 3)

    def test_transient_negative_margins_do_not_expand_the_client(self):
        screen = QRect(0, 0, 1366, 728)
        self.assertEqual(compact_window_geometry(screen, QMargins(-8, -31, -8, -8)),
                         compact_window_geometry(screen, QMargins()))

    def test_impossible_frame_and_tiny_screen_keep_positive_client_size(self):
        client = compact_window_geometry(QRect(0, 0, 20, 20), QMargins(50, 50, 50, 50))
        self.assertTrue(client.isValid())
        self.assertEqual(client.width(), 1)
        self.assertEqual(client.height(), 1)
        minimum = compact_window_minimum_size(QRect(0, 0, 20, 20), QMargins(50, 50, 50, 50))
        self.assertEqual(minimum, client.size())


if __name__ == "__main__":
    unittest.main()
