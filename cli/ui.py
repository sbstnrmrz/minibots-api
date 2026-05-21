"""Terminal rendering — Claude Code aesthetic.

All output goes through rich Console. Cursor manipulation for the
gray-bar user-message trick and thinking indicator uses sys.stdout
directly with ANSI escape codes.
"""

import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def print_banner(base_url: str = "") -> None:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=2)

    left = (
        "\n"
        "[bold white] ✦ minibots[/bold white]\n"
        "\n"
        f"[dim] {base_url or 'http://localhost:8000'}[/dim]\n"
        "[dim] Interactive terminal client[/dim]\n"
    )

    right = (
        "[bold #e88080]Getting started[/bold #e88080]\n"
        "Type [bold white]/help[/bold white] to see all available commands\n"
        "\n"
        "[bold #e88080]Commands[/bold #e88080]\n"
        "[dim]/new      Start a new chat\n"
        "/resume   Resume a previous session\n"
        "/bots     List all bots\n"
        "/history  Show conversation history\n"
        "/quit     Exit[/dim]"
    )

    grid.add_row(left, right)

    console.print(
        Panel(
            grid,
            title="[dim]minibots cli[/dim]",
            title_align="left",
            border_style="#cc4444",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print()


# ---------------------------------------------------------------------------
# Session header
# ---------------------------------------------------------------------------

def print_session_header(bot_name: str, chat_id: str) -> None:
    label = f"{bot_name}  ·  {chat_id[:8]}…"
    width = console.width
    dashes = "─" * max(0, width - len(label) - 2)
    console.print(f"[dim]{label} {dashes}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# Help / tables
# ---------------------------------------------------------------------------

def print_help() -> None:
    console.print()
    console.print("  [bold white]Commands[/bold white]")
    console.print()
    rows = [
        ("/help",    "Show this list"),
        ("/resume",  "Switch to another chat session"),
        ("/new",     "Start a new chat with a different bot"),
        ("/history", "Reload and display current conversation"),
        ("/bots",    "List all available bots"),
        ("/clear",   "Clear the screen"),
        ("/quit",    "Exit"),
    ]
    for name, desc in rows:
        console.print(f"  [bold cyan]{name:<12}[/bold cyan] [dim]{desc}[/dim]")
    console.print()


def print_bots_table(bots: list[dict]) -> None:
    console.print()
    console.print("  [bold white]Bots[/bold white]")
    console.print()
    for bot in bots:
        wf = (
            f"  [dim]workflow {bot['workflow_id']}[/dim]"
            if bot.get("workflow_id") else ""
        )
        console.print(
            f"  [dim]{bot['id']:>3}[/dim]  "
            f"[bold]{bot['name']}[/bold]  "
            f"[dim cyan]{bot.get('bot_type', '?')}[/dim cyan]"
            f"{wf}"
        )
    console.print()


def print_chats_table(chats: list[dict]) -> None:
    if not chats:
        console.print("[dim]  No existing sessions.[/dim]\n")
        return
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="dim",
        border_style="dim",
        pad_edge=True,
        expand=False,
        show_edge=False,
    )
    table.add_column("Session", style="dim", max_width=22)
    table.add_column("Bot", width=5, justify="right")
    table.add_column("Msgs", width=5, justify="right")
    table.add_column("Last message", style="dim", max_width=52)
    for chat in chats:
        table.add_row(
            chat["chat_id"][:20] + "…",
            str(chat.get("bot_id", "?")),
            str(chat.get("message_count", 0)),
            (chat.get("last_message") or "—")[:52],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def print_user_message(text: str, replace_line: bool = True) -> None:
    """Print user message as a full-width gray bar.

    replace_line=True: move cursor up one line and overwrite the
    prompt_toolkit echo with the styled version (used after pt_prompt).
    replace_line=False: print directly (used for history replay).
    """
    if replace_line:
        # Go up one line (where prompt_toolkit left the echo) and clear it.
        sys.stdout.write("\033[A\033[2K\r")
        sys.stdout.flush()
    line = f"> {text}"
    padding = " " * max(0, console.width - len(line))
    console.print(f"[bold]{line}[/bold]{padding}", style="on grey19", no_wrap=True)


def print_message(role: str, content: str) -> None:
    """Print an agent reply with ● bullet. User messages in history use the bar."""
    if role == "user":
        print_user_message(content, replace_line=False)
        console.print()
    else:
        console.print(f"[bold]●[/bold] {content}")
        console.print()


def print_history(messages: list[dict]) -> None:
    if not messages:
        console.print("[dim]  No messages in this session yet.[/dim]\n")
        return
    console.print()
    console.print(Rule("[dim]history[/dim]", style="dim"))
    console.print()
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "user":
            print_user_message(content, replace_line=False)
            console.print()
        else:
            console.print(f"[dim bold]●[/dim bold] [dim]{content}[/dim]")
            console.print()
    console.print(Rule(style="dim"))
    console.print()


# ---------------------------------------------------------------------------
# Thinking indicator
# ---------------------------------------------------------------------------

def start_thinking() -> None:
    """Print a blank separator then the thinking spinner on the same line."""
    console.print()
    sys.stdout.write("\033[38;5;240m✺ Thinking…\033[0m")
    sys.stdout.flush()


def stop_thinking() -> None:
    """Erase the thinking indicator line so the agent reply can replace it."""
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def print_elapsed(seconds: float) -> None:
    if seconds >= 2:
        console.print(f"[dim]✺ Responded in {seconds:.0f}s[/dim]")
        console.print()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def print_error(msg: str) -> None:
    console.print(f"  [red]✗[/red]  {msg}")


def print_info(msg: str) -> None:
    console.print(f"  [dim]{msg}[/dim]")


def print_status(msg: str) -> None:
    console.print(f"  {msg}")


def clear_screen() -> None:
    console.clear()
