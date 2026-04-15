"""
src/agents/evaluation/claude_client.py

Async Anthropic interface for the LLM Evaluation Node.
Parses prompts via the Gatekeeper (LLMEvaluationResponse) and logs to the DB.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Dict, Any, Optional

import structlog
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.schemas.llm import (
    LLMEvaluationResponse,
    MarketCategory,
    ReflectionResponse,
    ReflectionVerdict,
    SentimentResponse,
)
from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.grok_client import GrokClient, NEUTRAL_SENTIMENT
from src.agents.execution.polymarket_client import PolymarketClient
from src.db.models import AgentDecisionLog
from src.db.repositories.decision_repo import DecisionRepository

logger = structlog.get_logger(__name__)

_GROK_ELIGIBLE: frozenset[MarketCategory] = frozenset(
    {
        MarketCategory.CRYPTO,
        MarketCategory.POLITICS,
    }
)

_CHAIN_BUDGET: float = (
    2.0  # shared wall-clock budget (seconds) across the evaluation chain
)
_CHAIN_BUDGET_DRY_RUN: float = 60.0  # relaxed budget for debugging / dry-run pipelines


class _DecimalSafeEncoder(json.JSONEncoder):
    """JSON encoder that round-trips Decimal values through float for JSON
    serialization.  The Decimal gate in ``ReflectionResponse`` guarantees
    precision was sanitised at parse time; this encoder makes the dict
    JSON-serialisable again for Gatekeeper ingestion."""

    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


_ROUTING_TABLE: Dict[MarketCategory, list[str]] = {
    MarketCategory.CRYPTO: [
        "btc",
        "bitcoin",
        "eth",
        "ethereum",
        "crypto",
        "token",
        "defi",
        "blockchain",
        "sol",
        "solana",
    ],
    MarketCategory.POLITICS: [
        "election",
        "president",
        "senate",
        "congress",
        "vote",
        "candidate",
        "party",
        "referendum",
        "governor",
        "minister",
    ],
    MarketCategory.SPORTS: [
        "nfl",
        "nba",
        "mlb",
        "nhl",
        "soccer",
        "football",
        "basketball",
        "baseball",
        "tennis",
        "ufc",
        "match",
        "game",
        "tournament",
    ],
}


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
        decision_repo_factory: Callable[
            [AsyncSession], DecisionRepository
        ] = DecisionRepository,
    ):
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.config = config
        self._db_factory = db_session_factory
        self._decision_repo_factory = decision_repo_factory
        self.client = AsyncAnthropic(
            api_key=self.config.anthropic_api_key.get_secret_value()
        )
        self._grok_client = GrokClient(
            api_key=self.config.grok_api_key,
            base_url=self.config.grok_base_url,
            model=self.config.grok_model,
            mocked=self.config.grok_mocked,
        )
        self._running = False
        self.model = self.config.anthropic_model

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
            item_fetched = False
            try:
                item = await self.in_queue.get()
                item_fetched = True
                await self._process_evaluation(item)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Unexpected error in Claude evaluation loop.", error=str(e)
                )
            finally:
                if item_fetched:
                    self.in_queue.task_done()

    async def _route_market(self, item: Dict[str, Any]) -> MarketCategory:
        """Layer 0: classify market into a domain category via keyword matching."""
        condition_id = item.get("condition_id", "")
        title = item.get("title", "")
        tags = " ".join(item.get("tags", []))
        text = f"{condition_id} {title} {tags}".lower()

        for category in [
            MarketCategory.CRYPTO,
            MarketCategory.POLITICS,
            MarketCategory.SPORTS,
        ]:
            if any(kw in text for kw in _ROUTING_TABLE[category]):
                return category
        return MarketCategory.GENERAL

    def _log_sentiment(
        self,
        *,
        status: str,
        reason: str,
        sentiment: SentimentResponse,
        snapshot_id: str,
    ) -> None:
        """Emit a normalized sentiment audit log entry."""
        logger.info(
            "grok_sentiment",
            status=status,
            reason=reason,
            sentiment_score=str(sentiment.sentiment_score),
            tweet_volume_delta=sentiment.tweet_volume_delta,
            top_narrative_summary=sentiment.top_narrative_summary,
            snapshot_id=snapshot_id,
        )

    async def _fetch_sentiment(
        self,
        category: MarketCategory,
        market_state: Dict[str, Any],
        snapshot_id: str,
    ) -> SentimentResponse:
        """Stage A: fetch sentiment from Grok for eligible categories.

        - CRYPTO / POLITICS -> call GrokClient (timeout handled internally).
        - SPORTS / GENERAL -> neutral fallback immediately (skip Grok).
        - Any failure -> neutral fallback; never stalls the pipeline.
        """
        if category not in _GROK_ELIGIBLE:
            self._log_sentiment(
                status="SKIPPED",
                reason="SKIPPED_CATEGORY",
                sentiment=NEUTRAL_SENTIMENT,
                snapshot_id=snapshot_id,
            )
            return NEUTRAL_SENTIMENT

        try:
            sentiment = await self._grok_client.analyze_sentiment(
                condition_id=market_state.get("condition_id", ""),
                market_title=market_state.get("title", ""),
                market_category=category,
                reference_timestamp_utc=str(market_state.get("timestamp", "")),
                tags=market_state.get("tags"),
            )
            self._log_sentiment(
                status="SUCCESS",
                reason="RECEIVED",
                sentiment=sentiment,
                snapshot_id=snapshot_id,
            )
            return sentiment

        except asyncio.TimeoutError:
            self._log_sentiment(
                status="ERROR",
                reason="TIMEOUT",
                sentiment=NEUTRAL_SENTIMENT,
                snapshot_id=snapshot_id,
            )
            return NEUTRAL_SENTIMENT

        except Exception as exc:
            reason = "SCHEMA_ERROR" if "validat" in str(exc).lower() else "HTTP_ERROR"
            self._log_sentiment(
                status="ERROR",
                reason=reason,
                sentiment=NEUTRAL_SENTIMENT,
                snapshot_id=snapshot_id,
            )
            return NEUTRAL_SENTIMENT

    async def _process_evaluation(self, item: Dict[str, Any]) -> None:
        t0 = time.monotonic()
        market_state = item.get("state", item)
        snapshot_id = item.get("snapshot_id", "local_test_no_id")

        # WI-14: Fresh market data fetch before evaluation
        yes_token_id = item.get("yes_token_id")
        if not yes_token_id:
            logger.warning(
                "Missing yes_token_id — non-tradable input, skipping evaluation.",
                snapshot_id=snapshot_id,
            )
            return

        try:
            pm_client = PolymarketClient(host=self.config.clob_rest_url)
            wi14_snapshot = await pm_client.fetch_order_book(yes_token_id)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(
                "WI-14 market data fetch error — conservative skip.",
                snapshot_id=snapshot_id,
                error=str(exc),
            )
            return

        if wi14_snapshot is None:
            logger.warning(
                "WI-14 market data unavailable — conservative skip.",
                snapshot_id=snapshot_id,
            )
            return
        snapshot_yes_token_id = str(wi14_snapshot.token_id)

        # Enrich market_state with fresh WI-14 pricing
        market_state["best_bid"] = wi14_snapshot.best_bid
        market_state["best_ask"] = wi14_snapshot.best_ask
        market_state["midpoint"] = wi14_snapshot.midpoint_probability
        market_state["spread"] = wi14_snapshot.spread

        # Stage 0: Route market category
        category = await self._route_market(market_state)

        # Stage A: Fetch sentiment
        sentiment = await self._fetch_sentiment(category, market_state, snapshot_id)

        # Stage B: Primary evaluation candidate (no Gatekeeper validation yet)
        prompt = PromptFactory.build_evaluation_prompt(
            market_state=market_state,
            category=category,
            sentiment=sentiment,
        )
        chain_budget = _CHAIN_BUDGET_DRY_RUN if self.config.dry_run else _CHAIN_BUDGET
        remaining = chain_budget - (time.monotonic() - t0)
        if remaining <= 0:
            logger.error(
                "Budget exhausted before primary evaluation.", snapshot_id=snapshot_id
            )
            return
        try:
            primary_result = await asyncio.wait_for(
                self._get_primary_candidate(prompt, snapshot_id),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Primary evaluation exceeded shared budget.",
                snapshot_id=snapshot_id,
                dry_run=self.config.dry_run,
            )
            return
        if not primary_result:
            logger.error(
                "Failed to obtain primary candidate after retries.",
                snapshot_id=snapshot_id,
            )
            return

        primary_raw_text, primary_json, token_usage = primary_result

        # Stage C: Reflection audit — strict remaining budget
        chain_budget = _CHAIN_BUDGET_DRY_RUN if self.config.dry_run else _CHAIN_BUDGET
        remaining_budget = chain_budget - (time.monotonic() - t0)
        reflection = await self._run_reflection_audit(
            primary_candidate_json=primary_json,
            market_state=market_state,
            sentiment=sentiment,
            snapshot_id=snapshot_id,
            budget=remaining_budget,
        )

        # Stage C→D: Apply reflection verdict to choose final candidate
        final_json = self._apply_reflection_verdict(reflection, primary_json)

        # Stage D: Terminal Gatekeeper validation
        try:
            eval_resp = LLMEvaluationResponse.model_validate_json(final_json)
        except ValidationError as e:
            logger.error(
                "Final candidate failed Gatekeeper validation.",
                snapshot_id=snapshot_id,
                reflection_verdict=reflection.verdict.value,
                errors=str(e),
            )
            return

        eval_resp = eval_resp.model_copy(
            update={
                "market_context": eval_resp.market_context.model_copy(
                    update={"yes_token_id": snapshot_yes_token_id}
                )
            }
        )

        # Build reflection audit envelope for persistence
        reflection_envelope = (
            f"[REFLECTION_AUDIT]{reflection.model_dump_json()}[/REFLECTION_AUDIT]"
        )
        enriched_raw = f"{reflection_envelope}\n{primary_raw_text}"

        # 1. Persistence
        await self._persist_decision(eval_resp, enriched_raw, token_usage, snapshot_id)

        # 2. Logging
        logger.info(
            "Evaluation complete (Gatekeeper Enforced)",
            snapshot_id=snapshot_id,
            market_category=category.value,
            action=eval_resp.recommended_action.value,
            expected_value=eval_resp.expected_value,
            position_size_pct=eval_resp.position_size_pct,
            approved=eval_resp.decision_boolean,
            reflection_verdict=reflection.verdict.value,
            reflection_flags=(
                reflection.bias_flags
                + reflection.consistency_flags
                + reflection.risk_flags
            ),
            reflection_reason=reflection.audit_note,
            reflection_latency_ms=reflection.latency_ms,
            input_tokens=token_usage["input"],
            output_tokens=token_usage["output"],
        )

        logger.info(
            "CLAUDE_DECISION",
            market=market_state.get("condition_id", "unknown"),
            action=eval_resp.recommended_action.value,
            confidence=eval_resp.confidence_score,
            expected_value=eval_resp.expected_value,
            approved=eval_resp.decision_boolean,
        )

        # 3. Routing
        if eval_resp.decision_boolean:
            logger.info(
                "Trade APPROVED by Gatekeeper. Enqueueing for Execution.",
                snapshot_id=snapshot_id,
            )
            await self.out_queue.put(
                {
                    "snapshot_id": snapshot_id,
                    "evaluation": eval_resp,
                    "yes_token_id": snapshot_yes_token_id,
                }
            )
        else:
            logger.info("Trade REJECTED/HOLD by Gatekeeper.", snapshot_id=snapshot_id)

    def _extract_json(self, text: str) -> str:
        """Attempts to cleanly extract a JSON object from markdown or raw text."""
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # Try to find the first '{' and last '}'
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1].strip()

        return text.strip()

    # ------------------------------------------------------------------
    # Stage B: Primary candidate retrieval (pre-Gatekeeper)
    # ------------------------------------------------------------------

    async def _get_primary_candidate(
        self,
        prompt: str,
        snapshot_id: str,
        max_retries: int = 2,
    ) -> Optional[tuple[str, str, Dict[str, int]]]:
        """Make primary LLM call and return (raw_text, candidate_json, token_usage).

        Retries on unparseable JSON but does NOT run Gatekeeper validation —
        that happens after reflection (Stage D).
        """
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(max_retries + 1):
            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    messages=messages,
                    temperature=0.0,
                )
                raw_content = resp.content[0].text
                json_str = self._extract_json(raw_content)

                # Verify parseable JSON (structural only, no Gatekeeper filters)
                json.loads(json_str)

                token_usage = {
                    "input": resp.usage.input_tokens,
                    "output": resp.usage.output_tokens,
                }
                return raw_content, json_str, token_usage

            except json.JSONDecodeError:
                logger.warning(
                    "Primary candidate JSON parse error. Re-prompting.",
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                if attempt < max_retries:
                    messages.append({"role": "assistant", "content": raw_content})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Your response was not valid JSON. Return ONLY a raw JSON object.",
                        }
                    )
                else:
                    return None
            except Exception as e:
                logger.error(
                    "Anthropic API Error.",
                    error=str(e),
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                if attempt == max_retries:
                    return None
                await asyncio.sleep(2.0**attempt)

        return None

    # ------------------------------------------------------------------
    # Stage C: Reflection Auditor
    # ------------------------------------------------------------------

    async def _run_reflection_audit(
        self,
        *,
        primary_candidate_json: str,
        market_state: Dict[str, Any],
        sentiment: SentimentResponse,
        snapshot_id: str,
        budget: float,
    ) -> ReflectionResponse:
        """Execute single-pass adversarial reflection on the primary candidate.

        Uses strict remaining shared budget. If budget is exhausted (<=0),
        the API call is skipped entirely and a conservative REJECTED verdict
        is returned immediately.
        """
        if budget <= 0:
            logger.warning(
                "Budget exhausted before reflection. Defaulting to REJECTED.",
                snapshot_id=snapshot_id,
            )
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note="BUDGET_EXHAUSTED",
                latency_ms=0,
            )

        prompt = PromptFactory.build_reflection_prompt(
            market_state=market_state,
            sentiment=sentiment,
            primary_candidate_json=primary_candidate_json,
            snapshot_id=snapshot_id,
        )

        try:
            resp = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                ),
                timeout=budget,
            )
            raw = resp.content[0].text
            json_str = self._extract_json(raw)
            reflection = ReflectionResponse.model_validate_json(json_str)

            # Loop guard: ADJUSTED with missing correction → downgrade to REJECTED
            if (
                reflection.verdict == ReflectionVerdict.ADJUSTED
                and reflection.corrected_candidate_json is None
            ):
                logger.warning(
                    "ADJUSTED verdict missing corrected_candidate_json — downgrading to REJECTED.",
                    snapshot_id=snapshot_id,
                )
                return ReflectionResponse(
                    verdict=ReflectionVerdict.REJECTED,
                    bias_flags=reflection.bias_flags,
                    consistency_flags=reflection.consistency_flags,
                    risk_flags=reflection.risk_flags,
                    audit_note="ADJUSTED_MISSING_PAYLOAD: downgraded to REJECTED",
                    latency_ms=reflection.latency_ms,
                )

            return reflection

        except asyncio.TimeoutError:
            logger.warning(
                "Reflection audit timed out (budget exhausted). Defaulting to REJECTED.",
                snapshot_id=snapshot_id,
                budget_s=round(budget, 3),
            )
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note="BUDGET_EXHAUSTED",
                latency_ms=int(budget * 1000),
            )

        except Exception as exc:
            logger.error(
                "Reflection audit error. Defaulting to REJECTED.",
                snapshot_id=snapshot_id,
                error=str(exc),
            )
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note=f"REFLECTION_ERROR: {exc}",
                latency_ms=0,
            )

    def _apply_reflection_verdict(
        self,
        reflection: ReflectionResponse,
        primary_candidate_json: str,
    ) -> str:
        """Choose the final candidate JSON based on the reflection verdict.

        - APPROVED  → pass original candidate unchanged.
        - ADJUSTED  → use corrected candidate (single-pass, no recursion).
        - REJECTED  → build conservative HOLD candidate.
        """
        if reflection.verdict == ReflectionVerdict.APPROVED:
            return primary_candidate_json

        if reflection.verdict == ReflectionVerdict.ADJUSTED:
            return json.dumps(
                reflection.corrected_candidate_json, cls=_DecimalSafeEncoder
            )

        # REJECTED → conservative HOLD
        return self._build_hold_candidate(primary_candidate_json)

    @staticmethod
    def _build_hold_candidate(primary_candidate_json: str) -> str:
        """Construct a conservative HOLD candidate from the primary candidate.

        Sets confidence to 0.0 so the Gatekeeper MIN_CONFIDENCE filter
        triggers, enforcing HOLD with position_size_pct=0.0.
        """
        candidate = json.loads(primary_candidate_json)
        candidate["decision_boolean"] = False
        candidate["recommended_action"] = "HOLD"
        candidate["confidence_score"] = 0.0
        return json.dumps(candidate)

    async def _evaluate_with_retries(
        self, prompt: str, snapshot_id: str, max_retries: int = 2
    ) -> Optional[tuple[LLMEvaluationResponse, str, Dict[str, int]]]:
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Calling Anthropic API...",
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    messages=messages,
                    temperature=0.0,
                )

                raw_content = resp.content[0].text
                json_str = self._extract_json(raw_content)

                try:
                    eval_response = LLMEvaluationResponse.model_validate_json(json_str)

                    token_usage = {
                        "input": resp.usage.input_tokens,
                        "output": resp.usage.output_tokens,
                    }

                    return eval_response, raw_content, token_usage

                except ValidationError as e:
                    logger.warning(
                        "JSON Validation Error. Re-prompting Claude...",
                        attempt=attempt + 1,
                        snapshot_id=snapshot_id,
                    )
                    if attempt < max_retries:
                        messages.append({"role": "assistant", "content": raw_content})
                        fix_prompt = f"Your response failed strict Pydantic JSON schema validation. Fix these errors:\n{str(e)}\n\nReturn ONLY the corrected JSON."
                        messages.append({"role": "user", "content": fix_prompt})
                    else:
                        logger.error(
                            "Max retries exceeded for JSON validation.",
                            snapshot_id=snapshot_id,
                            errors=str(e),
                        )
                        return None

            except Exception as e:
                logger.error(
                    "Anthropic API Error.",
                    error=str(e),
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                if attempt == max_retries:
                    return None
                await asyncio.sleep(2.0**attempt)

        return None

    async def _persist_decision(
        self,
        eval_resp: LLMEvaluationResponse,
        raw_text: str,
        token_usage: Dict[str, int],
        snapshot_id: str,
    ) -> None:
        """Persists the full audit trail and Gatekeeper invariants into SQLite."""
        if self._db_factory is None:
            logger.error(
                "No db_session_factory configured — cannot persist decision.",
                snapshot_id=snapshot_id,
            )
            return
        try:
            async with self._db_factory() as session:
                repo = self._decision_repo_factory(session)
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
                    output_tokens=token_usage["output"],
                )
                await repo.insert_decision(decision_log)
                await session.commit()
        except Exception as e:
            logger.error(
                "Failed to persist AgentDecisionLog to database.",
                error=str(e),
                snapshot_id=snapshot_id,
            )
