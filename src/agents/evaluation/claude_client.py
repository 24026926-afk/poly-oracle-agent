"""
src/agents/evaluation/claude_client.py

Async Anthropic interface for the LLM Evaluation Node.
Parses prompts via the Gatekeeper (LLMEvaluationResponse) and logs to the DB.
"""

import asyncio
import hashlib
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
    LLMProvider,
    LLMProviderName,
    LLMBudgetBlockReason,
    LLMProviderMetadata,
    LLMEvaluationResponse,
    MarketCategory,
    ReflectionResponse,
    ReflectionVerdict,
    SentimentResponse,
)
from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.grok_client import GrokClient, NEUTRAL_SENTIMENT
from src.agents.evaluation.llm_cost_guard import (
    LLMBudgetGuard,
    MarketCognitiveCircuitBreaker,
)
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


def _compute_grok_budget(config: Any) -> float:
    """Compute total Grok budget safely — guards against MagicMock configs."""
    try:
        timeout = float(config.grok_timeout_seconds)
        retries = int(config.grok_max_retries)
        raw = timeout * (retries + 1)
        chain_budget = (
            _CHAIN_BUDGET_DRY_RUN
            if bool(getattr(config, "dry_run", False))
            else _CHAIN_BUDGET
        )
        return min(raw, chain_budget * 0.7)
    except (TypeError, ValueError):
        return 4.0  # safe default


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
    MarketCategory.POLITICS: [
        "politics",
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
        "sports",
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
    MarketCategory.ESPORTS: [
        "esports",
        "esport",
        "league of legends",
        "lol",
        "dota",
        "counter-strike",
        "cs2",
        "valorant",
    ],
    MarketCategory.IRAN: [
        "iran",
        "tehran",
        "ayatollah",
        "irgc",
        "nuclear deal",
        "sanctions",
    ],
    MarketCategory.FINANCE: [
        "finance",
        "stock",
        "stocks",
        "equity",
        "fed",
        "rates",
        "bond",
        "treasury",
        "oil",
        "gold",
    ],
    MarketCategory.GEOPOLITICS: [
        "geopolitics",
        "war",
        "ceasefire",
        "sanctions",
        "nato",
        "united nations",
        "conflict",
        "diplomacy",
    ],
    MarketCategory.TECH: [
        "tech",
        "technology",
        "ai",
        "artificial intelligence",
        "apple",
        "google",
        "microsoft",
        "tesla",
        "chip",
        "semiconductor",
    ],
    MarketCategory.CULTURE: [
        "culture",
        "movie",
        "music",
        "album",
        "celebrity",
        "oscars",
        "grammys",
        "product launch",
    ],
    MarketCategory.ECONOMY: [
        "economy",
        "inflation",
        "cpi",
        "jobs report",
        "unemployment",
        "gdp",
        "recession",
    ],
    MarketCategory.WEATHER: [
        "weather",
        "rain",
        "snow",
        "hurricane",
        "temperature",
        "storm",
        "tornado",
    ],
    MarketCategory.MENTIONS: [
        "mentions",
        "mention",
        "tweet",
        "tweets",
        "trend",
        "trending",
    ],
    MarketCategory.ELECTIONS: [
        "election",
        "elections",
        "primary",
        "ballot",
        "poll",
        "polling",
        "electoral",
    ],
}


def _coerce_market_category(raw: object) -> MarketCategory | None:
    """Map raw Gamma/internal category values to MarketCategory."""
    if raw is None:
        return None
    if isinstance(raw, MarketCategory):
        return raw
    if not isinstance(raw, str):
        return None

    normalized = raw.strip().upper().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    try:
        return MarketCategory(normalized)
    except ValueError:
        try:
            return MarketCategory[normalized]
        except KeyError:
            return None


def _hash_market_key(market_key: str) -> str:
    """Hash a market key to a short, low-cardinality identifier for logs.

    Prevents raw condition_id exposure in log output per WI-52."""
    return hashlib.sha256(market_key.encode()).hexdigest()[:8]


def _keyword_matches(text: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


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
        metrics: Any = None,
    ):
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.config = config
        self._db_factory = db_session_factory
        self._decision_repo_factory = decision_repo_factory
        self._metrics = metrics

        # WI-54: Resolve provider configuration
        llm_provider_str = self.config.llm_provider.strip().lower()

        if llm_provider_str == LLMProvider.DEEPSEEK.value:
            _api_key = self.config.deepseek_api_key.get_secret_value()
            _base_url = self.config.deepseek_base_url
            _model = self.config.deepseek_model
            _max_tokens = self.config.deepseek_max_tokens
            _max_retries = self.config.deepseek_max_retries
            self._provider_name = LLMProviderName.DEEPSEEK
        else:
            _api_key = self.config.anthropic_api_key.get_secret_value()
            _base_url = "https://api.anthropic.com"
            _model = self.config.anthropic_model
            _max_tokens = self.config.anthropic_max_tokens
            _max_retries = self.config.anthropic_max_retries
            self._provider_name = LLMProviderName.ANTHROPIC

        self.client = AsyncAnthropic(api_key=_api_key, base_url=_base_url, max_retries=_max_retries)
        self.model = _model
        self.max_tokens = _max_tokens
        self.max_retries = _max_retries

        # WI-54: Build secret-free provider metadata for audit/logs
        from urllib.parse import urlparse

        _host = (
            urlparse(_base_url).netloc
            if isinstance(_base_url, str) and "://" in _base_url
            else str(_base_url)
        )
        try:
            self._provider_metadata = LLMProviderMetadata(
                provider=self._provider_name.value,
                model_name=str(_model),
                base_url_host=_host,
            )
        except Exception:
            self._provider_metadata = LLMProviderMetadata(
                provider=self._provider_name.value,
                model_name="unknown",
                base_url_host="unknown",
            )

        self._grok_client = GrokClient(
            api_key=self.config.grok_api_key,
            base_url=self.config.grok_base_url,
            model=self.config.grok_model,
            mocked=self.config.grok_mocked,
            live_enabled=self.config.grok_live_enabled,
            timeout_seconds=self.config.grok_timeout_seconds,
            max_retries=self.config.grok_max_retries,
            total_budget_seconds=_compute_grok_budget(self.config),
        )
        self._running = False
        self._budget_guard = LLMBudgetGuard(self.config, metrics=self._metrics)
        self._cognitive_breaker = MarketCognitiveCircuitBreaker(
            self.config, metrics=self._metrics
        )

    async def start(self) -> None:
        """Starts the evaluation loop."""
        self._running = True
        logger.info(
            "Starting LLM Evaluation Node...",
            model=self.model,
            provider=self._provider_metadata.provider,
            base_url_host=self._provider_metadata.base_url_host,
        )

        # WI-54: Record provider selection metric and active provider gauge
        if self._metrics is not None:
            asyncio.ensure_future(
                self._metrics.record_provider_selection(
                    provider=self._provider_name.value
                )
            )
            asyncio.ensure_future(
                self._metrics.set_active_provider(
                    provider=self._provider_name.value
                )
            )

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
        """Layer 0: classify market from Gamma category, then keyword fallback."""
        category = _coerce_market_category(item.get("category"))
        if category is not None:
            return category

        condition_id = item.get("condition_id", "")
        title = item.get("title") or item.get("question", "")
        raw_tags = item.get("tags", [])
        tags = " ".join(str(tag) for tag in raw_tags if tag is not None)
        text = f"{condition_id} {title} {tags}".lower()

        fallback_order = [
            MarketCategory.CRYPTO,
            MarketCategory.POLITICS,
            MarketCategory.ELECTIONS,
            MarketCategory.SPORTS,
            MarketCategory.ESPORTS,
            MarketCategory.IRAN,
            MarketCategory.FINANCE,
            MarketCategory.GEOPOLITICS,
            MarketCategory.TECH,
            MarketCategory.CULTURE,
            MarketCategory.ECONOMY,
            MarketCategory.WEATHER,
            MarketCategory.MENTIONS,
        ]
        for fallback_category in fallback_order:
            keywords = _ROUTING_TABLE[fallback_category]
            if any(_keyword_matches(text, keyword) for keyword in keywords):
                return fallback_category
        return MarketCategory.CULTURE

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
        - non-eligible categories -> neutral fallback immediately (skip Grok).
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
            if sentiment is NEUTRAL_SENTIMENT:
                failure_reason = self._grok_client.last_failure_reason
                self._log_sentiment(
                    status="FALLBACK",
                    reason=failure_reason.value if failure_reason else "NEUTRAL_RETURNED",
                    sentiment=sentiment,
                    snapshot_id=snapshot_id,
                )
            else:
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

        # WI-52: Derive market key for budget/cooldown tracking
        market_key = market_state.get("condition_id", "unknown")

        # WI-52: Check cognitive cooldown before any provider call
        cooldown_decision = await self._cognitive_breaker.check_cooldown(market_key)
        if cooldown_decision.in_cooldown:
            logger.warning(
                "market_in_cooldown — skipping evaluation.",
                market_key=_hash_market_key(market_key),
                reason=cooldown_decision.cooldown_reason.value if cooldown_decision.cooldown_reason else None,
                snapshot_id=snapshot_id,
            )
            # WI-52: Emit cooldown block metric for actual blocked evaluation
            if self._metrics is not None:
                asyncio.ensure_future(self._metrics.record_llm_cooldown_block())
            return

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
            primary_result, _primary_block = await asyncio.wait_for(
                self._get_primary_candidate(prompt, snapshot_id, market_key=market_key),
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
                block_reason=_primary_block,
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
            market_key=market_key,
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
            provider=self._provider_metadata.provider,
            model=self._provider_metadata.model_name,
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
                    "category": category,
                }
            )
        else:
            logger.info("Trade REJECTED/HOLD by Gatekeeper.", snapshot_id=snapshot_id)

        # WI-52: Record outcome for cognitive cooldown tracking
        action = eval_resp.recommended_action.value
        if action == "HOLD":
            await self._cognitive_breaker.record_outcome(
                market_key=market_key, outcome_type="hold",
            )
        elif action == "SKIP":
            await self._cognitive_breaker.record_outcome(
                market_key=market_key, outcome_type="skip",
            )
        elif action == "BUY":
            await self._cognitive_breaker.record_outcome(
                market_key=market_key, outcome_type="buy",
            )
        elif action == "SELL":
            await self._cognitive_breaker.record_outcome(
                market_key=market_key, outcome_type="sell",
            )

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
    # WI-55: Public evaluate contract for backtest injection
    # ------------------------------------------------------------------

    async def evaluate_for_backtest(
        self, prompt: str, snapshot_id: str = "backtest",
        market_key: str = "backtest-market",
    ) -> tuple[dict[str, Any], dict[str, int] | None, str | None]:
        """Public async evaluate(prompt) -> (dict, usage, block_reason).

        Exercises the full canonical ClaudeClient evaluation chain:
        cooldown → budget guard → primary API call → retry → JSON extraction
        → simple reflection → verdict application.

        ``market_key`` activates per-market budget and cooldown enforcement.

        Returns ``(parsed_dict, token_usage, block_reason)`` where
        ``block_reason`` is one of ``"budget"``, ``"cooldown"``,
        ``"provider_error"``, or ``None`` (call proceeded normally).
        """
        import json as _json

        # --- Cooldown check (matches _process_evaluation flow) ---
        cooldown = await self._cognitive_breaker.check_cooldown(market_key)
        if cooldown.in_cooldown:
            return self._bt_fallback("cooldown blocked primary call"), None, "cooldown"

        # --- Primary call (budget guard + retries + JSON extraction inside; timeout wrapper) ---
        # _get_primary_candidate returns (result, block_reason); block_reason
        # is returned by value to avoid cross-market bleed under concurrent calls.
        try:
            primary, primary_block = await asyncio.wait_for(
                self._get_primary_candidate(
                    prompt=prompt, snapshot_id=snapshot_id,
                    max_retries=self.max_retries, market_key=market_key,
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            return self._bt_fallback("primary call timed out"), None, "provider_error"

        if primary is None:
            reason = primary_block or "provider_error"
            return self._bt_fallback("primary call failed after retries"), None, reason

        _raw, candidate_json, usage = primary

        # --- Simple reflection (same guards active; fail-closed) ---
        reflection = await self._bt_reflection(
            candidate_json=candidate_json, snapshot_id=snapshot_id,
            market_key=market_key,
        )

        if reflection.verdict.value == "REJECTED":
            # Propagate budget-block reason from reflection to the caller
            refl_block = (
                "budget" if "budget" in (reflection.audit_note or "").lower()
                else None
            )
            return self._bt_fallback("reflection rejected the candidate"), usage, refl_block

        final_json = candidate_json
        if (
            reflection.verdict.value == "ADJUSTED"
            and reflection.corrected_candidate_json is not None
        ):
            final_json = _json.dumps(reflection.corrected_candidate_json)

        try:
            parsed = _json.loads(final_json, parse_float=Decimal)
        except (_json.JSONDecodeError, TypeError):
            return self._bt_fallback("final candidate unparseable after reflection"), usage, None

        return parsed, usage, None  # type: ignore[return-value]

    async def _bt_reflection(
        self, *, candidate_json: str, snapshot_id: str,
        market_key: str = "backtest-market",
    ) -> ReflectionResponse:
        """Single-shot reflection call for backtest context.

        Uses the same budget guard and API path as ``_get_primary_candidate``.
        Market context and sentiment are not available — a minimal review
        prompt is used instead.

        **Fail-closed:** budget-blocked or failed reflection returns REJECTED
        so the primary candidate is NOT silently passed through.
        """
        refl_key = f"{market_key}-reflection"
        budget_ok = await self._budget_guard.check_budget(
            call_type="reflection", market_key=refl_key,
        )
        if not budget_ok.allowed:
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note="budget blocked reflection — rejecting candidate",
                latency_ms=0,
            )

        prompt = (
            "You are an audit reviewer. Review this trading candidate JSON "
            "for numerical consistency, bias, and risk. Return ONLY valid JSON:\n"
            '{"verdict": "APPROVED"|"REJECTED"|"ADJUSTED", '
            '"bias_flags": [], "consistency_flags": [], "risk_flags": [], '
            '"audit_note": "... (min 20 chars)", "correction_instructions": null, '
            '"corrected_candidate_json": null}\n\n'
            f"CANDIDATE:\n{candidate_json}"
        )

        try:
            resp = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                ),
                timeout=30.0,
            )
            raw = resp.content[0].text
            json_str = self._extract_json(raw)
            r = ReflectionResponse.model_validate_json(json_str)

            rec_usage = getattr(resp, "usage", None)
            await self._budget_guard.record_usage(
                provider=self._provider_name,
                model_name=self.model,
                input_tokens=getattr(rec_usage, "input_tokens", None),
                output_tokens=getattr(rec_usage, "output_tokens", None),
                market_key=refl_key,
            )
            return r
        except (asyncio.TimeoutError, Exception):
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note="reflection call timed out or failed — rejecting candidate",
                latency_ms=0,
            )

    @staticmethod
    def _bt_fallback(reason: str) -> dict[str, Any]:
        """Build a structurally valid fallback response (>=50 char reasoning)."""
        return {
            "market_context": {
                "condition_id": "0000000000",
                "outcome_evaluated": "YES",
                "best_bid": "0.5", "best_ask": "0.5", "midpoint": "0.5",
            },
            "probabilistic_estimate": {"p_true": "0.5", "p_market": "0.5"},
            "risk_assessment": {
                "liquidity_risk_score": "0.5",
                "resolution_risk_score": "0.5",
                "information_asymmetry_flag": False,
                "risk_notes": f"Fallback — {reason} — this explanation meets minimum length for LLMEvaluationResponse validation schema requirements.",
            },
            "confidence_score": 0.0,
            "decision_boolean": False,
            "recommended_action": "HOLD",
            "reasoning_log": f"Provider call blocked or returned unusable output because {reason} — conservative HOLD enforced.",
        }

    # ------------------------------------------------------------------
    # Stage B: Primary candidate retrieval (pre-Gatekeeper)
    # ------------------------------------------------------------------

    async def _get_primary_candidate(
        self,
        prompt: str,
        snapshot_id: str,
        max_retries: Optional[int] = None,
        market_key: Optional[str] = None,
    ) -> tuple[Optional[tuple[str, str, Dict[str, int]]], Optional[str]]:
        """Make primary LLM call and return (result, block_reason).

        ``result`` is ``(raw_text, candidate_json, token_usage)`` when the call
        succeeds, ``None`` otherwise.  ``block_reason`` is one of ``"budget"``
        or ``"provider_error"`` when the call was blocked, else ``None``.

        Retries on unparseable JSON but does NOT run Gatekeeper validation —
        that happens after reflection (Stage D).

        WI-52: Budget guard is enforced before EVERY paid provider call,
        including each retry attempt.  Returning the block reason directly
        (rather than via instance state) avoids cross-market bleed under
        concurrent invocation.
        """
        max_retries = max_retries if max_retries is not None else self.max_retries
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(max_retries + 1):
            # WI-52: Enforce budget before EACH paid call (including retries)
            budget_decision = await self._budget_guard.check_budget(
                call_type="primary", market_key=market_key,
            )
            if not budget_decision.allowed:
                logger.warning(
                    "llm_budget_blocked — skipping primary provider call.",
                    reason=budget_decision.block_reason.value if budget_decision.block_reason else None,
                    snapshot_id=snapshot_id,
                    attempt=attempt + 1,
                )
                return None, "budget"

            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    temperature=0.0,
                )
                raw_content = resp.content[0].text
                json_str = self._extract_json(raw_content)

                # Verify parseable JSON (structural only, no Gatekeeper filters)
                json.loads(json_str)

                # WI-52: Safely extract usage — may be missing/malformed
                _usage = getattr(resp, "usage", None)
                token_usage = {
                    "input": getattr(_usage, "input_tokens", None),
                    "output": getattr(_usage, "output_tokens", None),
                }

                # WI-52: Record usage after successful call (fallback handled internally)
                await self._budget_guard.record_usage(
                    provider=self._provider_name,
                    model_name=self.model,
                    input_tokens=token_usage["input"],
                    output_tokens=token_usage["output"],
                    market_key=market_key,
                )

                return (raw_content, json_str, token_usage), None

            except json.JSONDecodeError:
                logger.warning(
                    "Primary candidate JSON parse error. Re-prompting.",
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                # WI-52: Record fallback token accounting for invalid JSON
                await self._budget_guard.record_usage(
                    provider=self._provider_name,
                    model_name=self.model,
                    input_tokens=None,
                    output_tokens=None,
                    market_key=market_key,
                )
                # WI-52: Record invalid JSON outcome for cooldown tracking
                await self._cognitive_breaker.record_outcome(
                    market_key=market_key or "unknown",
                    outcome_type="invalid_json",
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
                    return None, "provider_error"
            except Exception as e:
                logger.error(
                    "Provider API Error.",
                    provider=self._provider_metadata.provider,
                    error=str(e),
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                # WI-52: Record provider error
                await self._budget_guard.record_provider_error(market_key=market_key)
                await self._cognitive_breaker.record_outcome(
                    market_key=market_key or "unknown",
                    outcome_type="provider_error",
                )
                if attempt == max_retries:
                    return None, "provider_error"
                await asyncio.sleep(2.0**attempt)

        return None, "provider_error"

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
        market_key: Optional[str] = None,
    ) -> ReflectionResponse:
        """Execute single-pass adversarial reflection on the primary candidate.

        Uses strict remaining shared budget. If budget is exhausted (<=0),
        the API call is skipped entirely and a conservative REJECTED verdict
        is returned immediately.

        WI-52: Budget guard is enforced before every paid reflection call.
        """
        # WI-52: Enforce budget before paid reflection call
        reflection_budget_decision = await self._budget_guard.check_budget(
            call_type="reflection", market_key=market_key,
        )
        if not reflection_budget_decision.allowed:
            logger.warning(
                "llm_budget_blocked — skipping reflection provider call.",
                reason=reflection_budget_decision.block_reason.value if reflection_budget_decision.block_reason else None,
                market_key=_hash_market_key(market_key) if market_key else None,
                snapshot_id=snapshot_id,
            )
            return ReflectionResponse(
                verdict=ReflectionVerdict.REJECTED,
                audit_note="BUDGET_BLOCKED_REFLECTION",
                latency_ms=0,
            )

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

            # WI-52: Record usage for successful reflection call (fallback handled internally)
            _usage = getattr(resp, "usage", None)
            await self._budget_guard.record_usage(
                provider=self._provider_name,
                model_name=self.model,
                input_tokens=getattr(_usage, "input_tokens", None),
                output_tokens=getattr(_usage, "output_tokens", None),
                market_key=market_key,
            )

            return reflection

        except asyncio.TimeoutError:
            logger.warning(
                "Reflection audit timed out (budget exhausted). Defaulting to REJECTED.",
                snapshot_id=snapshot_id,
                budget_s=round(budget, 3),
            )
            # WI-52: Record error with fallback accounting
            await self._budget_guard.record_provider_error(market_key=market_key)
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
            # WI-52: Record error with fallback accounting
            await self._budget_guard.record_provider_error(market_key=market_key)
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
        self, prompt: str, snapshot_id: str, max_retries: Optional[int] = None,
        market_key: Optional[str] = None,
    ) -> Optional[tuple[LLMEvaluationResponse, str, Dict[str, int]]]:
        """Direct evaluation path with budget guard enforcement.

        WI-52: Budget guard is enforced before EVERY paid provider call,
        including each retry attempt.
        """
        max_retries = max_retries if max_retries is not None else self.max_retries
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(max_retries + 1):
            # WI-52: Enforce budget before EACH paid call (including retries)
            budget_decision = await self._budget_guard.check_budget(
                call_type="primary", market_key=market_key,
            )
            if not budget_decision.allowed:
                logger.warning(
                    "llm_budget_blocked — skipping _evaluate_with_retries call.",
                    reason=budget_decision.block_reason.value if budget_decision.block_reason else None,
                    snapshot_id=snapshot_id,
                    attempt=attempt + 1,
                )
                return None

            try:
                logger.debug(
                    "Calling Anthropic API...",
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    temperature=0.0,
                )

                raw_content = resp.content[0].text
                json_str = self._extract_json(raw_content)

                try:
                    eval_response = LLMEvaluationResponse.model_validate_json(json_str)

                    # WI-52: Safely extract usage — may be missing/malformed
                    _usage = getattr(resp, "usage", None)
                    token_usage = {
                        "input": getattr(_usage, "input_tokens", None),
                        "output": getattr(_usage, "output_tokens", None),
                    }

                    # WI-52: Record usage after successful call (fallback handled internally)
                    await self._budget_guard.record_usage(
                        provider=self._provider_name,
                        model_name=self.model,
                        input_tokens=token_usage["input"],
                        output_tokens=token_usage["output"],
                        market_key=market_key,
                    )

                    return eval_response, raw_content, token_usage

                except ValidationError as e:
                    logger.warning(
                        "JSON Validation Error. Re-prompting Claude...",
                        attempt=attempt + 1,
                        snapshot_id=snapshot_id,
                    )
                    # WI-52: Record fallback accounting for invalid JSON
                    await self._budget_guard.record_usage(
                        provider=self._provider_name,
                        model_name=self.model,
                        input_tokens=None, output_tokens=None,
                        market_key=market_key,
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
                    "Provider API Error.",
                    provider=self._provider_metadata.provider,
                    error=str(e),
                    attempt=attempt + 1,
                    snapshot_id=snapshot_id,
                )
                # WI-52: Record provider error
                await self._budget_guard.record_provider_error(market_key=market_key)
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
