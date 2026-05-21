"""Slash-command completer for the chat input prompt.

Only activates when the input starts with '/'.
Yields completions with a short description shown in the dropdown meta column.
"""

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

COMMANDS: list[dict] = [
    {"name": "help",    "description": "Show available commands"},
    {"name": "resume",  "description": "Switch to another chat session"},
    {"name": "new",     "description": "Start a new chat with a different bot"},
    {"name": "history", "description": "Reload and display current conversation"},
    {"name": "bots",    "description": "List all available bots"},
    {"name": "clear",   "description": "Clear the screen"},
    {"name": "quit",    "description": "Exit the CLI"},
]


class SlashCompleter(Completer):
    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        prefix = text[1:].lower()
        for cmd in COMMANDS:
            if cmd["name"].startswith(prefix):
                yield Completion(
                    "/" + cmd["name"],
                    start_position=-len(text),
                    display="/" + cmd["name"],
                    display_meta=cmd["description"],
                )
