import asyncio
import json
import websockets

SERVER_URL = "ws://localhost:8000/ws/chat"


async def main():
    async with websockets.connect(SERVER_URL) as ws:
        print("Conectado. Escribe tu mensaje (Ctrl+C para salir).\n")
        loop = asyncio.get_event_loop()

        while True:
            text = await loop.run_in_executor(None, input, "> ")
            if not text.strip():
                continue

            await ws.send(json.dumps({"message": text}))
            data = json.loads(await ws.recv())
            print(f"\nBot: {data['response']}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
