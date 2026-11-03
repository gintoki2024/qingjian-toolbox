"""Home files are bounded and read-only; notebook previews cannot consume alerts."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from qingjian.home_content import (
    ContentRotation, DEFAULT_SETTINGS, HomeContentStore, MAX_TEXT_BYTES,
    read_quotes, scan_content, upcoming_reminders,
)
from qingjian.notebook_store import NotebookStore


class HomeContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("QINGJIAN_TEST_TMPDIR"))
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_none_store_is_in_memory_and_settings_are_copied(self):
        with patch("qingjian.home_content.os.replace") as replace:
            store = HomeContentStore(None)
            store.update({"image_mode": "single"})
            settings = store.settings
            settings["folder"] = "changed"
            self.assertEqual(store.settings["folder"], "")
            replace.assert_not_called()

    def test_preferences_survive_restart_without_copying_materials(self):
        path = self.root / "config" / "home_content.json"
        store = HomeContentStore(path)
        store.update({"folder": str(self.root / "素材"), "fixed_quote": "句子.txt#2", "quote_mode": "single"})
        self.assertEqual(HomeContentStore(path).settings, store.settings)
        self.assertEqual(set(json.loads(path.read_text("utf-8"))), {"schema", "settings"})
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_write_keeps_previous_state(self):
        path = self.root / "home_content.json"
        store = HomeContentStore(path)
        store.update({"image_interval": 10})
        original = path.read_bytes()
        with patch("qingjian.home_content.os.replace", side_effect=OSError("readonly")):
            with self.assertRaises(OSError):
                store.update({"image_interval": 30})
        self.assertEqual(store.settings["image_interval"], 10)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_invalid_existing_settings_are_not_overwritten(self):
        path = self.root / "home_content.json"
        path.write_bytes(b"broken")
        store = HomeContentStore(path)
        self.assertTrue(store.last_error)
        self.assertEqual(store.settings, DEFAULT_SETTINGS)
        with self.assertRaises(ValueError):
            store.update({"folder": "somewhere"})
        self.assertEqual(path.read_bytes(), b"broken")

    def test_invalid_preferences_rejected(self):
        for values in ({"show_images": 1}, {"image_interval": True}, {"image_interval": 2},
                       {"quote_mode": "unknown"}, {"display_mode": "all"}, {"reminder_days": 0}, {"folder": "bad\x00name"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                HomeContentStore().update(values)

    def test_legacy_settings_migrate_without_losing_folder_or_playback(self):
        path = self.root / "home-settings.json"
        legacy = {key: value for key, value in DEFAULT_SETTINGS.items() if key != "display_mode"}
        legacy.update(folder="D:/首页素材", quote_mode="single", fixed_quote="句子.txt#3")
        path.write_text(json.dumps({"schema": 1, "settings": legacy}), "utf-8")
        store = HomeContentStore(path)
        self.assertEqual(store.settings["display_mode"], "images")
        for key, value in legacy.items():
            self.assertEqual(store.settings[key], value)
        store.update({"display_mode": "quotes"})
        self.assertEqual(HomeContentStore(path).settings["display_mode"], "quotes")

    def test_text_encodings_and_blank_paragraph_rules(self):
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "gb18030"):
            with self.subTest(encoding=encoding):
                path = self.root / (encoding + ".txt")
                text = "今天做一点。\nTomorrow too.\n\n然后休息。"
                path.write_bytes(text.encode(encoding))
                quotes = read_quotes(path)
                self.assertEqual([item.text for item in quotes], ["今天做一点。\nTomorrow too.", "然后休息。"])

    def test_no_blank_lines_means_one_quote_per_line(self):
        path = self.root / "每天.txt"
        path.write_text("第一句\r\n第二句\r\n第三句", "utf-8")
        self.assertEqual([item.text for item in read_quotes(path)], ["第一句", "第二句", "第三句"])

    def test_text_size_binary_and_long_paragraph_rejected(self):
        path = self.root / "测试.txt"
        for raw in (b"a" * (MAX_TEXT_BYTES + 1), b"\x00\x00\x02\x03", ("字" * 8001).encode("utf-8")):
            path.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                read_quotes(path)

    def test_scan_only_top_level_and_never_writes_materials(self):
        (self.root / "B.JPG").write_bytes(b"bad image still identified")
        (self.root / "a.png").write_bytes(b"png")
        (self.root / "其他.docx").write_bytes(b"not used")
        (self.root / "想说的话.TXT").write_text("一\n二", "utf-8")
        (self.root / "空.txt").write_text(" \n", "utf-8")
        sub = self.root / "子目录"
        sub.mkdir()
        (sub / "隐藏.txt").write_text("不递归读取", "utf-8")
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        snapshot = scan_content(str(self.root))
        self.assertEqual([path.name for path in snapshot.images], ["a.png", "B.JPG"])
        self.assertEqual([item.text for item in snapshot.quotes], ["一", "二"])
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_missing_folder_empty_folder_and_bad_txt_are_understandable(self):
        self.assertFalse(scan_content("").warnings)
        self.assertFalse(scan_content(str(self.root)).warnings)
        self.assertIn("无法访问", scan_content(str(self.root / "missing")).warnings[0])
        (self.root / "太长.txt").write_bytes(b"a" * (MAX_TEXT_BYTES + 1))
        (self.root / "好.txt").write_text("继续展示", "utf-8")
        snapshot = scan_content(str(self.root))
        self.assertEqual(snapshot.quotes[0].text, "继续展示")
        self.assertIn("512 KB", snapshot.warnings[0])

    def test_sequential_random_and_single_have_distinct_behavior(self):
        rotation = ContentRotation(random.Random(22))
        rotation.reset(["a", "b", "c"])
        self.assertEqual([rotation.current, rotation.step(), rotation.step(), rotation.step()], ["a", "b", "c", "a"])
        rotation.reset(["a", "b", "c"], "single", "b")
        self.assertEqual(rotation.step(automatic=True), "b")
        self.assertEqual(rotation.step(), "c")
        rotation.reset(["a", "b", "c"], "random")
        self.assertEqual(len({rotation.current, rotation.step(), rotation.step()}), 3)
        previous = rotation.current
        for _ in range(20):
            current = rotation.step()
            self.assertNotEqual(previous, current)
            previous = current

    def test_cursor_survives_refresh_and_handles_deleted_fixed_item(self):
        rotation = ContentRotation()
        rotation.reset(["a", "b"], keep="b")
        self.assertEqual(rotation.current, "b")
        rotation.reset(["a"], "single", "gone")
        self.assertEqual(rotation.current, "a")
        rotation.reset([])
        self.assertIsNone(rotation.step())

    def test_reminder_preview_is_read_only_and_includes_overdue(self):
        now = datetime(2026, 11, 2, 12)
        store = NotebookStore(self.root / "notebook.json")
        store.upsert_task({"title": "逾期一次", "date": "2026-11-01", "remind_at": "2026-11-01T09:00:00"}, now=now)
        store.upsert_task({"title": "今天全天", "date": "2026-11-02"}, now=now)
        store.upsert_task({"title": "一周之外", "date": "2026-11-09"}, now=now)
        store.upsert_task({"title": "已经完成", "date": "2026-11-02", "completed": True}, now=now)
        before, revision = store.path.read_bytes(), store.revision
        with patch.object(store, "collect_due", side_effect=AssertionError("preview consumed reminder")):
            rows = upcoming_reminders(store, now=now)
        self.assertEqual([row["title"] for row in rows], ["逾期一次", "今天全天"])
        self.assertTrue(rows[0]["overdue"])
        self.assertFalse(rows[1]["overdue"])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(store.revision, revision)
        self.assertFalse(store.pending_alerts)

    def test_pending_recurrence_is_shown_without_advancing_schedule(self):
        now = datetime(2026, 11, 2, 12)
        store = NotebookStore()
        store.upsert_task({"title": "每日复盘", "date": "2026-11-01", "remind_at": "2026-11-01T09:00:00", "repeat": {"kind": "daily", "interval": 1}}, now=now)
        alerts = store.collect_due(now)
        due_before, revision = store.tasks[0]["next_due"], store.revision
        rows = upcoming_reminders(store, now=now)
        self.assertTrue(rows[0]["overdue"])
        self.assertTrue(rows[0]["recurring"])
        self.assertEqual(store.tasks[0]["next_due"], due_before)
        self.assertEqual(store.revision, revision)
        self.assertEqual(store.pending_alerts, alerts)
        store.acknowledge(alerts[0]["id"], now)
        rows = upcoming_reminders(store, now=now)
        self.assertEqual(rows[0]["due"], datetime(2026, 11, 3, 9))

    def test_weekday_recurrence_uses_existing_schedule(self):
        now = datetime(2026, 11, 7, 8)  # Saturday
        store = NotebookStore()
        store.upsert_task({"title": "工作日", "date": "2026-11-07", "remind_at": "2026-11-07T09:00:00", "repeat": {"kind": "weekdays", "interval": 1}}, now=now)
        self.assertEqual(upcoming_reminders(store, now=now)[0]["due"], datetime(2026, 11, 9, 9))


if __name__ == "__main__":
    unittest.main()
