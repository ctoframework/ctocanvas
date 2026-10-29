import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from PIL import Image

import ctocanvas
from ctocanvas import Box, Canvas, Line

EXAMPLE = Path(__file__).parent / "examples" / "demo.yaml"


def run(*args):
    return CliRunner().invoke(ctocanvas.cli, [str(a) for a in args])


def small():
    return Canvas([Box(0, 0, "a"), Box(10, 0, "b")], [Line(5, 1, 10, 1)])


def test_load_example():
    canvas = ctocanvas.load(EXAMPLE)
    assert len(canvas.boxes) == 4 and len(canvas.lines) == 5


def test_box_size_counts_wide_characters():
    assert Box(0, 0, "abc").w == 7
    assert Box(0, 0, "🚀").w == 6


def test_ascii_box_and_line():
    assert ctocanvas.ascii_canvas(small()) == ["┌───┐     ┌───┐", "│ a │─────│ b │", "└───┘     └───┘"]


def test_line_corner_and_junction():
    canvas = Canvas(lines=[Line(0, 0, 2, 2), Line(1, 0, 1, 2)])
    grid = ctocanvas.raster(canvas)
    assert grid[(2, 0)][0] == "┐" and grid[(1, 0)][0] == "┬" and grid[(2, 2)][0] == "│"


def test_negative_coordinates_and_bounds():
    canvas = Canvas([Box(-10, -5, "x")])
    assert ctocanvas.bounds(canvas) == (-10, -5, -10 + 4, -3)
    assert len(ctocanvas.ascii_canvas(canvas)) == 3


def test_item_at():
    canvas = small()
    assert ctocanvas.item_at(canvas, 2, 1) is canvas.boxes[0]
    assert ctocanvas.item_at(canvas, 7, 1) is canvas.lines[0]
    assert ctocanvas.item_at(canvas, 7, 5) is None


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "c.yaml"
    ctocanvas.save(small(), path)
    assert ctocanvas.dump(ctocanvas.load(path)) == ctocanvas.dump(small())


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"boxes": "x"},
        {"boxes": [{"x": 1, "y": 1}]},
        {"boxes": [{"x": "1", "y": 1, "text": "a"}]},
        {"boxes": [{"x": 1, "y": 1, "text": "a\nb"}]},
        {"lines": [{"x1": 1, "y1": 1, "x2": 2}]},
    ],
)
def test_loader_rejects_bad_data(data):
    with pytest.raises(ctocanvas.DataError):
        ctocanvas.parse(data)


def test_cli_ascii(tmp_path):
    out = tmp_path / "c.txt"
    assert run(EXAMPLE, "--format", "ascii", "-o", out).exit_code == 0
    assert "Idea" in out.read_text(encoding="utf-8")


def test_cli_png_and_svg(tmp_path):
    assert run(EXAMPLE, "--format", "png", "-o", tmp_path / "c.png").exit_code == 0
    assert Image.open(tmp_path / "c.png").width > 100
    assert run(EXAMPLE, "--format", "svg", "-o", tmp_path / "c.svg").exit_code == 0
    root = ET.parse(tmp_path / "c.svg").getroot()
    assert root.tag.endswith("svg")


def test_empty_canvas_exports(tmp_path):
    path = tmp_path / "e.yaml"
    path.write_text(yaml.safe_dump({}), encoding="utf-8")
    assert run(path, "--format", "png", "-o", tmp_path / "e.png").exit_code == 0
    assert run(path, "--format", "ascii").output.strip() == ""


def test_missing_file_is_an_error(tmp_path):
    result = run(tmp_path / "nope.yaml", "--format", "ascii")
    assert result.exit_code != 0


def test_write_picks_format_by_extension(tmp_path):
    ctocanvas.write(small(), tmp_path / "out" / "c.txt")
    assert (tmp_path / "out" / "c.txt").exists()
    with pytest.raises(ctocanvas.DataError):
        ctocanvas.write(small(), tmp_path / "c.bmp")


def test_view_undo_and_move():
    view = ctocanvas.View(small(), Path("x.yaml"))
    view.selected = view.canvas.boxes[0]
    view.delete()
    assert len(view.canvas.boxes) == 1
    view.undo_last()
    assert len(view.canvas.boxes) == 2
    view.press(1, 1)
    view.motion(4, 3)
    assert (view.canvas.boxes[0].x, view.canvas.boxes[0].y) == (3, 2)
    view.release(None, 4, 3)  # the end of a drag is not a click
    view.press(20, 20)
    view.motion(18, 18)
    assert (view.ox, view.oy) == (2, 2)


def right_click(view, monkeypatch, choose, sx, sy):
    """Right click a cell and pick the menu entry whose hotkey is `choose`."""
    seen = []

    def fake_menu(screen, y, x, entries):
        seen.append([label for _, label, _ in entries])
        return next(i for i, (hotkey, _, _) in enumerate(entries) if hotkey == choose)

    monkeypatch.setattr(ctocanvas, "menu", fake_menu)
    view.context_menu(None, sx, sy)
    return seen[0]


def click(view, sx, sy):
    view.press(sx, sy)
    view.release(None, sx, sy)


def test_click_draws_boxes_until_the_tool_changes(monkeypatch):
    added = []
    monkeypatch.setattr(ctocanvas.View, "add_box", lambda self, screen, x, y: added.append((x, y)))
    view = ctocanvas.View(small(), Path("x.yaml"))
    click(view, 30, 10)
    assert added == [(30, 10)]

    labels = right_click(view, monkeypatch, "l", 30, 10)
    assert labels[:2] == ["● Draw boxes", "○ Draw lines"] and view.tool == "line"
    click(view, 30, 10)  # starts a line, the next click finishes it
    assert view.anchor == (30, 10)
    click(view, 40, 10)
    assert view.anchor is None and (view.canvas.lines[-1].x2, view.canvas.lines[-1].y2) == (40, 10)
    click(view, 50, 10)  # still the line tool
    assert view.anchor == (50, 10) and added == [(30, 10)]

    right_click(view, monkeypatch, "b", 0, 0)
    assert view.tool == "box"


def test_item_menu(monkeypatch):
    view = ctocanvas.View(small(), Path("x.yaml"))
    click(view, 2, 1)  # a click on a box selects it
    assert view.selected is view.canvas.boxes[0]
    labels = right_click(view, monkeypatch, "m", 2, 1)
    assert labels[2:5] == ["Edit text", "Move", "Delete"]
    click(view, 12, 11)
    assert (view.canvas.boxes[0].x, view.canvas.boxes[0].y) == (10, 10)

    right_click(view, monkeypatch, "d", 12, 11)
    assert len(view.canvas.boxes) == 1


def test_box_grows_while_typing_and_cancel_drops_it(monkeypatch):
    class Screen:
        def getmaxyx(self):
            return 30, 100

    view = ctocanvas.View(Canvas(), Path("x.yaml"))
    widths = []

    def fake_ask(screen, label, text="", redraw=None, cursor=None, click_keeps=False):
        for typed in ("a", "a longer text"):
            redraw(typed)
            widths.append(view.canvas.boxes[0].w)
        return None

    monkeypatch.setattr(ctocanvas, "ask", fake_ask)
    monkeypatch.setattr(ctocanvas, "draw", lambda screen, v: None)
    view.add_box(Screen(), 3, 4)
    assert widths == [5, 17]
    assert view.canvas.boxes == [] and not view.dirty


def test_pick_color_then_click_item_paints_it(tmp_path):
    view = ctocanvas.View(small(), tmp_path / "c.yaml")
    view.pick(ctocanvas.SWATCH * 3)  # blue
    view.press(1, 1)
    view.release(None, 1, 1)
    bg, fg = ctocanvas.PALETTE[3]
    assert (view.canvas.boxes[0].bg, view.canvas.boxes[0].fg) == (bg, fg)
    assert view.canvas.boxes[1].bg is None and view.dirty and view.selected is None
    view.pick(0)  # the clear swatch
    view.press(1, 1)
    view.release(None, 1, 1)
    assert view.canvas.boxes[0].bg is None and view.canvas.boxes[0].fg is None


def test_colors_round_trip_and_export(tmp_path):
    canvas = small()
    canvas.boxes[0].bg, canvas.boxes[0].fg, canvas.lines[0].fg = "#fde68a", "#78350f", "#ff0000"
    path = tmp_path / "c.yaml"
    ctocanvas.save(canvas, path)
    loaded = ctocanvas.load(path)
    assert (loaded.boxes[0].bg, loaded.boxes[0].fg, loaded.lines[0].fg) == ("#fde68a", "#78350f", "#ff0000")
    ctocanvas.render_svg(loaded, tmp_path / "c.svg")
    assert "#fde68a" in (tmp_path / "c.svg").read_text()
    ctocanvas.render_png(loaded, tmp_path / "c.png")
    path.write_text("boxes:\n- {x: 0, y: 0, text: a, bg: red}\n")
    with pytest.raises(ctocanvas.DataError):
        ctocanvas.load(path)
