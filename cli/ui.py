"""Terminal rendering — Claude Code aesthetic.

Uses rich for all output. No network calls, no business logic.
"""

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def print_banner(base_url: str = "") -> None:
    console.print()
    line = Text()
    line.append(" ✦ ", style="bold cyan")
    line.append("minibots", style="bold white")
    if base_url:
        line.append(f"  {base_url}", style="dim")
    console.print(line)
    console.print(
        "  [dim]Type [bold white]/help[/bold white] to see commands[/dim]"
    )
    console.print()


def print_session_header(bot_name: str, chat_id: str) -> None:
    console.print()
    console.print(Rule(
        f"[dim]{bot_name}[/dim]  [dim white]{chat_id[:16]}…[/dim white]",
        style="dim",
        align="left",
    ))
    console.print()


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


# ---------------------------------------------------------------------------
# Data tables
# ---------------------------------------------------------------------------

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

def print_history(messages: list[dict]) -> None:
    if not messages:
        console.print("[dim]  No messages in this session yet.[/dim]\n")
        return
    console.print()
    console.print(Rule("[dim]history[/dim]", style="dim"))
    console.print()
    for m in messages:
        _print_bubble(m.get("role", "?"), m.get("content", ""), dimmed=True)
    console.print(Rule(style="dim"))
    console.print()


def print_message(role: str, content: str) -> None:
    _print_bubble(role, content)


def _print_bubble(role: str, content: str, dimmed: bool = False) -> None:
    if role == "user":
        label = Text("You", style="bold green")
    else:
        label = Text("Agent", style="bold white")

    dim_open  = "[dim]" if dimmed else ""
    dim_close = "[/dim]" if dimmed else ""

    console.print(label)
    console.print(f"{dim_open}  {content}{dim_close}")
    console.print()


# ---------------------------------------------------------------------------
# Status / misc
# ---------------------------------------------------------------------------

def print_error(msg: str) -> None:
    console.print(f"  [red]✗[/red]  {msg}")


def print_info(msg: str) -> None:
    console.print(f"  [dim]{msg}[/dim]")


def print_status(msg: str) -> None:
    console.print(f"  {msg}")


def print_thinking() -> None:
    console.print("  [dim]…[/dim]", end="\r")


def clear_thinking() -> None:
    console.print("     ", end="\r")


def clear_screen() -> None:
    console.clear()
