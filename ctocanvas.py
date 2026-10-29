#!/usr/bin/env python3
"""ctocanvas: draw boxes, text and lines on an infinite terminal canvas, or export one as ASCII, PNG, or SVG."""

import copy
import curses
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import click
import svgwrite
import yaml
from PIL import Image, ImageDraw, ImageFont

DEFAULT_DATA = "canvas.yaml"
DEFAULT_OUTPUT = "canvas"
FORMATS = ("interactive", "ascii", "png", "svg")

BG = "#ffffff"
INK = "#111111"
LINE = "#8aa4c4"
FILL = "#f4f7fb"
STROKE = "#2b6cb0"
FONT_MONO = "DejaVuSansMono.ttf"
FONT_SIZE = 20
FONT_EMOJI = "NotoColorEmoji.ttf"
CELL_W = 12  # one terminal column, in pixels
CELL_H = 24  # one terminal row, in pixels
MARGIN = 40
HEX = re.compile(r"#[0-9a-fA-F]{6}")
# (background, foreground); the first swatch clears the colors
PALETTE: list[tuple[str | None, str | None]] = [
    (None, None),
    ("#fde68a", "#78350f"),
    ("#bbf7d0", "#14532d"),
    ("#bfdbfe", "#1e3a8a"),
    ("#fecaca", "#7f1d1d"),
    ("#e9d5ff", "#581c87"),
    ("#fbcfe8", "#831843"),
    ("#1f2937", "#f9fafb"),
]


class DataError(Exception):
    pass


@dataclass(eq=False)
class Box:
    x: int
    y: int
    text: str
    bg: str | None = None
    fg: str | None = None

    @property
    def w(self) -> int:
        return cols(self.text) + 4

    h = 3


@dataclass(eq=False)
class Line:
    """A line from (x1, y1) to (x2, y2): horizontal first, then vertical."""

    x1: int
    y1: int
    x2: int
    y2: int
    fg: str | None = None


@dataclass
class Canvas:
    boxes: list[Box] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    @property
    def items(self) -> list[Box | Line]:
        return [*self.boxes, *self.lines]

    def remove(self, item: Box | Line) -> None:
        group = self.boxes if isinstance(item, Box) else self.lines
        group[:] = [i for i in group if i is not item]


def parse(data: Any) -> Canvas:
    if data is None:
        return Canvas()
    if not isinstance(data, dict):
        raise DataError("canvas is not a mapping")
    canvas = Canvas()
    for key, kind, fields in (("boxes", Box, ("x", "y")), ("lines", Line, ("x1", "y1", "x2", "y2"))):
        entries = data.get(key) or []
        if not isinstance(entries, list):
            raise DataError(f"'{key}' must be a list")
        for i, entry in enumerate(entries, start=1):
            where = f"{key}.{i}"
            if not isinstance(entry, dict):
                raise DataError(f"{where} is not a mapping")
            nums = []
            for name in fields:
                value = entry.get(name)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise DataError(f"{where}: '{name}' must be an integer")
                nums.append(value)
            colors = {}
            for name in ("bg", "fg") if kind is Box else ("fg",):
                value = entry.get(name)
                if value is not None and not (isinstance(value, str) and HEX.fullmatch(value)):
                    raise DataError(f"{where}: '{name}' must be a color like #aabbcc")
                colors[name] = value
            if kind is Box:
                text = entry.get("text")
                if not isinstance(text, str) or not text.strip() or "\n" in text:
                    raise DataError(f"{where}: 'text' must be a non-empty single line")
                canvas.boxes.append(Box(*nums, text.strip(), **colors))
            else:
                canvas.lines.append(Line(*nums, **colors))
    return canvas


def load(path: Path | str) -> Canvas:
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))


def dump(canvas: Canvas) -> dict:
    def colors(item: Box | Line) -> dict:
        return {k: v for k in ("bg", "fg") if (v := getattr(item, k, None))}

    return {
        "boxes": [{"x": b.x, "y": b.y, "text": b.text, **colors(b)} for b in canvas.boxes],
        "lines": [{"x1": l.x1, "y1": l.y1, "x2": l.x2, "y2": l.y2, **colors(l)} for l in canvas.lines],
    }


def save(canvas: Canvas, path: Path | str) -> None:
    Path(path).write_text(yaml.safe_dump(dump(canvas), allow_unicode=True, sort_keys=False), encoding="utf-8")


# --- cell grid -------------------------------------------------------------------------------


def cells(char: str) -> int:
    """Columns a character takes on a terminal."""
    if unicodedata.combining(char) or ord(char) in (0x200D, 0xFE0F):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def cols(text: str) -> int:
    return sum(cells(c) for c in text)


def clip(text: str, columns: int) -> str:
    out = ""
    for char in text:
        columns -= cells(char)
        if columns < 0:
            break
        out += char
    return out


N, E, S, W = 1, 2, 4, 8
JOIN = {1: "│", 4: "│", 5: "│", 2: "─", 8: "─", 10: "─", 3: "└", 6: "┌", 12: "┐", 9: "┘", 7: "├", 13: "┤", 14: "┬", 11: "┴", 15: "┼"}


def path(line: Line) -> list[tuple[int, int]]:
    """The cells a line covers, in order."""
    points = [(line.x1, line.y1)]
    sx = 1 if line.x2 >= line.x1 else -1
    sy = 1 if line.y2 >= line.y1 else -1
    points += [(x, line.y1) for x in range(line.x1 + sx, line.x2 + sx, sx)]
    points += [(line.x2, y) for y in range(line.y1 + sy, line.y2 + sy, sy)]
    return points


def direction(a: tuple[int, int], b: tuple[int, int]) -> int:
    return E if b[0] > a[0] else W if b[0] < a[0] else S if b[1] > a[1] else N


def opposite(d: int) -> int:
    return {N: S, S: N, E: W, W: E}[d]


Grid = dict[tuple[int, int], tuple[str, Box | Line]]


def raster(canvas: Canvas) -> Grid:
    """Every occupied cell mapped to its character and the item that drew it. Boxes cover lines."""
    masks: dict[tuple[int, int], int] = {}
    owner: dict[tuple[int, int], Line] = {}
    for line in canvas.lines:
        pts = path(line)
        for cell in pts:
            masks.setdefault(cell, 0)
            owner[cell] = line
        for a, b in zip(pts, pts[1:]):
            masks[a] |= direction(a, b)
            masks[b] |= opposite(direction(a, b))
    grid: Grid = {cell: (JOIN.get(mask, "•"), owner[cell]) for cell, mask in masks.items()}
    for box in canvas.boxes:
        right, bottom = box.x + box.w - 1, box.y + box.h - 1
        for x in range(box.x, right + 1):
            for y in range(box.y, bottom + 1):
                edge_x, edge_y = x in (box.x, right), y in (box.y, bottom)
                char = " "
                if edge_x and edge_y:
                    char = {(0, 0): "┌", (1, 0): "┐", (0, 1): "└", (1, 1): "┘"}[(x == right, y == bottom)]
                elif edge_y:
                    char = "─"
                elif edge_x:
                    char = "│"
                grid[(x, y)] = (char, box)
        x = box.x + 2
        for char in box.text:
            width = cells(char)
            if width:
                grid[(x, box.y + 1)] = (char, box)
                for extra in range(1, width):
                    grid[(x + extra, box.y + 1)] = ("", box)
            x += width
    return grid


def bounds(canvas: Canvas) -> tuple[int, int, int, int]:
    """Inclusive (min x, min y, max x, max y) of everything drawn; one cell at the origin when empty."""
    grid = raster(canvas)
    if not grid:
        return 0, 0, 0, 0
    xs = [x for x, _ in grid]
    ys = [y for _, y in grid]
    return min(xs), min(ys), max(xs), max(ys)


def item_at(canvas: Canvas, x: int, y: int) -> Box | Line | None:
    hit = raster(canvas).get((x, y))
    return hit[1] if hit else None


def ascii_canvas(canvas: Canvas) -> list[str]:
    grid = raster(canvas)
    if not grid:
        return []
    x0, y0, x1, y1 = bounds(canvas)
    rows = []
    for y in range(y0, y1 + 1):
        row = "".join((grid[(x, y)][0] if (x, y) in grid else " ") for x in range(x0, x1 + 1))
        rows.append(row.rstrip())
    return rows


# --- interactive -----------------------------------------------------------------------------

FAST = 10
QUIT_KEYS = {ord("q"), ord("Q")}
DELETE_KEYS = {ord("d"), curses.KEY_DC, curses.KEY_BACKSPACE, 127, 8}
EDIT_KEYS = {ord("e"), 10, 13, curses.KEY_ENTER}
UNDO_KEYS = {ord("u")}
SAVE_KEYS = {ord("s")}
EXPORT_KEYS = {ord("x")}
HOME_KEYS = {curses.KEY_HOME, ord("0")}
CANCEL_KEYS = {27}
HELP = "click draws · right click for menu · pick a color, then click an item · wheel/arrows scroll (shift: fast) · drag to move or pan · e edit · d delete · u undo · s save · x export · q quit"
BUTTON5 = getattr(curses, "BUTTON5_PRESSED", 0x200000)
CLICKS = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
SWATCH = 5  # screen columns per palette swatch, gap included
PAIRS: dict[tuple[str | None, str | None], int] = {}

Entry = tuple[str, str, Callable[[], None]]  # hotkey, label, action


def xterm256(color: str) -> int:
    r, g, b = (round(int(color[i : i + 2], 16) / 255 * 5) for i in (1, 3, 5))
    return 16 + 36 * r + 6 * g + b


def color_attr(fg: str | None, bg: str | None) -> int:
    """Curses attribute for a foreground/background pair; 0 when the terminal can't show colors."""
    if not (fg or bg) or not curses.has_colors() or curses.COLORS < 256:
        return 0
    if (fg, bg) not in PAIRS:
        if len(PAIRS) + 1 >= curses.COLOR_PAIRS:
            return 0
        curses.init_pair(len(PAIRS) + 1, xterm256(fg) if fg else -1, xterm256(bg) if bg else -1)
        PAIRS[(fg, bg)] = len(PAIRS) + 1
    return curses.color_pair(PAIRS[(fg, bg)])


def put(screen: "curses.window", y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if 0 <= y < height and 0 <= x < width:
        try:
            screen.addstr(y, x, clip(text, width - x), attr)
        except curses.error:  # writing the bottom-right cell raises after drawing
            pass


def ask(
    screen: "curses.window",
    label: str,
    text: str = "",
    redraw: Callable[[str], None] | None = None,
    cursor: Callable[[str], tuple[int, int]] | None = None,
    click_keeps: Callable[[], None] | None = None,
) -> str | None:
    """Edit one line of text. Returns None when cancelled. With click_keeps, clicking elsewhere keeps the text and calls it.

    By default it is typed in the bottom row after the label. With redraw and cursor the caller shows the text
    itself (redraw gets the text typed so far before each repaint) and cursor says where on screen it ends."""
    curses.curs_set(1)
    try:
        while True:
            if redraw:
                redraw(text)
            height, width = screen.getmaxyx()
            if cursor:
                y, x = cursor(text)
                screen.move(min(max(y, 0), height - 1), min(max(x, 0), width - 1))
            else:
                screen.move(height - 1, 0)
                screen.clrtoeol()
                shown = clip(label + text, width - 1)
                put(screen, height - 1, 0, shown, curses.A_BOLD)
                screen.move(height - 1, min(cols(shown), width - 1))
            screen.refresh()
            key = screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                return text.strip()
            if key == curses.KEY_MOUSE:
                try:
                    state = curses.getmouse()[4]
                except curses.error:
                    continue
                if click_keeps and state & CLICKS:
                    click_keeps()
                    return text.strip()
            elif key == "\x1b":
                return None
            if key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                text = text[:-1]
            elif key == "\x15":  # ctrl-u
                text = ""
            elif isinstance(key, str) and key.isprintable():
                text += key
    finally:
        curses.curs_set(0)


def menu(screen: "curses.window", sy: int, sx: int, entries: list[Entry]) -> int | None:
    """Pop up a menu with its corner at screen cell (sy, sx). Returns the chosen index, or None when dismissed.

    Choose with ↑/↓ and Enter, a hotkey (the underlined letter), or the mouse. Esc or a click outside dismisses."""
    height, width = screen.getmaxyx()
    inner = max(cols(label) for _, label, _ in entries) + 2
    h, w = len(entries) + 2, inner + 2
    top, left = max(min(sy, height - h), 0), max(min(sx, width - w), 0)
    win = curses.newwin(h, w, top, left)
    win.keypad(True)
    at = 0
    try:
        while True:
            win.erase()
            win.box()
            for i, (hotkey, label, _) in enumerate(entries):
                attr = curses.A_REVERSE if i == at else 0
                win.addstr(i + 1, 1, f" {label}".ljust(inner), attr)
                pos = label.lower().find(hotkey)
                if pos >= 0:
                    win.addstr(i + 1, 2 + pos, label[pos], attr | curses.A_UNDERLINE | curses.A_BOLD)
            win.refresh()
            key = win.get_wch()
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, state = curses.getmouse()
                except curses.error:
                    continue
                inside = left < mx < left + w - 1 and top < my < top + h - 1
                if inside and state & curses.REPORT_MOUSE_POSITION and not state & CLICKS:
                    at = my - top - 1
                elif state & CLICKS:
                    return my - top - 1 if inside else None
            elif key in ("\x1b", "q"):
                return None
            elif key == curses.KEY_UP:
                at = (at - 1) % len(entries)
            elif key == curses.KEY_DOWN:
                at = (at + 1) % len(entries)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                return at
            elif isinstance(key, str):
                for i, (hotkey, _, _) in enumerate(entries):
                    if key.lower() == hotkey:
                        return i
    finally:
        del win


@dataclass
class View:
    canvas: Canvas
    path: Path
    ox: int = 0  # canvas cell shown in the top-left corner of the screen
    oy: int = 0
    selected: Box | Line | None = None
    anchor: tuple[int, int] | None = None  # first end of a line being drawn
    moving: tuple[Box | Line, int, int] | None = None  # item chosen to move and the cell it was chosen at
    grab: tuple[Box | Line | None, int, int] | None = None  # item (or None: the canvas) pressed and the cell it was at
    dragged: bool = False
    undo: list[Canvas] = field(default_factory=list)
    dirty: bool = False
    message: str = ""
    tool: str = "box"  # what a click on empty space draws: "box" or "line"; kept until another is chosen
    brush: int | None = None  # palette swatch that clicking an item applies
    pending: tuple[int, int, int] | None = None  # mouse event read by key(), handled by the main loop

    def remember(self) -> None:
        self.undo.append(copy.deepcopy(self.canvas))
        del self.undo[:-100]
        self.dirty = True

    def undo_last(self) -> None:
        if not self.undo:
            self.message = "nothing to undo"
            return
        self.canvas = self.undo.pop()
        self.selected = self.anchor = self.grab = self.moving = None
        self.dirty = True

    def cell(self, sx: int, sy: int) -> tuple[int, int]:
        return sx + self.ox, sy + self.oy

    def scroll(self, dx: int, dy: int) -> None:
        self.ox += dx
        self.oy += dy

    def shift(self, item: Box | Line, dx: int, dy: int) -> None:
        if isinstance(item, Box):
            item.x += dx
            item.y += dy
        else:
            item.x1 += dx
            item.x2 += dx
            item.y1 += dy
            item.y2 += dy

    def add_box(self, screen: "curses.window", x: int, y: int) -> None:
        box = Box(x, y, "")
        self.remember()
        self.canvas.boxes.append(box)
        self.selected = box
        self.edit(screen, box, new=True)

    def edit(self, screen: "curses.window", box: Box, new: bool = False) -> None:
        """Ask for the text of a box; it grows with every key typed. Cancelling restores it (or drops a new box)."""
        original = box.text
        if not new:
            self.remember()
        dirty = self.dirty

        def show(text: str) -> None:
            box.text = text
            draw(screen, self)

        def end(text: str) -> tuple[int, int]:
            return box.y + 1 - self.oy, box.x + 2 + cols(text) - self.ox

        self.message = "type the text in the box · Enter or a click away keeps it · Esc cancels"
        text = ask(screen, "text: ", original, show, end, click_keeps=self.unselect)
        self.message = ""
        if text:
            box.text = text
            self.dirty = True
            return
        box.text = original
        self.undo.pop()
        self.dirty = dirty and len(self.undo) > 0
        if new:
            self.canvas.remove(box)
            self.selected = None

    def unselect(self) -> None:
        self.selected = None

    def pick(self, sx: int) -> None:
        """Choose the palette swatch at screen column sx; picking the chosen one again puts the brush down."""
        index, within = divmod(sx, SWATCH)
        if within >= SWATCH - 1 or index >= len(PALETTE):
            return
        self.brush = None if self.brush == index else index
        self.message = "click an item to color it (Esc cancels)" if self.brush is not None else ""

    def paint(self, item: Box | Line) -> None:
        bg, fg = PALETTE[self.brush]
        self.remember()
        item.fg = fg
        if isinstance(item, Box):
            item.bg = bg
        self.selected = None

    def delete(self) -> None:
        if self.selected is not None:
            self.remember()
            self.canvas.remove(self.selected)
            self.selected = None

    def finish_line(self, x: int, y: int) -> None:
        x1, y1 = self.anchor
        self.anchor = None
        if (x1, y1) != (x, y):
            self.remember()
            line = Line(x1, y1, x, y)
            self.canvas.lines.append(line)
            self.selected = line

    def press(self, sx: int, sy: int) -> None:
        x, y = self.cell(sx, sy)
        self.grab = (item_at(self.canvas, x, y), x, y)
        self.dragged = False

    def motion(self, sx: int, sy: int) -> None:
        """Dragging moves the pressed item, or pans the canvas when it was pressed on empty space."""
        if self.grab is None:
            return
        item, gx, gy = self.grab
        x, y = self.cell(sx, sy)
        if (x, y) == (gx, gy):
            return
        if item is None:  # keep the grabbed cell under the pointer
            self.ox += gx - x
            self.oy += gy - y
        else:
            if not self.dragged:
                self.remember()
            self.selected = item
            self.shift(item, x - gx, y - gy)
            self.grab = (item, x, y)
        self.dragged = True

    def release(self, screen: "curses.window", sx: int, sy: int) -> None:
        """A press and release on one cell is a click: finish what is pending, or draw with the current tool."""
        grab, dragged = self.grab, self.dragged
        self.grab, self.dragged = None, False
        if dragged or grab is None:
            return
        x, y = self.cell(sx, sy)
        if self.moving is not None:
            item, fx, fy = self.moving
            self.moving = None
            self.remember()
            self.shift(item, x - fx, y - fy)
        elif self.anchor is not None:
            self.finish_line(x, y)
        elif self.brush is not None and grab[0] is not None:
            self.paint(grab[0])
        elif self.tool == "line":
            self.start_line(x, y)
        elif grab[0] is None:
            self.add_box(screen, x, y)
        else:
            self.selected = grab[0]

    def choose(self, tool: str) -> None:
        self.tool = tool
        self.message = f"{tool} tool: click to draw a {tool}"

    def context_menu(self, screen: "curses.window", sx: int, sy: int) -> None:
        """Right click: choose the drawing tool, or act on the item or canvas under the pointer."""
        x, y = self.cell(sx, sy)
        hit = self.selected = item_at(self.canvas, x, y)
        mark = lambda tool: "●" if self.tool == tool else "○"
        entries: list[Entry] = [
            ("b", f"{mark('box')} Draw boxes", lambda: self.choose("box")),
            ("l", f"{mark('line')} Draw lines", lambda: self.choose("line")),
        ]
        if hit is not None:
            if isinstance(hit, Box):
                entries.append(("e", "Edit text", lambda: self.edit(screen, hit)))
            entries.append(("m", "Move", lambda: self.start_move(hit, x, y)))
            entries.append(("d", "Delete", self.delete))
        else:
            entries += [
                ("u", "Undo", self.undo_last),
                ("s", "Save", self.save),
                ("x", "Export...", lambda: self.export(screen)),
                ("o", "Go to origin", self.go_home),
            ]
        choice = menu(screen, sy, sx, entries)
        if choice is not None:
            entries[choice][2]()

    def start_line(self, x: int, y: int) -> None:
        self.anchor = (x, y)
        self.message = "click where the line ends (Esc cancels)"

    def start_move(self, item: Box | Line, x: int, y: int) -> None:
        self.moving = (item, x, y)
        self.message = "click the new position (Esc cancels)"

    def go_home(self) -> None:
        self.ox = self.oy = 0

    def save(self) -> None:
        save(self.canvas, self.path)
        self.dirty = False
        self.message = f"saved {self.path}"

    def wheel(self, up: bool, shift: bool) -> None:
        step = -3 if up else 3
        if shift:
            self.scroll(step * 2, 0)
        else:
            self.scroll(0, step)

    def key(self, screen: "curses.window", key: int) -> bool:
        """Handle one key. Returns False to quit."""
        height, width = screen.getmaxyx()
        self.message = ""
        if key in QUIT_KEYS:
            if self.dirty and ask(screen, "Unsaved changes. Quit anyway? (y/N) ", "") not in ("y", "Y", "yes"):
                return True
            return False
        if key in CANCEL_KEYS:
            self.anchor = self.selected = self.moving = self.brush = None
        elif key == curses.KEY_LEFT:
            self.scroll(-2, 0)
        elif key == curses.KEY_RIGHT:
            self.scroll(2, 0)
        elif key == curses.KEY_UP:
            self.scroll(0, -1)
        elif key == curses.KEY_DOWN:
            self.scroll(0, 1)
        elif key == curses.KEY_SLEFT:
            self.scroll(-FAST * 2, 0)
        elif key == curses.KEY_SRIGHT:
            self.scroll(FAST * 2, 0)
        elif key == curses.KEY_SR:
            self.scroll(0, -FAST)
        elif key == curses.KEY_SF:
            self.scroll(0, FAST)
        elif key == curses.KEY_PPAGE:
            self.scroll(0, -(height - 2))
        elif key == curses.KEY_NPAGE:
            self.scroll(0, height - 2)
        elif key in HOME_KEYS:
            self.go_home()
        elif key in DELETE_KEYS:
            self.delete()
        elif key in EDIT_KEYS and isinstance(self.selected, Box):
            self.edit(screen, self.selected)
        elif key in UNDO_KEYS:
            self.undo_last()
        elif key in SAVE_KEYS:
            self.save()
        elif key in EXPORT_KEYS:
            self.export(screen)
        elif key == curses.KEY_MOUSE:
            try:
                _, sx, sy, _, state = curses.getmouse()
                self.pending = (sx, sy, state)
            except curses.error:
                pass
        return True

    def export(self, screen: "curses.window") -> None:
        default = Path(DEFAULT_OUTPUT) / f"{self.path.stem}.png"
        name = ask(screen, "export to (.txt .png .svg .yaml): ", str(default))
        if not name:
            return
        try:
            write(self.canvas, Path(name))
            self.message = f"wrote {name}"
        except (OSError, DataError) as err:
            self.message = str(err)


def draw(screen: "curses.window", view: View) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    grid = raster(view.canvas)
    for (x, y), (char, item) in grid.items():
        sx, sy = x - view.ox, y - view.oy
        painted = char.strip() or (isinstance(item, Box) and item.bg)
        if char and painted and 0 <= sy < height - 2 and 0 <= sx < width:
            attr = color_attr(item.fg, getattr(item, "bg", None))
            if item is view.selected:
                attr |= curses.A_REVERSE
            elif isinstance(item, Line):
                attr |= curses.A_DIM
            put(screen, sy, sx, char, attr)
    if view.anchor is not None:
        put(screen, view.anchor[1] - view.oy, view.anchor[0] - view.ox, "+", curses.A_BOLD)
    for i, (bg, fg) in enumerate(PALETTE):
        label = "[Aa]" if i == view.brush else " Aa " if bg else " ∅  "
        put(screen, height - 2, i * SWATCH, label, color_attr(fg, bg) | (curses.A_BOLD if i == view.brush else 0))
    status = view.message or HELP
    where = f" {'*' if view.dirty else ''}{view.ox},{view.oy}"
    put(screen, height - 1, 0, clip(status, max(width - cols(where) - 1, 0)), curses.A_DIM)
    put(screen, height - 1, max(width - cols(where), 0), where, curses.A_BOLD)


def browse(screen: "curses.window", canvas: Canvas, path: Path) -> None:
    curses.curs_set(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.mouseinterval(0)  # report presses and releases, not merged clicks
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    screen.keypad(True)
    print("\033[?1003h", end="", flush=True)  # report motion even with xterm-style terminfo
    view = View(canvas, path)
    try:
        while True:
            draw(screen, view)
            screen.refresh()
            try:
                raw = screen.get_wch()
            except KeyboardInterrupt:
                continue
            key = ord(raw) if isinstance(raw, str) else raw
            if not view.key(screen, key):
                return
            if view.pending is not None:
                sx, sy, state = view.pending
                view.pending = None
                handle_mouse(screen, view, sx, sy, state)
    finally:
        print("\033[?1003l", end="", flush=True)


def handle_mouse(screen: "curses.window", view: View, sx: int, sy: int, state: int) -> None:
    height, _ = screen.getmaxyx()
    shift = bool(state & (curses.BUTTON_SHIFT | curses.BUTTON_CTRL))
    if state & curses.BUTTON4_PRESSED:
        view.wheel(True, shift)
    elif state & BUTTON5:
        view.wheel(False, shift)
    elif sy >= height - 1:
        return
    elif sy == height - 2 and state & curses.BUTTON1_PRESSED:
        view.pick(sx)
    elif state & curses.BUTTON3_PRESSED:
        view.context_menu(screen, sx, sy)
    elif state & curses.BUTTON1_PRESSED:
        view.press(sx, sy)
    elif state & curses.BUTTON1_RELEASED:
        view.release(screen, sx, sy)
    elif state & curses.REPORT_MOUSE_POSITION:
        view.motion(sx, sy)


# --- graphic ---------------------------------------------------------------------------------


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(FONT_MONO, size)
    except OSError:
        return ImageFont.load_default()


@dataclass
class Layout:
    boxes: list[tuple[Box, float, float, float, float]]  # box and its rectangle, between cell centres
    lines: list[list[tuple[float, float]]]
    canvas_lines: list[Line]
    width: int
    height: int
    origin: tuple[int, int]

    def px(self, x: float, y: float) -> tuple[float, float]:
        """Pixel position of the centre of cell (x, y)."""
        return MARGIN + (x - self.origin[0] + 0.5) * CELL_W, MARGIN + (y - self.origin[1] + 0.5) * CELL_H


def layout(canvas: Canvas) -> Layout:
    x0, y0, x1, y1 = bounds(canvas)
    lay = Layout([], [], list(canvas.lines), 0, 0, (x0, y0))
    lay.width = int(2 * MARGIN + (x1 - x0 + 1) * CELL_W)
    lay.height = int(2 * MARGIN + (y1 - y0 + 1) * CELL_H)
    for b in canvas.boxes:
        left, top = lay.px(b.x, b.y)
        right, bottom = lay.px(b.x + b.w - 1, b.y + b.h - 1)
        lay.boxes.append((b, left, top, right, bottom))
    grid = raster(canvas)
    for line in canvas.lines:
        pts = path(line)
        corners = [(line.x1, line.y1), (line.x2, line.y1), (line.x2, line.y2)]
        if len(pts) > 1:  # reach the border of a box the line ends against
            for end, d in ((0, opposite(direction(pts[0], pts[1]))), (-1, direction(pts[-2], pts[-1]))):
                x, y = pts[end]
                dx, dy = {E: (1, 0), W: (-1, 0), S: (0, 1), N: (0, -1)}[d]
                if isinstance(grid.get((x + dx, y + dy), (None, None))[1], Box):
                    pts[end] = (x + dx, y + dy)
            corners = dedupe([pts[0], *[c for c in corners[1:-1] if c in pts[1:-1]], pts[-1]])
        lay.lines.append([lay.px(x, y) for x, y in corners])
    return lay


def dedupe(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]


def is_emoji(char: str) -> bool:
    code = ord(char)
    return code >= 0x1F000 or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF


def draw_text(image: Image.Image, x: float, y: float, text: str, font: ImageFont.ImageFont, fill: str = INK) -> None:
    """Draw text on the cell grid, one character per cell; emoji use the color emoji font if installed."""
    draw = ImageDraw.Draw(image)
    for char in text:
        width = cells(char)
        if width and is_emoji(char):
            try:  # the color emoji font only exists at one size: draw it there, then scale down
                glyph = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
                ImageDraw.Draw(glyph).text((0, 0), char, font=ImageFont.truetype(FONT_EMOJI, 109), embedded_color=True)
                size = width * CELL_W
                small = glyph.resize((size, round(size * 128 / 136)), Image.LANCZOS)
                image.paste(small, (round(x), round(y - small.height / 2)), small)
            except OSError:
                pass
        elif width:
            draw.text((x, y), char, fill=fill, font=font, anchor="lm")
        x += width * CELL_W


def render_png(canvas: Canvas, path: Path | str) -> None:
    lay = layout(canvas)
    image = Image.new("RGB", (lay.width, lay.height), BG)
    draw = ImageDraw.Draw(image)
    font = load_font(FONT_SIZE)
    for line, points in zip(lay.canvas_lines, lay.lines):
        draw.line(points, fill=line.fg or LINE, width=2, joint="curve")
    for b, left, top, right, bottom in lay.boxes:
        draw.rounded_rectangle([left, top, right, bottom], radius=6, fill=b.bg or FILL, outline=b.fg or STROKE, width=2)
        draw_text(image, left + 1.5 * CELL_W, (top + bottom) / 2, b.text, font, b.fg or INK)
    image.save(path)


def render_svg(canvas: Canvas, path: Path | str) -> None:
    lay = layout(canvas)
    drawing = svgwrite.Drawing(size=(lay.width, lay.height), profile="full")
    drawing.viewbox(0, 0, lay.width, lay.height)
    drawing["font-family"] = "DejaVu Sans Mono, Menlo, Consolas, monospace"
    drawing.add(drawing.rect(insert=(0, 0), size=(lay.width, lay.height), fill=BG))
    for line, points in zip(lay.canvas_lines, lay.lines):
        drawing.add(drawing.polyline(points, fill="none", stroke=line.fg or LINE, stroke_width=2, stroke_linejoin="round"))
    for b, left, top, right, bottom in lay.boxes:
        drawing.add(drawing.rect(insert=(left, top), size=(right - left, bottom - top), rx=6, ry=6, fill=b.bg or FILL, stroke=b.fg or STROKE, stroke_width=2))
        drawing.add(
            drawing.text(
                b.text,
                insert=(left + 1.5 * CELL_W, (top + bottom) / 2),
                font_size=FONT_SIZE,
                fill=b.fg or INK,
                dominant_baseline="central",
            )
        )
    drawing.saveas(path, pretty=True)


# --- cli -------------------------------------------------------------------------------------

WRITERS = {
    ".png": render_png,
    ".svg": render_svg,
    ".txt": lambda canvas, path: Path(path).write_text("\n".join(ascii_canvas(canvas)) + "\n", encoding="utf-8"),
    ".yaml": save,
}


def write(canvas: Canvas, path: Path) -> None:
    """Write the canvas in the format its file extension names."""
    writer = WRITERS.get(path.suffix.lower())
    if writer is None:
        raise DataError(f"unknown extension '{path.suffix}' (use {', '.join(WRITERS)})")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer(canvas, path)


def load_canvas(path: str, missing_ok: bool) -> Canvas:
    if missing_ok and not Path(path).exists():
        return Canvas()
    try:
        return load(path)
    except (OSError, yaml.YAMLError, DataError) as err:
        raise click.ClickException(str(err)) from None


@click.command()
@click.argument("data", default=DEFAULT_DATA)
@click.option("--format", "fmt", type=click.Choice(FORMATS), default="interactive", show_default=True, help="interactive edits the canvas in the terminal; ascii prints it; png and svg export it.")
@click.option("-o", "--output", help="Output file (default for png and svg: canvas/<name>.<format>; with ascii, writes the text to this file instead of printing).")
def cli(data: str, fmt: str, output: str | None) -> None:
    """Draw on an infinite canvas stored in a YAML file."""
    interactive = fmt == "interactive" and sys.stdin.isatty() and sys.stdout.isatty() and output is None
    canvas = load_canvas(data, missing_ok=interactive)

    if interactive:
        os.environ.setdefault("ESCDELAY", "25")
        curses.wrapper(browse, canvas, Path(data))
        return

    if fmt in ("interactive", "ascii"):
        text = "\n".join(ascii_canvas(canvas))
        if output is None:
            click.echo(text)
        else:
            Path(output).write_text(text + "\n", encoding="utf-8")
            click.echo(f"Wrote {output}")
        return

    target = Path(output) if output else Path(DEFAULT_OUTPUT) / f"{Path(data).stem}.{fmt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    (render_png if fmt == "png" else render_svg)(canvas, target)


if __name__ == "__main__":
    cli()
