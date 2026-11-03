"""Own one Qt application for the test session, including native shutdown."""
import gc
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from qingjian.theme import STYLE


@pytest.fixture(autouse=True)
def isolate_shared_runtime_memory(tmp_path, monkeypatch):
    # Bootstrap checks must never read/write a developer's real location memory.
    monkeypatch.setenv('QINGJIAN_SHARED_RUNTIME_INDEX', str(tmp_path / 'isolated-user' / 'shared-runtime.json'))


@pytest.fixture(scope="session", autouse=True)
def desktop_application():
    # A session owns the app rather than leaving class-level Qt references
    # for Python's arbitrary module destruction order at interpreter exit.
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if not QFontDatabase.families() and font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    yield app
    gc.collect()
    for window in app.topLevelWidgets():
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    app.shutdown()
