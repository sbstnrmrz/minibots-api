"""Minibots CLI — interactive terminal client.

Usage:
    uv run python -m cli.main

Config (.env or env vars):
    API_BASE_URL   Server base URL  (default: http://localhost:8000)
    API_TOKEN      Shared API token (must match backend API_TOKEN)
"""

import os
import uuid

import questionary
from dotenv import load_dotenv
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from cli.client import APIClient, APIError, SocketClient
from cli.completer import SlashCompleter
from cli import ui

load_dotenv()

# ---------------------------------------------------------------------------
# prompt_toolkit style — dark, minimal, Claude Code-ish
# ---------------------------------------------------------------------------

_PT_STYLE = Style.from_dict({
    "prompt": "#666666",
    # dropdown menu
    "completion-menu":                    "bg:#1a1a1a",
    "completion-menu.completion":         "bg:#1a1a1a #cccccc",
    "completion-menu.completion.current": "bg:#264f78 #ffffff bold",
    "completion-menu.meta.completion":         "bg:#1a1a1a #555555",
    "completion-menu.meta.completion.current": "bg:#264f78 #888888",
    "scrollbar.background":  "bg:#1a1a1a",
    "scrollbar.button":      "bg:#444444",
})

_COMPLETER = SlashCompleter()


def _read_input() -> str:
    """Read one line from the user via prompt_toolkit (slash autocomplete active)."""
    return pt_prompt(
        HTML("<ansibrightblack>> </ansibrightblack>"),
        completer=_COMPLETER,
        complete_while_typing=True,
        style=_PT_STYLE,
        reserve_space_for_menu=4,
    ).strip()


# ---------------------------------------------------------------------------
# Menu helpers
# ---------------------------------------------------------------------------

def _menu_select_bot(api: APIClient) -> dict:
    try:
        bots = api.list_bots()
    except APIError as e:
        ui.print_error(str(e))
        raise SystemExit(1)
    if not bots:
        ui.print_error("No bots found. Seed one via setup_test.py or POST /bots.")
        raise SystemExit(1)

    ui.print_bots_table(bots)
    choices = [
        questionary.Choice(
            title=f"{b['name']}  [{b.get('bot_type', '?')}]  id={b['id']}",
            value=b,
        )
        for b in bots
    ]
    bot = questionary.select("Select a bot:", choices=choices).ask()
    if bot is None:
        raise KeyboardInterrupt
    return bot


def _menu_select_chat(api: APIClient) -> dict | None:
    try:
        chats = api.list_chats()
    except APIError as e:
        ui.print_error(str(e))
        return None

    if not chats:
        ui.print_info("No existing sessions found.")
        return None

    ui.print_chats_table(chats)

    def _label(c: dict) -> str:
        snippet = (c.get("last_message") or "")[:42]
        return (
            f"bot={c['bot_id']}  "
            f"{c['chat_id'][:18]}…  "
            f"({c.get('message_count', 0)} msgs)  \"{snippet}\""
        )

    choices = [questionary.Choice(title=_label(c), value=c) for c in chats]
    choices.append(questionary.Choice(title="← Back", value=None))
    return questionary.select("Select a session to resume:", choices=choices).ask()


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------

def _run_chat(
    api: APIClient,
    sio: SocketClient,
    bot: dict,
    chat_id: str,
    history: list[dict],
) -> str:
    """Interactive chat loop. Returns 'quit' | 'resume' | 'new'."""
    ui.print_session_header(bot["name"], chat_id)

    if history:
        ui.print_history(history)

    bot_id = bot["id"]

    while True:
        try:
            user_input = _read_input()
        except (EOFError, KeyboardInterrupt):
            return "quit"

        if not user_input:
            continue

        cmd = user_input.lower()

        # ── commands ──────────────────────────────────────────────────────
        if cmd == "/help":
            ui.print_help()
            continue

        if cmd == "/quit" or cmd == "/exit":
            return "quit"

        if cmd == "/resume":
            return "resume"

        if cmd == "/new":
            return "new"

        if cmd == "/clear":
            ui.clear_screen()
            ui.print_session_header(bot["name"], chat_id)
            continue

        if cmd == "/bots":
            try:
                ui.print_bots_table(api.list_bots())
            except APIError as e:
                ui.print_error(e.detail)
            continue

        if cmd == "/history":
            try:
                data = api.get_chat_history(chat_id)
                ui.print_history(data.get("messages", []))
            except APIError as e:
                ui.print_error(e.detail)
            continue

        if cmd.startswith("/"):
            ui.print_error(f"Unknown command {cmd!r}  —  type [bold]/help[/bold] for the list.")
            continue

        # ── send message ───────────────────────────────────────────────────
        ui.print_thinking()
        try:
            sio.send(content=user_input, bot_id=bot_id, chat_id=chat_id)
            role, content = sio.receive_reply(timeout=90)
            ui.clear_thinking()
            ui.print_message(role, content)
        except TimeoutError as e:
            ui.clear_thinking()
            ui.print_error(str(e))
        except APIError as e:
            ui.clear_thinking()
            ui.print_error(e.detail)
        except Exception as e:
            ui.clear_thinking()
            ui.print_error(f"Unexpected error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()
    base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
    token    = os.getenv("API_TOKEN", "")

    ui.print_banner(base_url)

    # Health check
    api = APIClient(base_url=base_url, token=token)
    if not api.health():
        ui.print_error(
            f"Server unreachable at {base_url}\n"
            "  Start it with:  [bold]uv run fastapi dev app/main.py[/bold]"
        )
        raise SystemExit(1)
    ui.print_info(f"Connected  ·  {base_url}")

    # Socket
    sio = SocketClient(base_url=base_url, token=token)
    try:
        sio.connect()
        ui.print_info("Socket ready")
    except Exception as e:
        ui.print_error(f"Socket connection failed: {e}")
        api.close()
        raise SystemExit(1)

    ui.console.print()

    try:
        action = "menu"   # start at main menu
        last_new = False  # coming from /new means skip straight to bot select

        while True:
            # ── main menu ─────────────────────────────────────────────────
            if action == "menu":
                choice = questionary.select(
                    "What would you like to do?",
                    choices=[
                        questionary.Choice("💬  New Chat",       value="new"),
                        questionary.Choice("🔁  Resume Session", value="resume"),
                        questionary.Choice("🚪  Quit",           value="quit"),
                    ],
                    use_shortcuts=True,
                ).ask()
                if choice is None or choice == "quit":
                    break
                action = choice

            # ── new chat ──────────────────────────────────────────────────
            if action == "new":
                try:
                    bot = _menu_select_bot(api)
                except (KeyboardInterrupt, SystemExit):
                    action = "menu"
                    continue
                action = _run_chat(api, sio, bot, str(uuid.uuid4()), [])
                continue

            # ── resume session ────────────────────────────────────────────
            if action == "resume":
                chat = _menu_select_chat(api)
                if chat is None:
                    action = "menu"
                    continue
                try:
                    data = api.get_chat_history(chat["chat_id"])
                    history = data.get("messages", [])
                    bots = api.list_bots()
                    bot = next((b for b in bots if b["id"] == chat["bot_id"]), None)
                except APIError as e:
                    ui.print_error(str(e))
                    action = "menu"
                    continue
                if bot is None:
                    ui.print_error(f"Bot {chat['bot_id']} not found — may have been deleted.")
                    action = "menu"
                    continue
                action = _run_chat(api, sio, bot, chat["chat_id"], history)
                continue

            # ── quit ──────────────────────────────────────────────────────
            if action == "quit":
                break

            # fallback (e.g. /help returned nothing useful)
            action = "menu"

    except KeyboardInterrupt:
        pass
    finally:
        sio.disconnect()
        api.close()
        ui.console.print()
        ui.print_info("Goodbye.")


if __name__ == "__main__":
    main()
