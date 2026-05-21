import dataclasses
import logging
from typing import Any

from app.agents.base import Agent, AgentContext
from app.agents.memory import MemoryStore
from app.tools import get_tools_for_agent, make_dispatcher_for_agent
from llm import DEFAULT_LLM_CONFIG, call_llm

logger = logging.getLogger("scheduling_agent")

SCHEDULING_SYSTEM_PROMPT = """Role: You are a professional scheduling assistant. Your job is to collect the information needed and confirm a reservation for the user.

Required fields before booking:
- booker_name: the user's full name
- service: what they want to book (e.g. "haircut", "consultation", "meeting")
- date: the desired date (YYYY-MM-DD)
- start_time: the desired starting time as an ISO 8601 datetime with timezone
- duration_minutes: how long the reservation should last in minutes

Optional:
- booker_contact: phone number or email — ask once; do not insist if declined.

Execution flow — follow this exact sequence:

STEP 1 — COLLECT INFORMATION
Ask for any missing required field one at a time. Do not proceed until all five required fields are known. Check conversation history — never ask for information already provided.

STEP 2 — CHECK AVAILABILITY
Once all required fields are collected, call check_availability(start_time, duration_minutes).
- If available: present the slot to the user and ask for explicit confirmation before booking. Example: "I can book your [service] on [date] at [time] for [duration] minutes. Shall I confirm?"
- If unavailable: call recommend_slots(date, duration_minutes) and present the returned options. Ask the user to pick one or suggest a different day if the list is empty.

STEP 3 — BOOK ONLY ON EXPLICIT CONFIRMATION
Do NOT call book_reservation until the user explicitly says yes, confirms, or approves the slot. A vague reply ("maybe", "let me think") is not confirmation — keep waiting.

STEP 4 — CONFIRM THE BOOKING
After book_reservation succeeds, tell the user their booking is confirmed with the date, time, and service. If the status is "conflict", apologize and return to step 2.

Strict constraints:
- NO MARKDOWN: plain text only. Hyphens "-" for lists. No asterisks, no headings.
- LANGUAGE MATCH: respond in the same language the user writes in. Do not switch languages mid-conversation.
- NO FILLER: no "Great!", "Of course!", "Certainly!" as standalone responses. Get to the point.
- NO REPETITION: check conversation history before asking for something already provided.
- HONESTY: if recommend_slots returns an empty list, say so clearly and ask for a different day."""

_DEFAULT_TOOL_NAMES = ["check_availability", "recommend_slots", "book_reservation"]


class SchedulingAgent(Agent):
    def __init__(
        self,
        system_prompt: str = SCHEDULING_SYSTEM_PROMPT,
        session_id: str | None = None,
        tool_names: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(tool_names if tool_names is not None else _DEFAULT_TOOL_NAMES)
        self._system_prompt = system_prompt
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._memory = MemoryStore()

    def run(self, ctx: AgentContext) -> AgentContext:
        session_id = ctx.chat_id or self._session_id

        history_block = ""
        if session_id:
            history = self._memory.load(session_id, self.name)
            if history:
                turns = "\n".join(
                    f"{m['role'].capitalize()}: {m['content']}" for m in history
                )
                history_block = f"<conversation_history>\n{turns}\n</conversation_history>\n\n"

        user_message = f"{history_block}User: {ctx.input}"
        config = dataclasses.replace(DEFAULT_LLM_CONFIG, system_prompt=self._system_prompt)

        tools = get_tools_for_agent(self._tool_names)
        base_dispatcher = make_dispatcher_for_agent(self._tool_names)

        # Inject chat_id and tenant_id into book_reservation args; the LLM
        # does not know these values and must not be asked to supply them.
        _chat_id = ctx.chat_id
        _tenant_id = self._tenant_id

        def dispatcher(name: str, args: dict[str, Any]) -> Any:
            if name == "book_reservation":
                args = {**args, "chat_id": _chat_id, "tenant_id": _tenant_id}
            return base_dispatcher(name, args)

        reply = call_llm(
            config,
            [{"role": "user", "content": user_message}],
            tools=tools or None,
            dispatcher=dispatcher if tools else None,
        )

        if session_id:
            self._memory.save(session_id, self.name, "user", ctx.input)
            self._memory.save(session_id, self.name, "assistant", reply)

        return dataclasses.replace(ctx, input=reply, retrieval_query=None)
