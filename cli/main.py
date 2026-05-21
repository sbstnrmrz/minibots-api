"""Minibots CLI — interactive terminal client.

Usage:
    uv run python -m cli.main
    # or
    uv run python cli/main.py

Config (env vars or .env):
    API_BASE_URL  Server base URL (default: http://localhost:8000)
    API_TOKEN     Shared API token (matches the backend API_TOKEN)
"""

import os
import uuid

import questionary
from dotenv import load_dotenv

from cli.client import APIClient, APIError, SocketClient
from cli.ui import (
    clear_thinking,
    console,
    print_banner,
    print_bots_table,
    print_chats_table,
    print_error,
    print_history,
    print_info,
    print_message,
    print_status,
    print_thinking,
)

load_dotenv()

_COMMANDS_HELP = (
    "[dim]Commands: [bold]/resume[/bold] — pick another session  "
    "[bold]/quit[/bold] — exit[/dim]"
)


# ---------------------------------------------------------------------------
# Menu helpers
# ---------------------------------------------------------------------------

def _select_bot(api: APIClient) -> dict:
    try:
        bots = api.list_bots()
    except APIError as e:
        print_error(str(e))
        raise SystemExit(1)

    if not bots:
        print_error("No bots found. Seed one with setup_test.py or POST /bots.")
        raise SystemExit(1)

    print_bots_table(bots)
    choices = [
        questionary.Choice(
            title=f"{b['name']}  [dim]{b.get('bot_type', '?')}  id={b['id']}[/dim]",
            value=b,
        )
        for b in bots
    ]
    bot = questionary.select(
        "Select a bot for this session:",
        choices=choices,
        use_shortcuts=False,
    ).ask()

    if bot is None:
        raise KeyboardInterrupt
    return bot


def _select_chat(api: APIClient) -> dict | None:
    try:
        chats = api.list_chats()
    except APIError as e:
        print_error(str(e))
        return None

    if not chats:
        print_info("No existing sessions found.")
        return None

    print_chats_table(chats)

    def _label(c: dict) -> str:
        snippet = (c.get("last_message") or "")[:40]
        return (
            f"bot={c['bot_id']}  {c['chat_id'][:20]}…  "
            f"({c.get('message_count', 0)} msgs)  \"{snippet}\""
        )

    choices = [questionary.Choice(title=_label(c), value=c) for c in chats]
    choices.append(questionary.Choice(title="← Back to main menu", value=None))

    return questionary.select(
        "Select a session to resume:",
        choices=choices,
        use_shortcuts=False,
    ).ask()


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------

def _run_chat_loop(
    api: APIClient,
    sio: SocketClient,
    bot: dict,
    chat_id: str,
    history: list[dict],
) -> str:
    """Run the interactive chat loop. Returns 'resume' or 'quit'."""
    bot_name = bot["name"]
    bot_id = bot["id"]

    console.print()
    print_status(
        f"Chatting with [bold]{bot_name}[/bold]  "
        f"[dim]session {chat_id[:16]}…[/dim]"
    )
    console.print(_COMMANDS_HELP)
    console.print()

    if history:
        print_history(history)

    while True:
        try:
            user_input = console.input("[bold green]You[/bold green]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("/quit", "/exit", "quit", "exit"):
            return "quit"
        if cmd == "/resume":
            return "resume"

        print_thinking()
        try:
            sio.send(content=user_input, bot_id=bot_id, chat_id=chat_id)
            role, content = sio.receive_reply(timeout=90)
            clear_thinking()
            print_message(role, content)
        except TimeoutError as e:
            clear_thinking()
            print_error(str(e))
        except APIError as e:
            clear_thinking()
            print_error(e.detail)
        except Exception as e:
            clear_thinking()
            print_error(f"Unexpected error: {e}")

    return "quit"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()
    base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
    token = os.getenv("API_TOKEN", "")

    print_banner()

    # --- health check ---
    api = APIClient(base_url=base_url, token=token)
    if not api.health():
        print_error(
            f"Server unreachable at [bold]{base_url}[/bold].\n"
            "  Start it with: [bold]uv run fastapi dev app/main.py[/bold]"
        )
        raise SystemExit(1)
    print_info(f"Connected to {base_url}")

    # --- socket.io ---
    sio = SocketClient(base_url=base_url, token=token)
    try:
        sio.connect()
        print_info("Socket ready.")
    except Exception as e:
        print_error(f"Socket connection failed: {e}")
        api.close()
        raise SystemExit(1)

    console.print()

    try:
        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice("💬  New Chat", value="new"),
                    questionary.Choice("🔁  Resume Session", value="resume"),
                    questionary.Choice("🚪  Quit", value="quit"),
                ],
                use_shortcuts=True,
            ).ask()

            if action is None or action == "quit":
                break

            # ── New chat ────────────────────────────────────────────────
            if action == "new":
                try:
                    bot = _select_bot(api)
                except (KeyboardInterrupt, SystemExit):
                    continue
                chat_id = str(uuid.uuid4())
                result = _run_chat_loop(api, sio, bot, chat_id, [])
                if result == "quit":
                    break
                # result == "resume" → fall through to resume flow next iteration

            # ── Resume session ──────────────────────────────────────────
            elif action == "resume":
                chat = _select_chat(api)
                if chat is None:
                    continue
                try:
                    history_data = api.get_chat_history(chat["chat_id"])
                    messages = history_data.get("messages", [])
                except APIError as e:
                    print_error(f"Could not load history: {e.detail}")
                    continue

                # Resolve the bot for this chat
                try:
                    bots = api.list_bots()
                    bot = next((b for b in bots if b["id"] == chat["bot_id"]), None)
                except APIError as e:
                    print_error(str(e))
                    continue

                if bot is None:
                    print_error(f"Bot {chat['bot_id']} not found — it may have been deleted.")
                    continue

                result = _run_chat_loop(api, sio, bot, chat["chat_id"], messages)
                if result == "quit":
                    break

    except KeyboardInterrupt:
        pass
    finally:
        sio.disconnect()
        api.close()
        console.print()
        print_info("Goodbye.")


if __name__ == "__main__":
    main()
