"""Changing target pages while a drag stays held must not submit early."""
from pathlib import Path
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QWheelEvent
from PySide6.QtWidgets import QApplication
import pytest

from qingjian.converter_ui import FormatRing
from qingjian.converter_hub import available_targets


@pytest.fixture
def ring():
    widget = FormatRing()
    widget.set_targets([dict(id=f'test:{i}', title=f'格式{i}', glyph=str(i)) for i in range(19)])
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def wheel(widget, angle=0, pixel=0, horizontal=0):
    point = QPointF(122, 35)
    event = QWheelEvent(point, point, QPoint(0, pixel), QPoint(horizontal, angle),
                        Qt.LeftButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


def enter(widget):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(Path.cwd() / '测试目录' / '输入.txt'))])
    event = QDragEnterEvent(QPoint(122, 35), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.ShiftModifier)
    QApplication.sendEvent(widget, event)
    assert event.isAccepted() and event.dropAction() == Qt.CopyAction
    return mime


def test_held_drag_wheel_changes_page_hover_and_actual_drop_target(ring):
    received = []
    ring.sources_dropped.connect(lambda sources, key: received.append((sources, key)))
    mime = enter(ring)
    wheel(ring, -120)
    assert ring.page == 1 and ring._hover == 0
    assert ring.visible_targets[ring._hover]['id'] == 'test:6'
    move = QDragMoveEvent(QPoint(122, 35), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.ShiftModifier)
    QApplication.sendEvent(ring, move)
    assert ring.page == 1 and not received
    wheel(ring, -120)
    assert ring.page == 2
    wheel(ring, 120)
    assert ring.page == 1 and not received
    drop = QDropEvent(QPointF(122, 35), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.ShiftModifier)
    QApplication.sendEvent(ring, drop)
    assert drop.dropAction() == Qt.CopyAction and drop.isAccepted()
    expected_source = str(Path(mime.urls()[0].toLocalFile()).absolute())
    assert received == [([expected_source], 'test:6')]
    assert not ring._drag_active and not ring.drag_wheel.active


def test_high_resolution_and_horizontal_wheel_do_not_flip_spuriously(ring):
    wheel(ring, horizontal=120)
    assert ring.page == 0
    wheel(ring, -30)
    wheel(ring, -30)
    wheel(ring, -30)
    assert ring.page == 0
    wheel(ring, -30)
    assert ring.page == 1
    wheel(ring, pixel=20)
    assert ring.page == 1
    wheel(ring, pixel=20)
    assert ring.page == 0
    wheel(ring, 120)
    assert ring.page == 3  # preserve circular previous/next behavior


@pytest.mark.parametrize('action', ['leave', 'hide', 'disable'])
def test_drag_lifetime_releases_bridge_and_pending_input(ring, action):
    mime = enter(ring)
    ring.drag_wheel._pending.append(-120)
    if action == 'leave':
        QApplication.sendEvent(ring, QDragLeaveEvent())
    elif action == 'hide':
        ring.hide()
    else:
        ring.setEnabled(False)
    ring._flush_drag_wheel()
    assert ring.page == 0
    assert not ring._drag_active and not ring.drag_wheel.active
    assert not ring.drag_wheel.take_pending()


def test_drop_flushes_native_wheel_received_before_button_release(ring):
    received = []
    ring.sources_dropped.connect(lambda sources, key: received.append(key))
    mime = enter(ring)
    ring.drag_wheel._pending.append(-120)
    drop = QDropEvent(QPointF(122, 35), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(ring, drop)
    assert received == ['test:6']


def test_one_page_disabled_and_invalid_drag_do_not_advance(ring):
    ring.set_targets(ring.targets[:6])
    wheel(ring, -120)
    assert ring.page == 0
    ring.setEnabled(False)
    wheel(ring, -120)
    assert ring.page == 0
    ring.setEnabled(True)
    mime = QMimeData()
    mime.setText('不是文件或链接')
    event = QDragEnterEvent(QPoint(122, 35), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(ring, event)
    assert not event.isAccepted() and not ring._drag_active


@pytest.mark.parametrize('count', [0, 1, 5, 6, 7, 12, 13])
def test_pages_fill_to_capacity_across_priority_groups(ring, count):
    targets = [dict(id=f'test:{i}', glyph=str(i), group='common' if i % 3 == 0 else 'more') for i in range(count)]
    ring.set_targets(targets)
    ordered = [item for item in targets if item['group'] == 'common'] + [item for item in targets if item['group'] == 'more']
    assert [item for page in ring.target_pages for item in page] == ordered
    assert len(ring.target_pages) == max(1, (count + ring.PAGE_SIZE - 1) // ring.PAGE_SIZE)
    assert all(len(page) == ring.PAGE_SIZE for page in ring.target_pages[:-1])
    assert len(ring.buttons) == min(count, ring.PAGE_SIZE)


@pytest.mark.parametrize(('suffix', 'count'), [('txt', 5), ('docx', 7), ('pptx', 7), ('pdf', 5)])
def test_actual_office_targets_use_minimum_pages(ring, suffix, count):
    targets = available_targets([str(Path.cwd() / '示例' / ('办公文件.' + suffix))])
    assert len(targets) == count
    assert {target['group'] for target in targets} == {'common', 'more'}
    ring.set_targets(targets)
    assert len(ring.target_pages) == (count + ring.PAGE_SIZE - 1) // ring.PAGE_SIZE
    assert ring.visible_targets == targets[:ring.PAGE_SIZE]
    mime = enter(ring)
    wheel(ring, -120)
    if count <= ring.PAGE_SIZE:
        assert ring.page == 0
        assert not ring.drag_wheel.active
    else:
        assert ring.page == 1
        assert ring.visible_targets == targets[ring.PAGE_SIZE:]
    assert mime.hasUrls()


def test_partly_filled_second_page_still_scrolls_and_drops(ring):
    ring.set_targets([dict(id=f'test:{i}', glyph=str(i), group='common' if i < 4 else 'more') for i in range(7)])
    assert len(ring.visible_targets) == 6 and len(ring.target_pages) == 2
    mime = enter(ring)
    wheel(ring, -120)
    assert [item['id'] for item in ring.visible_targets] == ['test:6']
    drop = QDropEvent(QPointF(122, 35), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    received = []
    ring.sources_dropped.connect(lambda sources, key: received.append(key))
    QApplication.sendEvent(ring, drop)
    assert received == ['test:6']
