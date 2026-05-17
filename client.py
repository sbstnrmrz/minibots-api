import asyncio
import sys
import uuid
import json
import websockets

SERVER_URL = "ws://localhost:8000/ws/chat"


async def main(bot_id: int | None):
    chat_id = str(uuid.uuid4())

    async with websockets.connect(SERVER_URL) as ws:
        print(f"Conectado. bot_id={bot_id}  chat_id={chat_id}")
        print("Escribe tu mensaje (Ctrl+C para salir).\n")

        loop = asyncio.get_event_loop()

        while True:
            text = await loop.run_in_executor(None, lambda: input("> "))
            if not text.strip():
                continue

            payload = {"message": text}
            if bot_id:
                payload["bot_id"] = bot_id
                payload["chat_id"] = chat_id

            await ws.send(json.dumps(payload))

            raw = await ws.recv()
            data = json.loads(raw)
            print(f"\nBot: {data.get('response', data)}\n")


if __name__ == "__main__":
    bot_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    try:
        asyncio.run(main(bot_id))
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
