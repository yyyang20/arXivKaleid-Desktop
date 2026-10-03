# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""从 A0 SVG 母版确定性生成 Windows 多尺寸 ICO 与可选验收预览。"""
from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "assets/app-icon.svg"
OUTPUT = ROOT / "assets/app-icon.ico"
SIZES = (16, 24, 32, 48, 64, 256)
SMALL_FILL = {
    "navy": "#07294d",
    "white": "#f8fbff",
    "lightBlue": "#4daef2",
    "brightBlue": "#1677ff",
    "cyan": "#14d5d1",
}


def checked_output(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("icon_output_outside_project")
    return resolved


def simplified_svg(source: bytes) -> bytes:
    """16/24 px 使用无渐变版本，减少细尺寸的色带和边缘噪声。"""
    text = source.decode("utf-8")
    text = re.sub(r"\s*<defs>.*?</defs>", "", text, flags=re.DOTALL)
    for name, color in SMALL_FILL.items():
        text = text.replace(f'fill="url(#{name})"', f'fill="{color}"')
    return text.encode("utf-8")


def render(svg: bytes, size: int) -> bytes:
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise RuntimeError("app_icon_svg_invalid")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter, QRectF(0, 0, size, size))
    finally:
        painter.end()
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("app_icon_png_encode_failed")
    return bytes(payload)


def build_ico(master: bytes) -> tuple[bytes, dict[int, bytes]]:
    layers = {
        size: render(simplified_svg(master) if size <= 24 else master, size)
        for size in SIZES
    }
    header_size = 6 + 16 * len(layers)
    offset = header_size
    directory = []
    payloads = []
    for size, png in layers.items():
        dimension = 0 if size == 256 else size
        directory.append(struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset,
        ))
        payloads.append(png)
        offset += len(png)
    return struct.pack("<HHH", 0, 1, len(layers)) + b"".join(directory + payloads), layers


def write_preview(path: Path, layers: dict[int, bytes]) -> None:
    """并排显示浅色/深色背景及原生尺寸，供小图标人工验收。"""
    width, height = 980, 720
    canvas = QImage(width, height, QImage.Format.Format_RGB32)
    canvas.fill(QColor("#e9eff5"))
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QColor("#17243b"))
        painter.setFont(QFont("Microsoft YaHei UI", 12))
        painter.drawText(28, 35, "A0 多尺寸 ICO · 16/24 px 使用简化无渐变层")
        for row, (label, background) in enumerate((("浅色", "#f7fafd"), ("深色", "#17243b"))):
            top = 58 + row * 320
            painter.fillRect(24, top, width - 48, 300, QColor(background))
            painter.setPen(QColor("#17243b") if row == 0 else QColor("#f4f7fb"))
            painter.drawText(42, top + 30, label)
            x = 110
            for size, png in layers.items():
                image = QImage.fromData(png, "PNG")
                painter.drawImage(x, top + 46, image)
                painter.drawText(x, top + 286, f"{size} px")
                x += size + 70
    finally:
        painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(path)):
        raise RuntimeError("app_icon_preview_save_failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    # 预览中的字体与文本布局需要显式 Qt application；不进入事件循环。
    application = QGuiApplication.instance() or QGuiApplication([])
    master = MASTER.read_bytes()
    ico, layers = build_ico(master)
    checked_output(OUTPUT).write_bytes(ico)
    if args.preview:
        write_preview(checked_output(args.preview), layers)
    print(OUTPUT)
    application.processEvents()


if __name__ == "__main__":
    main()
