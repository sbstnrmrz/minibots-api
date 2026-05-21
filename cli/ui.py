"""Terminal rendering — rich panels, tables, chat bubbles.

No network calls, no business logic.
"""

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()


def print_banner() -> None:
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Minibots CLI[/bold cyan]\n"
        "[dim]Interactive terminal chat client[/dim]",
        box=box.DOUBLE_EDGE,
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()


def print_bots_table(bots: list[dict]) -> None:
    table = Table(
        title="Available Bots",
        box=box.ROUNDED,
        border_style="cyan",
        show_lines=False,
        expand=False,
    )
    table.add_column("ID", style="dim", width=5, justify="right")
    table.add_column("Name", style="bold white")
    table.add_column("Type", style="cyan")
    table.add_column("Workflow", style="dim")
    for bot in bots:
        table.add_row(
            str(bot["id"]),
            bot["name"],
            bot.get("bot_type", "—"),
            str(bot.get("workflow_id") or "—"),
        )
    console.print(table)
    console.print()


def print_chats_table(chats: list[dict]) -> None:
    if not chats:
        console.print("[dim]No existing sessions.[/dim]\n")
        return
    table = Table(
        title="Resumable Sessions",
        box=box.ROUNDED,
        border_style="cyan",
        show_lines=True,
        expand=False,
    )
    table.add_column("Chat ID", style="dim", max_width=24)
    table.add_column("Bot", width=5, justify="right")
    table.add_column("Msgs", width=5, justify="right")
    table.add_column("Last message", style="dim", max_width=55)
    for chat in chats:
        table.add_row(
            chat["chat_id"],
            str(chat.get("bot_id", "?")),
            str(chat.get("message_count", 0)),
            (chat.get("last_message") or "—")[:55],
        )
    console.print(table)
    console.print()


def print_history(messages: list[dict]) -> None:
    if not messages:
        return
    console.print(Rule("[dim]conversation history[/dim]", style="dim"))
    for m in messages:
        _print_bubble(m.get("role", "?"), m.get("content", ""))
    console.print(Rule(style="dim"))
    console.print()


def print_message(role: str, content: str) -> None:
    _print_bubble(role, content)


def _print_bubble(role: str, content: str) -> None:
    if role == "user":
        label = Text("You", style="bold green")
    else:
        label = Text("Agent", style="bold blue")
    console.print(label, end="")
    console.print(f": {content}")


def print_error(msg: str) -> None:
    console.print(f"[bold red]✗[/bold red] {msg}")


def print_info(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


def print_status(msg: str) -> None:
    console.print(msg, style="cyan")


def print_thinking() -> None:
    console.print("[dim]Agent is thinking…[/dim]", end="\r")


def clear_thinking() -> None:
    console.print(" " * 30, end="\r")
