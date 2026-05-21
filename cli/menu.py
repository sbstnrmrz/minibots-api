"""prompt_toolkit interactive menus — Claude Code aesthetic.

Replaces questionary selects with Application-based inline menus
that match the design spec: white-bg header, light-blue active item,
dim category labels, nav-hint toolbar.
"""

from dataclasses import dataclass
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

_STYLE = Style.from_dict({
    "header":   "bg:white fg:black bold",
    "category": "fg:#555555",
    "selected": "fg:#87ceeb bold",
    "item":     "fg:#cccccc",
    "toolbar":  "bg:#1a1a1a fg:#555555",
})

_TOOLBAR_TEXT = " ↔ to switch  •  ↑/↓ to navigate  •  Enter to select  •  Esc to close "


@dataclass
class MenuItem:
    label: str
    value: Any
    category: str = ""


def run_menu(title: str, items: list[MenuItem]) -> Any | None:
    """Run an inline interactive menu. Returns the selected value, or None on cancel."""
    if not items:
        return None

    idx = [0]
    result = [None]

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        idx[0] = (idx[0] - 1) % len(items)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        idx[0] = (idx[0] + 1) % len(items)
        event.app.invalidate()

    @kb.add("enter")
    def _select(event):
        result[0] = items[idx[0]].value
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit()

    def _render() -> FormattedText:
        lines: StyleAndTextTuples = []
        lines += [("class:header", f" {title} "), ("", "\n\n")]
        prev_cat = None
        for i, item in enumerate(items):
            if item.category and item.category != prev_cat:
                prev_cat = item.category
                lines.append(("class:category", f"  {item.category}\n"))
            if i == idx[0]:
                lines.append(("class:selected", f"  ❯ {item.label}\n"))
            else:
                lines.append(("class:item", f"    {item.label}\n"))
        lines.append(("", "\n"))
        return FormattedText(lines)

    def _toolbar() -> FormattedText:
        return FormattedText([("class:toolbar", _TOOLBAR_TEXT)])

    layout = Layout(HSplit([
        Window(FormattedTextControl(_render), dont_extend_height=True),
        Window(FormattedTextControl(_toolbar), height=1),
    ]))

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=_STYLE,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    app.run()
    return result[0]
