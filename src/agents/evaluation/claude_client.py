"""
src/agents/evaluation/claude_client.py

Async Anthropic interface for the LLM Evaluation Node.
Parses prompts via the Gatekeeper (LLMEvaluationResponse) and logs to the DB.
"""

import asyncio
import re
from typing import Dict, Any, Optional

import structlog
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.schemas.llm import LLMEvaluationResponse
from src.db.models import AgentDecisionLog
from src.db.repositories.decision_repo import DecisionRepository

logger = structlog.get_logger(__name__)

class ClaudeClient:
    """
    LLM Evaluation Node.
    Consumes evaluation prompts, queries Claude, enforces strict JSON validation via Pydantic,
    persists the reasoning audit trail to the DB, and forwards approved trades.
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[Dict[str, Any]],
        out_queue: asyncio.Queue[Dict[str, Any]],
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession] | None = None,
    ):
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.config = config
        self._db_factory = db_session_factory
        self.client = AsyncAnthropic(api_key=self.config.anthropic_api_key.get_secret_value())
        self._running = False
        self.model = "claude-3-5-sonnet-latest"
        
    async def start(self) -> None:
        """Starts the evaluation loop."""
        self._running = True
        logger.info("Starting Claude Evaluation Node...", model=self.model)
        
        # Start the background consumption loop
        task = asyncio.create_task(self._consume_queue())
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            task.cancel()
                
    async def stop(self) -> None:
        """Gracefully stops the client."""
        logger.info("Stopping Claude Evaluation Node...")
        self._running = False
        
    async def _consume_queue(self) -> None:
        while self._running:
            try:
                item = await self.in_queue.get()
                await self._process_evaluation(item)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in Claude evaluation loop.", error=str(e))
            finally:
                self.in_queue.task_done()

    async def _process_evaluation(self, item: Dict[str, Any]) -> None:
        prompt = item.get("prompt")
        snapshot_id = item.get("snapshot_id", "local_test_no_id")
        
        if not prompt:
            logger.warning("Empty prompt received. Skipping.")
            return

        result = await self._evaluate_with_retries(prompt, snapshot_id)
        if not result:
            logger.error("Failed to obtain valid LLM evaluation after retries.", snapshot_id=snapshot_id)
            return
            
        eval_resp, raw_text, token_usage = result
        
        # 1. Persistence
        await self._persist_decision(eval_resp, raw_text, token_usage, snapshot_id)
        
        # 2. Logging
        logger.info(
            "Evaluation complete (Gatekeeper Enforced)",
            snapshot_id=snapshot_id,
            action=eval_resp.recommended_action.value,
            expected_value=eval_resp.expected_value,
            position_size_pct=eval_resp.position_size_pct,
            approved=eval_resp.decision_boolean,
            input_tokens=token_usage["input"],
            output_tokens=token_usage["output"]
        )
        
        # 3. Routing
        if eval_resp.decision_boolean:
            logger.info("Trade APPROVED by Gatekeeper. Enqueueing for Execution.", snapshot_id=snapshot_id)
            await self.out_queue.put({
                "snapshot_id": snapshot_id,
                "evaluation": eval_resp
            })
        else:
            logger.info("Trade REJECTED/HOLD by Gatekeeper.", snapshot_id=snapshot_id)

    def _extract_json(self, text: str) -> str:
        """Attempts to cleanly extract a JSON object from markdown or raw text."""
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
            
        # Try to find the first '{' and last '}'
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1].strip()
            
        return text.strip()

    async def _evaluate_with_retries(
        self, 
        prompt: str, 
        snapshot_id: str, 
        max_retries: int = 2
    ) -> Optional[tuple[LLMEvaluationResponse, str, Dict[str, int]]]:
        messages = [{"role": "user", "content": prompt}]
        
        for attempt in range(max_retries + 1):
            try:
                logger.debug("Calling Anthropic API...", attempt=attempt+1, snapshot_id=snapshot_id)
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    messages=messages,
                    temperature=0.0
                )
                
                raw_content = resp.content[0].text
                json_str = self._extract_json(raw_content)
                
                try:
                    eval_response = LLMEvaluationResponse.model_validate_json(json_str)
                    
                    token_usage = {
                        "input": resp.usage.input_tokens,
                        "output": resp.usage.output_tokens
                    }
                    
                    return eval_response, raw_content, token_usage
                    
                except ValidationError as e:
                    logger.warning("JSON Validation Error. Re-prompting Claude...", attempt=attempt+1, snapshot_id=snapshot_id)
                    if attempt < max_retries:
                        messages.append({"role": "assistant", "content": raw_content})
                        fix_prompt = f"Your response failed strict Pydantic JSON schema validation. Fix these errors:\n{str(e)}\n\nReturn ONLY the corrected JSON."
                        messages.append({"role": "user", "content": fix_prompt})
                    else:
                        logger.error("Max retries exceeded for JSON validation.", snapshot_id=snapshot_id, errors=str(e))
                        return None
                        
            except Exception as e:
                logger.error("Anthropic API Error.", error=str(e), attempt=attempt+1, snapshot_id=snapshot_id)
                if attempt == max_retries:
                    return None
                await asyncio.sleep(2.0 ** attempt)
                
        return None

    async def _persist_decision(
        self,
        eval_resp: LLMEvaluationResponse,
        raw_text: str,
        token_usage: Dict[str, int],
        snapshot_id: str
    ) -> None:
        """Persists the full audit trail and Gatekeeper invariants into SQLite."""
        if self._db_factory is None:
            logger.error("No db_session_factory configured — cannot persist decision.", snapshot_id=snapshot_id)
            return
        try:
            async with self._db_factory() as session:
                repo = DecisionRepository(session)
                decision_log = AgentDecisionLog(
                    snapshot_id=snapshot_id,
                    confidence_score=eval_resp.confidence_score,
                    expected_value=eval_resp.expected_value,
                    decision_boolean=eval_resp.decision_boolean,
                    recommended_action=eval_resp.recommended_action.value,
                    implied_probability=eval_resp.probabilistic_estimate.p_true,
                    reasoning_log=raw_text,
                    prompt_version="v1.0.0",
                    llm_model_id=self.model,
                    input_tokens=token_usage["input"],
                    output_tokens=token_usage["output"]
                )
                await repo.insert_decision(decision_log)
                await session.commit()
        except Exception as e:
            logger.error("Failed to persist AgentDecisionLog to database.", error=str(e), snapshot_id=snapshot_id)
