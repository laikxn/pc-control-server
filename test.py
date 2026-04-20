import asyncio
import websockets

async def test():
    uri = "ws://192.168.1.204:8000"

    try:
        async with websockets.connect(uri) as ws:
            print("CONNECTED!")
    except Exception as e:
        print("FAILED:", e)

asyncio.run(test())