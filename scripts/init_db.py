#!/usr/bin/env python3
"""
scripts/init_db.py

Utility script to initialize the physical database schema.
Creates all tables defined in src/db/models.py using the async engine.
"""

import sys
import os
import asyncio

# Add the project root to the Python path to allow absolute imports from src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.db.engine import engine
from src.db.models import Base

async def init_db() -> None:
    """
    Creates all tables in the physical database asynchronously.
    """
    print(f"Initializing database using engine: {engine.url}")
    
    async with engine.begin() as conn:
        # Create all tables (MarketSnapshot, AgentDecisionLog, ExecutionTx)
        await conn.run_sync(Base.metadata.create_all)
        
    print("Database tables created successfully.")
    
    # Dispose the engine to close connections cleanly
    await engine.dispose()

if __name__ == "__main__":
    try:
        asyncio.run(init_db())
    except Exception as e:
        print(f"Error initializing database: {e}")
        sys.exit(1)
