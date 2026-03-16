#!/usr/bin/env python3
"""
src/orchestrator.py

Main entry point and orchestrator for poly-oracle-agent.
Wires together Ingestion, Context, Evaluation, and Execution nodes
using thread-safe asyncio queues.
"""

import asyncio
import sys

import structlog
from dotenv import load_dotenv

# Ensure .env is explicitly loaded if running from root
load_dotenv()

from src.core.config import AppConfig
from src.agents.ingestion.ws_client import AsyncWebSocketClient
from src.agents.context.aggregator import DataAggregator
from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.execution.signer import TransactionSigner
from src.agents.execution.broadcaster import TxBroadcaster
from src.db.engine import engine

# Configure structlog for the root execution
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ]
)

logger = structlog.get_logger(__name__)

async def main() -> None:
    """Main orchestration loop."""
    logger.info("Initializing Poly-Oracle-Agent Orchestrator...")

    # 0. Load Configuration
    try:
        config = AppConfig()
    except Exception as e:
        logger.error("Configuration validation failed.", error=str(e))
        sys.exit(1)
        
    # Valid testing asset on Polymarket
    asset_id = "0x2173a110cb1ba3c7b39912066c07dd82a4664b5953dd4305bc8c3e03cd530e8c"

    # 1. Initialize Asyncio Queues
    # Bridging mechanism between isolated agent components
    market_queue: asyncio.Queue = asyncio.Queue()     # ws_client -> aggregator
    prompt_queue: asyncio.Queue = asyncio.Queue()     # aggregator -> claude_client
    execution_queue: asyncio.Queue = asyncio.Queue()  # claude_client -> broadcaster

    logger.info("Queues instantiated. Bridging modules...")

    # 2. Instantiate Modules (The 4 Layers of Architecture)
    
    # Layer 1: Ingestion
    ws_client = AsyncWebSocketClient(queue=market_queue, condition_id=asset_id)
    
    # Layer 2: Context
    aggregator = DataAggregator(
        input_queue=market_queue, 
        output_queue=prompt_queue, 
        condition_id=asset_id
    )
    
    # Layer 3: Evaluation (The Brain & Gatekeeper)
    claude_client = ClaudeClient(
        in_queue=prompt_queue,
        out_queue=execution_queue,
        config=config
    )
    
    # Layer 4: Execution
    signer = TransactionSigner(config=config)
    broadcaster = TxBroadcaster(in_queue=execution_queue, signer=signer)

    logger.info("Layer 1 (Ingestion), Layer 2 (Context), Layer 3 (Evaluation), and Layer 4 (Execution) READY.")

    # 3. Spin up concurrent background loops
    tasks = [
        asyncio.create_task(ws_client.start(), name="WsClientTask"),
        asyncio.create_task(aggregator.start(), name="AggregatorTask"),
        asyncio.create_task(claude_client.start(), name="ClaudeClientTask"),
        asyncio.create_task(broadcaster.start(), name="BroadcasterTask")
    ]

    # 4. Global Execution & Graceful Shutdown
    try:
        logger.info("Bot is running concurrently. Press Ctrl+C to shut down.")
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown signal received (CancelledError).")
    except KeyboardInterrupt:
        logger.info("Shutdown signal received (KeyboardInterrupt).")
    finally:
        logger.info("Initiating graceful shutdown sequence...")
        
        # Stop all running infinite loops inside the agents
        await ws_client.stop()
        await aggregator.stop()
        await claude_client.stop()
        await broadcaster.stop()
        
        # Cancel hanging tasks
        for task in tasks:
            if not task.done():
                task.cancel()
                
        # Wait a moment for tasks to wrap up cleanly
        await asyncio.sleep(1)
        
        # Clean up database connections and release file handles
        await engine.dispose()
        logger.info("Database connections closed.")
        logger.info("Graceful shutdown complete. Exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Quietly catch the outer keyboard interrupt too
