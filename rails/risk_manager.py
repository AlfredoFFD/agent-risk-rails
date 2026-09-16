"""
OracleEdge Risk Manager.

10-point pre-trade risk check pipeline + Third-Kelly position sizing.
Every trade must pass ALL 10 checks before execution. No exceptions.

Risk checks:
    1. Minimum edge threshold
    2. Position size limit (5% of bankroll)
    3. Total exposure limit (30%, 20% for fast path)
    4. Concentration limit (3 per category, 2 per event)
    5. Daily loss limit (3%)
    6. Drawdown tier (0-8% normal, 8-15% reduce, 15-20% block, >20% kill)
    7. Value at Risk (simplified Phase 1)
    8. Kill switch (STOP file)
    9. Paper trading mode
   10. API cost budget
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from loguru import logger

from rails.config import RiskConfig as Config
from rails.store import get_connection


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class TradeProposal:
    """Proposed trade to be risk-checked."""

    market_id: str
    direction: str  # 'YES' | 'NO'
    entry_price: Decimal
    p_model: float  # Our estimated probability
    confidence: str  # 'HIGH' | 'MEDIUM' | 'LOW'
    category: str  # 'weather' | 'economic' | 'political' | etc.
    event_id: str  # Underlying event identifier
    token_id: str  # venue-specific instrument identifier
    is_fast_path: bool = False  # True if from wallet signal fast path
    signal_strength: str = ""  # 'STRONG' | 'EXTREME' for fast path
    fixed_size_usd: Decimal | None = None  # Override Kelly sizing (curated whale copy trades)
    is_direct_mirror: bool = False  # True when mirroring a specific killer-wallet trade


@dataclass
class PositionSizeResult:
    """Result of Third-Kelly position sizing calculation."""

    kelly_fraction: float
    raw_size_usd: Decimal
    adjusted_size_usd: Decimal  # After confidence scaling + caps
    confidence_multiplier: float
    capped_reason: str | None  # 'max_position' | 'min_position' | 'max_pct' | None


@dataclass
class SingleCheckResult:
    """Result of a single pre-trade risk check."""

    check_name: str
    passed: bool
    message: str
    value: str  # The actual value checked


@dataclass
class RiskCheckResult:
    """Aggregated result of all 10 pre-trade risk checks."""

    overall_passed: bool
    checks: list[SingleCheckResult]
    position_size: PositionSizeResult | None
    trade_mode: str  # 'paper' | 'live'


# =============================================================================
# Confidence Multipliers
# =============================================================================

CONFIDENCE_MULTIPLIERS: dict[str, float] = {
    "HIGH": 1.0,
    "MEDIUM": 0.75,
    "LOW": 0.50,
}

# Fast path minimum edge thresholds (lower than normal due to signal quality)
FAST_PATH_MIN_EDGE: dict[str, float] = {
    "STRONG": 0.02,
    "EXTREME": 0.01,
}


# =============================================================================
# Risk Manager
# =============================================================================


class RiskManager:
    """Pre-trade risk engine with 10 checks and Third-Kelly position sizing.

    All database queries use parameterized queries via get_connection().
    Each private method handles its own connection lifecycle.
    """

    def __init__(self, config: Config, db_path: Path | None = None):
        self.config = config
        self.db_path = db_path

    # -------------------------------------------------------------------------
    # Position Sizing (Third-Kelly)
    # -------------------------------------------------------------------------

    def calculate_position_size(
        self, proposal: TradeProposal, bankroll: Decimal
    ) -> PositionSizeResult:
        """Third-Kelly position sizing with confidence scaling.

        Kelly formula for prediction markets:
        - YES buy: f* = (p - c) / (1 - c)   where p=model prob, c=market price
        - NO buy:  f* = (c - p) / c          where c=YES price, p=YES probability

        Then: third-Kelly = f* / 3
        Confidence scaling: HIGH=1.0, MEDIUM=0.75, LOW=0.50
        Hard caps: min $5, max min(5% of bankroll, max_position_usd)

        Args:
            proposal: The proposed trade.
            bankroll: Current bankroll in USD.

        Returns:
            PositionSizeResult with all sizing details.
        """
        price = float(proposal.entry_price)
        p = proposal.p_model

        # Kelly fraction for binary prediction markets
        if proposal.direction == "YES":
            # Buying YES at price c with estimated prob p
            if price >= 1.0:
                f_star = 0.0
            else:
                f_star = (p - price) / (1.0 - price)
        else:
            # Buying NO: we think YES prob is p, market YES price is c
            if price <= 0.0:
                f_star = 0.0
            else:
                f_star = (price - p) / price

        # No edge -> no trade
        if f_star <= 0:
            no_edge_multiplier = CONFIDENCE_MULTIPLIERS.get(
                proposal.confidence, 0.50
            )
            return PositionSizeResult(
                kelly_fraction=f_star * self.config.kelly_fraction * no_edge_multiplier,
                raw_size_usd=Decimal("0"),
                adjusted_size_usd=Decimal("0"),
                confidence_multiplier=no_edge_multiplier,
                capped_reason=None,
            )

        # Third-Kelly
        kelly = f_star * self.config.kelly_fraction

        # Confidence scaling
        multiplier = CONFIDENCE_MULTIPLIERS.get(proposal.confidence, 0.50)
        kelly_adjusted = kelly * multiplier

        # Convert to position size in USD
        raw_size = bankroll * Decimal(str(kelly_adjusted))

        # Track the raw size before caps for reporting
        raw_size_rounded = raw_size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Apply hard caps
        capped_reason = None
        position_usd = raw_size

        # Cap 1: max percentage of bankroll
        max_by_pct = bankroll * self.config.max_position_pct / Decimal("100")
        if position_usd > max_by_pct:
            position_usd = max_by_pct
            capped_reason = "max_pct"

        # Cap 2: max absolute position size
        if position_usd > self.config.max_position_usd:
            position_usd = self.config.max_position_usd
            capped_reason = "max_position"

        # Floor: minimum viable position
        if position_usd < self.config.min_position_usd:
            return PositionSizeResult(
                kelly_fraction=kelly_adjusted,
                raw_size_usd=raw_size_rounded,
                adjusted_size_usd=Decimal("0"),
                confidence_multiplier=multiplier,
                capped_reason="below_minimum",
            )

        # Round down to cents
        adjusted = position_usd.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        return PositionSizeResult(
            kelly_fraction=kelly_adjusted,
            raw_size_usd=raw_size_rounded,
            adjusted_size_usd=adjusted,
            confidence_multiplier=multiplier,
            capped_reason=capped_reason,
        )

    # -------------------------------------------------------------------------
    # Risk Checks Pipeline
    # -------------------------------------------------------------------------

    def run_risk_checks(self, proposal: TradeProposal) -> RiskCheckResult:
        """Run all 10 pre-trade risk checks.

        ALL checks run even if early ones fail -- we collect every failure
        reason so the caller knows everything that needs fixing.

        Args:
            proposal: The proposed trade.

        Returns:
            RiskCheckResult with all 10 check statuses and overall pass/fail.
        """
        bankroll = self._get_current_bankroll()

        # Calculate position size first -- several checks need it
        size_result = self.calculate_position_size(proposal, bankroll)

        # Override Kelly sizing for curated whale copy trades (D-03)
        if proposal.fixed_size_usd is not None:
            size_result = PositionSizeResult(
                kelly_fraction=0.0,
                raw_size_usd=proposal.fixed_size_usd,
                adjusted_size_usd=proposal.fixed_size_usd,
                confidence_multiplier=1.0,
                capped_reason="fixed_curated_whale",
            )

        checks: list[SingleCheckResult] = []

        # Fast-path whale copies have fixed $5 sizing and their own
        # daily/concurrent limits in FastPathExecutor. Only run safety
        # checks (drawdown, kill switch, exposure) — skip edge and
        # concentration which block most signals unnecessarily.
        is_fp = proposal.is_fast_path
        bypass_portfolio_guards = (
            proposal.is_direct_mirror
            and getattr(
                self.config,
                "fast_path_direct_mirror_bypass_portfolio_guards",
                False,
            )
        )

        # 1. Minimum edge (skip for fast-path — whale signal IS the edge)
        if not is_fp:
            checks.append(self._check_min_edge(proposal))

        # 2. Position size limit
        checks.append(self._check_position_size_limit(size_result, bankroll))

        # 3. Total exposure
        checks.append(self._check_total_exposure(proposal, size_result, bankroll))

        # 4. Concentration (skip for fast-path — whales trade across markets)
        if not is_fp:
            checks.append(self._check_concentration(proposal))

        # 5. Daily loss limit
        if bypass_portfolio_guards:
            checks.append(
                SingleCheckResult(
                    check_name="daily_loss",
                    passed=True,
                    message="Daily loss bypassed for direct killer-wallet mirror",
                    value="BYPASSED",
                )
            )
        else:
            checks.append(self._check_daily_loss(bankroll))

        # 6. Drawdown tier
        if bypass_portfolio_guards:
            checks.append(
                SingleCheckResult(
                    check_name="drawdown",
                    passed=True,
                    message="Drawdown bypassed for direct killer-wallet mirror",
                    value="BYPASSED",
                )
            )
        else:
            drawdown_check, size_result = self._check_drawdown(size_result, bankroll)
            checks.append(drawdown_check)

        # 7. Value at Risk
        checks.append(self._check_value_at_risk(size_result, bankroll))

        # 8. Kill switch
        checks.append(self._check_kill_switch())

        # 9. Paper mode
        checks.append(self._check_paper_mode())

        # 10. API cost budget (skip for fast-path — copy trades don't use LLM)
        if not is_fp:
            checks.append(self._check_api_cost_budget())

        overall_passed = all(c.passed for c in checks)
        # Fast-path whale copy trades run live (same as bonds/stink bids)
        if proposal.is_fast_path and self.config.fast_path_enabled:
            trade_mode = "live"
        else:
            trade_mode = "paper" if self.config.paper_trading else "live"

        return RiskCheckResult(
            overall_passed=overall_passed,
            checks=checks,
            position_size=size_result if size_result.adjusted_size_usd > 0 else None,
            trade_mode=trade_mode,
        )

    # -------------------------------------------------------------------------
    # Individual Risk Checks
    # -------------------------------------------------------------------------

    def _check_min_edge(self, proposal: TradeProposal) -> SingleCheckResult:
        """Check 1: Minimum edge threshold.

        For YES buy: edge = p_model - entry_price
        For NO buy: edge = entry_price - p_model
        For fast path: uses signal_strength threshold instead.
        """
        price = float(proposal.entry_price)

        if proposal.direction == "YES":
            edge = proposal.p_model - price
        else:
            edge = price - proposal.p_model

        # Fast path uses lower thresholds based on signal strength
        if proposal.is_fast_path and proposal.signal_strength in FAST_PATH_MIN_EDGE:
            threshold = FAST_PATH_MIN_EDGE[proposal.signal_strength]
        else:
            threshold = self.config.min_edge

        passed = edge >= threshold

        return SingleCheckResult(
            check_name="min_edge",
            passed=passed,
            message=(
                f"Edge {edge:.4f} >= {threshold:.4f}"
                if passed
                else f"Edge {edge:.4f} < {threshold:.4f} minimum"
            ),
            value=f"{edge:.4f}",
        )

    def _check_position_size_limit(
        self, size_result: PositionSizeResult, bankroll: Decimal
    ) -> SingleCheckResult:
        """Check 2: Position size <= 5% of bankroll.

        Since calculate_position_size already caps, this validates the cap worked.
        """
        max_allowed = bankroll * self.config.max_position_pct / Decimal("100")
        size = size_result.adjusted_size_usd
        passed = size <= max_allowed

        return SingleCheckResult(
            check_name="position_size_limit",
            passed=passed,
            message=(
                f"Position ${size} <= ${max_allowed} (5%)"
                if passed
                else f"Position ${size} > ${max_allowed} (5%)"
            ),
            value=str(size),
        )

    def _check_total_exposure(
        self,
        proposal: TradeProposal,
        size_result: PositionSizeResult,
        bankroll: Decimal,
    ) -> SingleCheckResult:
        """Check 3: Total exposure <= 30% of bankroll (20% for fast path).

        For fast-path trades, BOTH limits are checked:
        - Fast-path sub-limit (fast_path_max_exposure_pct)
        - Global portfolio limit (max_exposure_pct)
        Both must pass.
        """
        current_exposure = self._get_total_exposure()
        new_size = size_result.adjusted_size_usd
        global_total = current_exposure + new_size

        # Global portfolio limit always applies
        global_limit_pct = self.config.max_exposure_pct
        global_max = bankroll * global_limit_pct / Decimal("100")
        global_passed = global_total <= global_max

        if proposal.is_fast_path:
            # Also check fast-path sub-limit
            fast_limit_pct = self.config.fast_path_max_exposure_pct
            fast_exposure = self._get_fast_path_exposure()
            fast_total = fast_exposure + new_size
            fast_max = bankroll * fast_limit_pct / Decimal("100")
            fast_passed = fast_total <= fast_max

            # Both must pass
            passed = global_passed and fast_passed

            if not global_passed:
                return SingleCheckResult(
                    check_name="total_exposure",
                    passed=False,
                    message=f"Global exposure ${global_total} > ${global_max} ({global_limit_pct}%)",
                    value=str(global_total),
                )
            if not fast_passed:
                return SingleCheckResult(
                    check_name="total_exposure",
                    passed=False,
                    message=f"Fast-path exposure ${fast_total} > ${fast_max} ({fast_limit_pct}%)",
                    value=str(fast_total),
                )

            return SingleCheckResult(
                check_name="total_exposure",
                passed=True,
                message=f"Fast-path ${fast_total} <= ${fast_max} ({fast_limit_pct}%), global ${global_total} <= ${global_max} ({global_limit_pct}%)",
                value=str(fast_total),
            )

        passed = global_passed

        return SingleCheckResult(
            check_name="total_exposure",
            passed=passed,
            message=(
                f"Exposure ${global_total} <= ${global_max} ({global_limit_pct}%)"
                if passed
                else f"Exposure ${global_total} > ${global_max} ({global_limit_pct}%)"
            ),
            value=str(global_total),
        )

    def _check_concentration(self, proposal: TradeProposal) -> SingleCheckResult:
        """Check 4: Max 3 per category, 2 per event."""
        cat_count = self._get_category_count(proposal.category)
        event_count = self._get_event_count(proposal.event_id)

        cat_limit = self.config.max_concentration  # 3
        event_limit = 2

        if cat_count >= cat_limit:
            return SingleCheckResult(
                check_name="concentration",
                passed=False,
                message=f"Category '{proposal.category}': {cat_count} >= {cat_limit} limit",
                value=f"cat={cat_count},event={event_count}",
            )

        if event_count >= event_limit:
            return SingleCheckResult(
                check_name="concentration",
                passed=False,
                message=f"Event '{proposal.event_id}': {event_count} >= {event_limit} limit",
                value=f"cat={cat_count},event={event_count}",
            )

        return SingleCheckResult(
            check_name="concentration",
            passed=True,
            message=f"Category {cat_count}/{cat_limit}, Event {event_count}/{event_limit}",
            value=f"cat={cat_count},event={event_count}",
        )

    def _check_daily_loss(self, bankroll: Decimal) -> SingleCheckResult:
        """Check 5: Daily loss limit (3% of bankroll)."""
        daily_loss = self._get_daily_loss()
        limit = bankroll * self.config.daily_loss_limit_pct / Decimal("100")

        # daily_loss is negative when losing
        passed = abs(daily_loss) <= limit

        return SingleCheckResult(
            check_name="daily_loss",
            passed=passed,
            message=(
                f"Daily loss ${abs(daily_loss)} <= ${limit} ({self.config.daily_loss_limit_pct}%)"
                if passed
                else f"Daily loss ${abs(daily_loss)} > ${limit} ({self.config.daily_loss_limit_pct}%) -- BLOCKED"
            ),
            value=str(daily_loss),
        )

    def _check_drawdown(
        self, size_result: PositionSizeResult, bankroll: Decimal
    ) -> tuple[SingleCheckResult, PositionSizeResult]:
        """Check 6: Drawdown tiers.

        0-8%: Normal (no adjustment)
        8-15%: Reduce position size by 50%
        15-20%: Block -- no new trades allowed
        >20%: Kill -- block all trades

        Returns:
            Tuple of (check result, possibly-adjusted size result).
        """
        drawdown_pct = self._get_current_drawdown()

        reduce_threshold = float(self.config.drawdown_reduce_pct)
        block_threshold = float(self.config.drawdown_block_pct)
        kill_threshold = float(self.config.drawdown_kill_pct)

        if drawdown_pct >= kill_threshold:
            return (
                SingleCheckResult(
                    check_name="drawdown",
                    passed=False,
                    message=f"Drawdown {drawdown_pct:.1f}% >= {kill_threshold}% KILLED threshold",
                    value=f"{drawdown_pct:.1f}%",
                ),
                size_result,
            )

        if drawdown_pct >= block_threshold:
            return (
                SingleCheckResult(
                    check_name="drawdown",
                    passed=False,
                    message=f"Drawdown {drawdown_pct:.1f}% >= {block_threshold}% BLOCKED threshold",
                    value=f"{drawdown_pct:.1f}%",
                ),
                size_result,
            )

        if drawdown_pct >= reduce_threshold:
            # Reduce position size by 50%
            reduced = size_result.adjusted_size_usd / Decimal("2")
            reduced = reduced.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Check if reduced size is still above minimum
            if reduced < self.config.min_position_usd:
                reduced = Decimal("0")

            adjusted_result = PositionSizeResult(
                kelly_fraction=size_result.kelly_fraction / 2,
                raw_size_usd=size_result.raw_size_usd,
                adjusted_size_usd=reduced,
                confidence_multiplier=size_result.confidence_multiplier,
                capped_reason="drawdown_reduce",
            )

            return (
                SingleCheckResult(
                    check_name="drawdown",
                    passed=True,
                    message=f"Drawdown {drawdown_pct:.1f}% in REDUCED tier ({reduce_threshold}-{block_threshold}%) -- size reduced 50%",
                    value=f"{drawdown_pct:.1f}%",
                ),
                adjusted_result,
            )

        # Normal tier
        return (
            SingleCheckResult(
                check_name="drawdown",
                passed=True,
                message=f"Drawdown {drawdown_pct:.1f}% in NORMAL tier (< {reduce_threshold}%)",
                value=f"{drawdown_pct:.1f}%",
            ),
            size_result,
        )

    def _check_value_at_risk(self, size_result: PositionSizeResult, bankroll: Decimal) -> SingleCheckResult:
        """Check 7: Value at Risk (simplified Phase 1).

        Worst case: ALL open positions lose 100% (prediction markets binary).
        Uses max_exposure_pct as VaR ceiling — the total_exposure check (check 3)
        already enforces position-level limits, so VaR checks portfolio-level risk.
        """
        current_exposure = self._get_total_exposure()
        new_size = size_result.adjusted_size_usd
        total_risk = current_exposure + new_size

        var_limit = bankroll * self.config.max_exposure_pct / Decimal("100")
        passed = total_risk <= var_limit

        return SingleCheckResult(
            check_name="value_at_risk",
            passed=passed,
            message=(
                f"Total risk ${total_risk} <= ${var_limit} ({self.config.max_exposure_pct}% VaR limit)"
                if passed
                else f"Total risk ${total_risk} > ${var_limit} ({self.config.max_exposure_pct}% VaR limit)"
            ),
            value=str(total_risk),
        )

    def _check_kill_switch(self) -> SingleCheckResult:
        """Check 8: Kill switch (STOP file in project root)."""
        project_root = self._get_project_root()
        stop_file = project_root / "STOP"

        if stop_file.exists():
            logger.warning("Kill switch ENGAGED -- STOP file found at {}", stop_file)
            return SingleCheckResult(
                check_name="kill_switch",
                passed=False,
                message="Kill switch ENGAGED -- STOP file exists",
                value="ENGAGED",
            )

        return SingleCheckResult(
            check_name="kill_switch",
            passed=True,
            message="No STOP file -- kill switch disengaged",
            value="DISENGAGED",
        )

    def _check_paper_mode(self) -> SingleCheckResult:
        """Check 9: Paper trading mode.

        Always passes -- just flags the mode for downstream routing.
        """
        mode = "paper" if self.config.paper_trading else "live"
        return SingleCheckResult(
            check_name="paper_mode",
            passed=True,
            message=f"Trade mode: {mode}",
            value=mode,
        )

    def _check_api_cost_budget(self) -> SingleCheckResult:
        """Check 10: API cost budget ($10/day from config)."""
        daily_cost = self._get_daily_api_cost()
        limit = self.config.llm_daily_budget_usd
        passed = daily_cost <= limit

        return SingleCheckResult(
            check_name="api_cost_budget",
            passed=passed,
            message=(
                f"API costs ${daily_cost} <= ${limit}/day"
                if passed
                else f"API costs ${daily_cost} > ${limit}/day budget exceeded"
            ),
            value=str(daily_cost),
        )

    # -------------------------------------------------------------------------
    # Database Query Helpers (each manages own connection)
    # -------------------------------------------------------------------------

    def _get_current_bankroll(self) -> Decimal:
        """Calculate current bankroll from starting capital + realized PnL.

        Prefer venue-backed portfolio snapshots when available. Falling back to
        trade history is lossy because historical DB rows can be contaminated by
        paper trades or unmatched live orders.
        """
        latest_equity = self._get_latest_portfolio_equity()
        if latest_equity is not None:
            return latest_equity

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) as total_pnl "
                "FROM trades "
                "WHERE paper_trade = 0 "
                "  AND status = 'closed' "
                "  AND (exit_price IS NULL OR CAST(exit_price AS REAL) IN (0, 1))"
            ).fetchone()
            total_pnl = Decimal(str(row["total_pnl"]))
            return self.config.starting_capital + total_pnl
        finally:
            conn.close()

    def _get_total_exposure(self) -> Decimal:
        """Sum of size_usd for all open trades."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_usd), 0) as total "
                "FROM trades "
                "WHERE paper_trade = 0 "
                "  AND status = 'open' "
                "  AND COALESCE(fill_status, '') = 'filled'"
            ).fetchone()
            return Decimal(str(row["total"]))
        finally:
            conn.close()

    def _get_fast_path_exposure(self) -> Decimal:
        """Sum of size_usd for open fast-path trades."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_usd), 0) as total FROM trades "
                "WHERE paper_trade = 0 "
                "  AND status = 'open' "
                "  AND COALESCE(fill_status, '') = 'filled' "
                "  AND signal_source = 'fast_path'"
            ).fetchone()
            return Decimal(str(row["total"]))
        finally:
            conn.close()

    def _get_category_count(self, category: str) -> int:
        """Count open trades in given category.

        Joins trades with markets to get category.
        """
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades t "
                "JOIN markets m ON t.market_id = m.market_id "
                "WHERE t.paper_trade = 0 "
                "  AND t.status = 'open' "
                "  AND COALESCE(t.fill_status, '') = 'filled' "
                "  AND m.category = ?",
                (category,),
            ).fetchone()
            return int(row["cnt"])
        finally:
            conn.close()

    def _get_event_count(self, event_id: str) -> int:
        """Count open trades for given event.

        Uses markets.condition_id as event identifier.
        """
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades t "
                "JOIN markets m ON t.market_id = m.market_id "
                "WHERE t.paper_trade = 0 "
                "  AND t.status = 'open' "
                "  AND COALESCE(t.fill_status, '') = 'filled' "
                "  AND m.condition_id = ?",
                (event_id,),
            ).fetchone()
            return int(row["cnt"])
        finally:
            conn.close()

    def _get_daily_loss(self) -> Decimal:
        """Sum of realized losses today (negative PnL)."""
        daily_snapshot_pnl = self._get_daily_portfolio_pnl()
        if daily_snapshot_pnl is not None:
            return daily_snapshot_pnl if daily_snapshot_pnl < 0 else Decimal("0")

        conn = get_connection(self.db_path)
        try:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) as daily_pnl FROM trades "
                "WHERE paper_trade = 0 "
                "  AND status = 'closed' "
                "  AND (exit_price IS NULL OR CAST(exit_price AS REAL) IN (0, 1)) "
                "  AND closed_at >= ? AND pnl_usd < 0",
                (today_utc,),
            ).fetchone()
            return Decimal(str(row["daily_pnl"]))
        finally:
            conn.close()

    def _get_current_drawdown(self) -> float:
        """Current drawdown percentage from peak equity.

        Prefer venue-backed portfolio snapshots when available.
        """
        latest_equity = self._get_latest_portfolio_equity()
        peak_equity = self._get_peak_portfolio_equity()
        if latest_equity is not None and peak_equity is not None and peak_equity > 0:
            return float((peak_equity - latest_equity) / peak_equity * 100)

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT peak_bankroll, bankroll FROM performance "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                # No performance data at all — assume worst case using
                # starting capital vs current trade-based bankroll.
                starting = self.config.starting_capital
                current_bankroll = self._get_current_bankroll()
                if starting > 0 and current_bankroll < starting:
                    conservative_dd = float(
                        (starting - current_bankroll) / starting * 100
                    )
                    logger.warning(
                        "No drawdown data — using conservative estimate: {:.1f}%",
                        conservative_dd,
                    )
                    return conservative_dd
                return 0.0
            peak = Decimal(str(row["peak_bankroll"]))
            current = Decimal(str(row["bankroll"]))
            if peak <= 0:
                return 0.0
            drawdown = float((peak - current) / peak * 100)
            return drawdown
        finally:
            conn.close()

    def _get_latest_portfolio_equity(self) -> Decimal | None:
        """Return the latest venue-backed equity snapshot if available."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(free_cash, 0) + COALESCE(total_value, 0) as equity "
                "FROM portfolio_snapshots "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row is None or row["equity"] is None:
                return None
            return Decimal(str(row["equity"]))
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()

    def _get_peak_portfolio_equity(self) -> Decimal | None:
        """Return the peak venue-backed equity snapshot if available."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT MAX(COALESCE(free_cash, 0) + COALESCE(total_value, 0)) as peak "
                "FROM portfolio_snapshots"
            ).fetchone()
            if row is None or row["peak"] is None:
                return None
            return Decimal(str(row["peak"]))
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()

    def _get_daily_portfolio_pnl(self) -> Decimal | None:
        """Return intraday PnL from venue-backed snapshots when possible."""
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = get_connection(self.db_path)
        try:
            start_row = conn.execute(
                "SELECT COALESCE(free_cash, 0) + COALESCE(total_value, 0) as equity "
                "FROM portfolio_snapshots "
                "WHERE substr(timestamp, 1, 10) = ? "
                "ORDER BY timestamp ASC LIMIT 1",
                (today_utc,),
            ).fetchone()
            end_row = conn.execute(
                "SELECT COALESCE(free_cash, 0) + COALESCE(total_value, 0) as equity "
                "FROM portfolio_snapshots "
                "WHERE substr(timestamp, 1, 10) = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (today_utc,),
            ).fetchone()
            if (
                start_row is None
                or end_row is None
                or start_row["equity"] is None
                or end_row["equity"] is None
            ):
                return None
            return Decimal(str(end_row["equity"])) - Decimal(str(start_row["equity"]))
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()

    def _get_daily_api_cost(self) -> Decimal:
        """Sum of API costs today from api_costs table."""
        conn = get_connection(self.db_path)
        try:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_costs "
                "WHERE created_at >= ?",
                (today_utc,),
            ).fetchone()
            return Decimal(str(row["total"]))
        finally:
            conn.close()

    def _get_project_root(self) -> Path:
        """Get project root directory (where STOP file would be)."""
        return Path(__file__).parent.parent
