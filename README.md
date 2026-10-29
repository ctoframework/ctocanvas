# CTOCanvas

Draw boxes, text and lines on an infinite canvas in the terminal. Scroll in any direction with the mouse or the keys, save the canvas to YAML, or export it as ASCII, PNG, or SVG.

## Install

    pip install -r requirements.txt

Requires Python 3 with `click`, `Pillow`, `PyYAML` and `svgwrite`. `pytest` is only needed to run the tests. A terminal with mouse support is needed for clicking (any xterm-compatible one).

## Data

The canvas lives on a grid of terminal cells. `x` grows to the right and `y` downwards; both can be negative.

```yaml
boxes:
  - {x: 2, y: 1, text: Idea}
  - {x: 24, y: 1, text: Plan}
lines:
  - {x1: 10, y1: 2, x2: 24, y2: 2}
```

A box is three rows high with its top-left corner at `x`, `y`; it is as wide as its single-line `text` plus four. A line runs horizontally from `x1`, `y1` to `x2`, then vertically to `y2`. Boxes cover lines, and crossing lines join into `┼ ├ ┬` and friends. Text can contain emoji; PNG export draws them with `NotoColorEmoji.ttf` if it is installed.

The loader rejects coordinates that are not integers and boxes without a non-empty single-line `text`. `examples/demo.yaml` is a full example (see `examples/demo.png`).

## Usage

    python ctocanvas.py [canvas.yaml] [--format {interactive,ascii,png,svg}] [-o OUTPUT]

| Format        | Result                                                           |
|---------------|------------------------------------------------------------------|
| `interactive` | Full-screen ncurses editor (default). Creates the file on save.  |
| `ascii`       | The canvas printed as text.                                      |
| `png`         | One image, `canvas/<name>.png`, cropped to the drawing.          |
| `svg`         | One vector image, `canvas/<name>.svg`.                           |

Keys and mouse in interactive mode:

| Input                    | Action                                                          |
|--------------------------|-----------------------------------------------------------------|
| Click                    | Draws with the current tool: a box on empty space (then type its text), or a line (click start, click end). Clicking an item selects it. |
| Right click              | Context menu: choose the tool (Draw boxes / Draw lines; it stays until you pick another), plus Edit text, Move, Delete on an item, or Undo, Save, Export, Go to origin on empty space. |
| (in the menu)            | `↑` `↓` `Enter`, the underlined letter, or the mouse to choose; `Esc` or a click outside to dismiss. |
| Box here                 | Draws a box and asks for its text; the box grows as you type. `Enter` keeps it, `Esc` cancels. |
| Draw lines               | Click where the line starts, then where it ends.                |
| Move                     | Then click the new position. Or just drag an item.              |
| Drag empty space         | Pan the canvas.                                                 |
| Wheel / `←` `→` `↑` `↓`  | Scroll. `Shift`+wheel scrolls sideways; `Shift`+arrow scrolls 10 cells. |
| `PgUp` `PgDn` / `Home`   | Scroll a page / back to the origin.                             |
| `e` `Enter`              | Edit the selected box (shortcut for the menu entry).            |
| `d` `Delete`             | Delete the selected item (shortcut).                            |
| `u`                      | Undo.                                                           |
| `s`                      | Save to the YAML file (shortcut).                                          |
| `x`                      | Export (shortcut): type a file name ending in `.txt`, `.png`, `.svg` or `.yaml`. |
| `Esc`                    | Cancel a pending line or move, or deselect.                                     |
| `q`                      | Quit (asks first if there are unsaved changes).                 |

When stdin or stdout is not a terminal, or `-o` is given, `interactive` falls back to the `ascii` output. `-o` sets the output file for every format.

    $ python ctocanvas.py examples/demo.yaml --format ascii
    ┌──────┐              ┌──────┐              ┌─────────┐
    │ Idea │──────────────│ Plan │──────────────│ Ship 🚀 │
    └──────┘              └──────┘              └─────────┘
          │                   │
    …

## Tests

    python -m pytest
