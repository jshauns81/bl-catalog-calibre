"""Readable native Qt progress and control indicators on the supported host styles."""

from __future__ import annotations

import os
import sys
from math import ceil, floor
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre.style import TOKENS, build_stylesheet


def _luminance(color):
    channels = [channel / 255 for channel in (color.red(), color.green(), color.blue())]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def _contrast(first, second):
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.fixture(scope="module")
def qt_app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(params=[("Fusion", 12, False), ("Windows", 11, True)])
def controls(qt_app, request):
    style_name, point_size, bold = request.param
    available_styles = QtWidgets.QStyleFactory.keys()
    if style_name not in available_styles:
        pytest.skip(f"Qt style {style_name} is unavailable")
    previous_style = qt_app.style().objectName()
    previous_font = QtGui.QFont(qt_app.font())
    qt_app.setStyle(style_name)
    font = QtGui.QFont("DejaVu Sans", point_size)
    font.setBold(bold)
    qt_app.setFont(font)
    dialog = QtWidgets.QDialog()
    layout = QtWidgets.QVBoxLayout(dialog)
    label = QtWidgets.QLabel("Checking selected books · 50% · 7 / 14 books")
    label.setObjectName("OperationProgress")
    layout.addWidget(label)
    combo = QtWidgets.QComboBox()
    combo.addItems(["Snapshot Mode", "Live HTTP API"])
    layout.addWidget(combo)
    spin = QtWidgets.QSpinBox()
    spin.setRange(1, 5000)
    spin.setValue(50)
    layout.addWidget(spin)
    dialog.setStyleSheet(build_stylesheet())
    dialog.resize(520, 200)
    dialog.show()
    qt_app.processEvents()
    try:
        yield dialog, label, combo, spin, font
    finally:
        dialog.close()
        qt_app.setFont(previous_font)
        qt_app.setStyle(previous_style)
        qt_app.processEvents()


def test_progress_uses_the_host_font_and_readable_foreground(controls):
    _dialog, label, _combo, _spin, host_font = controls
    assert label.font().pointSizeF() == host_font.pointSizeF()
    assert label.font().weight() >= QtGui.QFont.Weight.DemiBold
    foreground = label.palette().color(QtGui.QPalette.ColorRole.WindowText)
    for surface in ("color_bg", "color_surface", "color_surface_2"):
        assert _contrast(foreground, QtGui.QColor(TOKENS[surface])) >= 4.5
    assert label.height() >= label.fontMetrics().height()


def test_arrows_are_visible_pixels_inside_usable_click_areas(qt_app, controls):
    _dialog, _label, combo, spin, _font = controls
    combo_option = QtWidgets.QStyleOptionComboBox()
    combo.initStyleOption(combo_option)
    spin_option = QtWidgets.QStyleOptionSpinBox()
    spin.initStyleOption(spin_option)
    subcontrols = (
        (combo, combo_option, QtWidgets.QStyle.ComplexControl.CC_ComboBox,
         QtWidgets.QStyle.SubControl.SC_ComboBoxArrow),
        (spin, spin_option, QtWidgets.QStyle.ComplexControl.CC_SpinBox,
         QtWidgets.QStyle.SubControl.SC_SpinBoxUp),
        (spin, spin_option, QtWidgets.QStyle.ComplexControl.CC_SpinBox,
         QtWidgets.QStyle.SubControl.SC_SpinBoxDown),
    )
    rectangles = []
    background = QtGui.QColor(TOKENS["color_surface_2"])
    for widget, option, complex_control, subcontrol in subcontrols:
        rect = widget.style().subControlRect(complex_control, option, subcontrol, widget)
        assert rect.width() >= 24
        assert rect.height() >= 20
        assert widget.rect().contains(rect)
        image = widget.grab().toImage()
        interior = rect.adjusted(4, 4, -4, -4)
        scale = image.devicePixelRatio()
        readable_pixels = sum(
            _contrast(image.pixelColor(x, y), background) >= 4.5
            for y in range(floor(interior.top() * scale), ceil((interior.bottom() + 1) * scale))
            for x in range(floor(interior.left() * scale), ceil((interior.right() + 1) * scale))
        )
        assert readable_pixels >= 20, f"The {type(widget).__name__} arrow is absent or unreadable"
        rectangles.append(rect)

    QtTest.QTest.mouseClick(spin, QtCore.Qt.MouseButton.LeftButton, pos=rectangles[1].center())
    assert spin.value() == 51
    QtTest.QTest.mouseClick(spin, QtCore.Qt.MouseButton.LeftButton, pos=rectangles[2].center())
    assert spin.value() == 50
    QtTest.QTest.mouseClick(combo, QtCore.Qt.MouseButton.LeftButton, pos=rectangles[0].center())
    qt_app.processEvents()
    assert combo.view().isVisible()
    combo.hidePopup()


def test_packaged_control_resources_are_readable_svg_data(qt_app):
    build_stylesheet()
    for direction in ("up", "down"):
        resource = QtCore.QFile(f":/bl-catalog/controls/{direction}.svg")
        assert resource.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
        data = bytes(resource.readAll())
        resource.close()
        source = Path(__file__).resolve().parents[1] / "integrations" / "calibre" / "bl_catalog_calibre" / "images" / f"control-{direction}.svg"
        assert data == source.read_bytes()
