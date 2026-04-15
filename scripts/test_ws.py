import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import structlog
from src.agents.ingestion.ws_client import AsyncWebSocketClient

structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
logger = structlog.get_logger()


async def consumer_mock(queue: asyncio.Queue):
    while True:
        msg = await queue.get()
        best_bid = msg.bids[0].price if msg.bids else "N/A"
        total_asks = len(msg.asks) if msg.asks else 0
        logger.info(
            "new_tick_received",
            event=msg.event,
            best_bid=best_bid,
            total_asks=total_asks,
        )
        queue.task_done()


async def main():
    # Use condition_id as expected by ws_client.py
    TEST_CONDITION_ID = (
        "0x2173a110cb1ba3c7b39912066c07dd82a4664b5953dd4305bc8c3e03cd530e8c"
    )

    queue = asyncio.Queue()
    ws_client = AsyncWebSocketClient(queue=queue, condition_id=TEST_CONDITION_ID)

    logger.info("starting_test", condition_id=TEST_CONDITION_ID)

    client_task = asyncio.create_task(ws_client.start())
    consumer_task = asyncio.create_task(consumer_mock(queue))

    try:
        await asyncio.sleep(15)
    finally:
        logger.info("shutting_down")
        await ws_client.stop()
        client_task.cancel()
        consumer_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
