"""Minimal dependency free PNG plotting.

CODE V runs headless, so analysis output has to be turned into an image the
client can look at. The service deliberately avoids a heavy plotting stack:
this module writes PNG files directly with zlib and the standard library, which
keeps the service installable offline and makes the drawing code auditable.

Only what the analyses need is implemented: a scatter plot for spot diagrams
and a line plot for MTF curves, both with a numeric 3x5 font for axis labels.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

RGB = tuple[int, int, int]

WHITE: RGB = (255, 255, 255)
BLACK: RGB = (0, 0, 0)
GREY: RGB = (150, 150, 150)
LIGHT_GREY: RGB = (225, 225, 225)
BLUE: RGB = (31, 90, 190)
RED: RGB = (200, 40, 40)
GREEN: RGB = (20, 140, 70)
ORANGE: RGB = (220, 130, 20)

# 3x5 pixel glyphs, one string per row, top row first.
FONT: dict[str, tuple[str, ...]] = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": ("..#", "..#", "..#", "..#", "..#"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "-": ("...", "...", "###", "...", "..."),
    "+": ("...", ".#.", "###", ".#.", "..."),
    ".": ("...", "...", "...", "...", ".#."),
    ",": ("...", "...", "...", ".#.", "#.."),
    ":": ("...", ".#.", "...", ".#.", "..."),
    "e": ("...", ".##", "##.", "#..", ".##"),
    "/": ("..#", "..#", ".#.", "#..", "#.."),
    " ": ("...", "...", "...", "...", "..."),
}


class Canvas:
    """A tiny RGB raster canvas that can serialise itself as a PNG."""

    def __init__(self, width: int, height: int, background: RGB = WHITE) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("canvas size must be positive")
        self.width = width
        self.height = height
        self._rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def fill(self, colour: RGB) -> None:
        pixel = bytes(colour)
        for row in self._rows:
            row[:] = pixel * self.width

    def set_pixel(self, x: int, y: int, colour: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = x * 3
            self._rows[y][offset : offset + 3] = bytes(colour)

    def hline(self, x0: int, x1: int, y: int, colour: RGB) -> None:
        if x0 > x1:
            x0, x1 = x1, x0
        for x in range(x0, x1 + 1):
            self.set_pixel(x, y, colour)

    def vline(self, x: int, y0: int, y1: int, colour: RGB) -> None:
        if y0 > y1:
            y0, y1 = y1, y0
        for y in range(y0, y1 + 1):
            self.set_pixel(x, y, colour)

    def line(self, x0: int, y0: int, x1: int, y1: int, colour: RGB) -> None:
        """Bresenham line, used for axes and MTF curves."""
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set_pixel(x0, y0, colour)
            if x0 == x1 and y0 == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x0 += sx
            if err2 <= dx:
                err += dx
                y0 += sy

    def rect(self, x0: int, y0: int, x1: int, y1: int, colour: RGB) -> None:
        self.hline(x0, x1, y0, colour)
        self.hline(x0, x1, y1, colour)
        self.vline(x0, y0, y1, colour)
        self.vline(x1, y0, y1, colour)

    def dot(self, x: int, y: int, colour: RGB, radius: int = 1) -> None:
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius + 1:
                    self.set_pixel(x + dx, y + dy, colour)

    def circle(self, cx: int, cy: int, radius: int, colour: RGB) -> None:
        if radius <= 0:
            self.set_pixel(cx, cy, colour)
            return
        x = radius
        y = 0
        err = 1 - radius
        while x >= y:
            for px, py in (
                (cx + x, cy + y),
                (cx - x, cy + y),
                (cx + x, cy - y),
                (cx - x, cy - y),
                (cx + y, cy + x),
                (cx - y, cy + x),
                (cx + y, cy - x),
                (cx - y, cy - x),
            ):
                self.set_pixel(px, py, colour)
            y += 1
            if err < 0:
                err += 2 * y + 1
            else:
                x -= 1
                err += 2 * (y - x) + 1

    def text(self, x: int, y: int, value: str, colour: RGB = BLACK, scale: int = 1) -> None:
        cursor = x
        for character in value:
            glyph = FONT.get(character) or FONT.get(character.lower()) or FONT[" "]
            for row_index, row in enumerate(glyph):
                for column_index, cell in enumerate(row):
                    if cell != "#":
                        continue
                    for sy in range(scale):
                        for sx in range(scale):
                            self.set_pixel(
                                cursor + column_index * scale + sx,
                                y + row_index * scale + sy,
                                colour,
                            )
            cursor += (len(glyph[0]) + 1) * scale

    def text_width(self, value: str, scale: int = 1) -> int:
        return len(value) * 4 * scale

    def to_png(self) -> bytes:
        raw = bytearray()
        for row in self._rows:
            raw.append(0)  # filter type 0 (None) for every scanline
            raw.extend(row)

        def chunk(tag: bytes, payload: bytes) -> bytes:
            body = tag + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )


@dataclass
class Axes:
    """Plot area plus its data ranges."""

    left: int
    top: int
    right: int
    bottom: int
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def to_pixel(self, x: float, y: float) -> tuple[int, int]:
        span_x = self.x_max - self.x_min or 1.0
        span_y = self.y_max - self.y_min or 1.0
        px = self.left + (x - self.x_min) / span_x * (self.right - self.left)
        py = self.bottom - (y - self.y_min) / span_y * (self.bottom - self.top)
        return int(round(px)), int(round(py))


def _nice_range(low: float, high: float, ticks: int = 4) -> tuple[float, float, float]:
    """Return a padded range and a round tick step for axis labels."""
    if high <= low:
        high = low + 1.0
    span = high - low
    padding = span * 0.1
    low -= padding
    high += padding
    step = (high - low) / max(ticks, 1)
    magnitude = 10 ** int(f"{step:e}".split("e")[1])
    for factor in (1, 2, 2.5, 5, 10):
        candidate = factor * magnitude
        if candidate >= step:
            step = candidate
            break
    return low, high, step


def _format_tick(value: float) -> str:
    if abs(value) >= 1000 or (value != 0 and abs(value) < 0.01):
        return f"{value:.1e}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def scatter_plot(
    points: list[tuple[float, float]],
    *,
    width: int = 420,
    height: int = 420,
    reference_radius: float | None = None,
    airy_radius: float | None = None,
    title: str = "",
) -> Canvas:
    """Plot ray coordinates in lens units with a symmetric field of view.

    The grey circle is the reference radius (the native 100% spot radius); the
    green one is the Airy disk radius, which the caller computes.
    """
    canvas = Canvas(width, height)
    margin_left, margin_top, margin_right, margin_bottom = 52, 34, 18, 34
    data_x = [p[0] for p in points] or [0.0]
    data_y = [p[1] for p in points] or [0.0]
    limit = max(max(abs(v) for v in data_x), max(abs(v) for v in data_y), 1e-6)
    if reference_radius:
        limit = max(limit, reference_radius)
    if airy_radius:
        limit = max(limit, airy_radius)
    limit *= 1.15

    axes = Axes(
        margin_left,
        margin_top,
        width - margin_right,
        height - margin_bottom,
        -limit,
        limit,
        -limit,
        limit,
    )
    _draw_frame(canvas, axes, title)
    for x, y in points:
        px, py = axes.to_pixel(x, y)
        canvas.dot(px, py, BLUE, 1)
    if reference_radius:
        radius_px = int(abs(axes.to_pixel(reference_radius, 0)[0] - axes.to_pixel(0, 0)[0]))
        canvas.circle(
            *axes.to_pixel(0.0, 0.0),
            radius_px,
            GREY,
        )
    if airy_radius:
        radius_px = int(abs(axes.to_pixel(airy_radius, 0)[0] - axes.to_pixel(0, 0)[0]))
        canvas.circle(*axes.to_pixel(0.0, 0.0), radius_px, GREEN)
    canvas.dot(*axes.to_pixel(0.0, 0.0), RED, 1)
    return canvas


def line_plot(
    series: list[tuple[str, list[tuple[float, float]], RGB]],
    *,
    width: int = 520,
    height: int = 380,
    title: str = "",
    y_label: str = "",
) -> Canvas:
    """Plot one or more (x, y) series with axis ticks and a legend."""
    canvas = Canvas(width, height)
    margin_left, margin_top, margin_right, margin_bottom = 58, 34, 92, 38
    xs = [x for _, points, _ in series for x, _ in points] or [0.0]
    ys = [y for _, points, _ in series for _, y in points] or [0.0]
    x_min, x_max, _ = _nice_range(min(xs), max(xs))
    y_min, y_max, _ = _nice_range(0.0, max(max(ys), 0.05))
    y_min = 0.0
    axes = Axes(margin_left, margin_top, width - margin_right, height - margin_bottom, x_min, x_max, y_min, y_max)
    _draw_frame(canvas, axes, title, y_label=y_label)

    for index, (label, points, colour) in enumerate(series):
        ordered = sorted(points)
        for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
            canvas.line(*axes.to_pixel(x0, y0), *axes.to_pixel(x1, y1), colour)
        legend_y = margin_top + index * 12
        canvas.line(width - margin_right + 8, legend_y + 2, width - margin_right + 24, legend_y + 2, colour)
        canvas.text(width - margin_right + 28, legend_y, label, BLACK, 1)
    return canvas


def _draw_frame(canvas: Canvas, axes: Axes, title: str, y_label: str = "") -> None:
    canvas.rect(axes.left, axes.top, axes.right, axes.bottom, BLACK)
    if title:
        canvas.text(axes.left, 10, title, BLACK, 1)
    if y_label:
        canvas.text(4, axes.top - 14, y_label, BLACK, 1)

    x_min, x_max, _ = _nice_range(axes.x_min, axes.x_max)
    y_min, y_max, _ = _nice_range(axes.y_min, axes.y_max)
    for step_index in range(5):
        fraction = step_index / 4
        x_value = axes.x_min + (axes.x_max - axes.x_min) * fraction
        y_value = axes.y_min + (axes.y_max - axes.y_min) * fraction
        px, _ = axes.to_pixel(x_value, 0.0)
        _, py = axes.to_pixel(0.0, y_value)
        canvas.vline(px, axes.top, axes.bottom, LIGHT_GREY)
        canvas.hline(axes.left, axes.right, py, LIGHT_GREY)
        canvas.text(px - 8, axes.bottom + 6, _format_tick(x_value), BLACK, 1)
        canvas.text(6, py - 2, _format_tick(y_value), BLACK, 1)
    del x_min, x_max, y_min, y_max
