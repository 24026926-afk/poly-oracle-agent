import asyncio
import websockets
import json

async def test():
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    async with websockets.connect(uri) as ws:
        sub = {"assets": ["0x2173a110cb1ba3c7b39912066c07dd82a4664b5953dd4305bc8c3e03cd530e8c"], "type": "market"}
        await ws.send(json.dumps(sub))
        print("Subscribed. Waiting for messages...")
        try:
            for _ in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"Received: {msg[:200]}")
        except asyncio.TimeoutError:
            print("Timeout waiting for message.")

if __name__ == "__main__":
    asyncio.run(test())
