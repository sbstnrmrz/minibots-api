import asyncio
import json
import websockets

SERVER_URL = "ws://localhost:8000/socket.io/?EIO=4&transport=websocket"


async def main():
    async with websockets.connect(SERVER_URL) as ws:
        # Engine.IO handshake
        await ws.recv()  # open packet "0{...}"
        await ws.send("40")  # Socket.IO connect
        await ws.recv()  # connect ack "40{...}"

        print("Conectado. Escribe tu mensaje (Ctrl+C para salir).\n")
        loop = asyncio.get_event_loop()

        async def listen():
            async for raw in ws:
                if raw == "2":  # ping
                    await ws.send("3")  # pong
                elif raw.startswith("42"):
                    event, data = json.loads(raw[2:])
                    role = data.get("role", "")
                    content = data.get("content", "")
                    if event == "new_message" and role == "agent":
                        print(f"\nBot: {content}\n> ", end="", flush=True)
                    elif event == "error":
                        print(f"\nError: {data.get('detail')}\n> ", end="", flush=True)

        asyncio.create_task(listen())

        while True:
            text = await loop.run_in_executor(None, input, "> ")
            if not text.strip():
                continue
            payload = "42" + json.dumps(["send_message", {"content": text, "role": "user"}])
            await ws.send(payload)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
