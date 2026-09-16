"""Risk limits, as plain data.

Every number a gate can refuse on lives here. Nothing else does — no venue
credentials, no endpoints, no strategy parameters. That separation is the point:
the limits are reviewable by someone who will never read the trading logic, and
the agent proposing a trade cannot reach in and widen them.

Construct one, pass it to RiskManager, and it is read-only for the process
lifetime. If a limit needs to change, it changes here and a human sees the diff.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskConfig:
    """Hard limits for an agent with execution authority.

    The defaults are deliberately conservative: a fresh install refuses more
    than it allows, and paper_trading is on. An operator has to opt into risk.
    """

    # --- sizing -----------------------------------------------------------
    starting_capital: Decimal = Decimal("1000")
    kelly_fraction: float = 0.333
    """Fraction of full Kelly to bet. Third-Kelly is the usual compromise:
    most of the growth, far less of the variance and ruin probability."""

    max_position_pct: Decimal = Decimal("5")
    """Ceiling on any single position, as a percentage of bankroll."""
    max_position_usd: Decimal = Decimal("100")
    """Absolute ceiling, whatever the percentage says. Belt and braces."""
    min_position_usd: Decimal = Decimal("5")
    """Below this, fees dominate the edge. Refuse instead of bleeding."""

    # --- exposure ---------------------------------------------------------
    max_exposure_pct: Decimal = Decimal("30")
    """Total capital at risk across all open positions."""
    fast_path_enabled: bool = False
    fast_path_max_exposure_pct: Decimal = Decimal("20")
    """A lower ceiling for decisions taken without full deliberation."""
    max_concentration: int = 3
    """Maximum simultaneous positions in one correlated category. Ten positions
    that all express the same view are one position with extra fees."""

    # --- loss limits ------------------------------------------------------
    daily_loss_limit_pct: Decimal = Decimal("3")
    drawdown_reduce_pct: Decimal = Decimal("8")
    """Past this drawdown, halve position sizes."""
    drawdown_block_pct: Decimal = Decimal("15")
    """Past this, refuse new positions. Existing ones may still be closed."""
    drawdown_kill_pct: Decimal = Decimal("20")
    """Past this, trip the kill switch. A human has to clear it."""

    # --- other budgets ----------------------------------------------------
    min_edge: float = 0.04
    """Minimum modeled edge to act at all. Filters noise trades."""
    llm_daily_budget_usd: Decimal = Decimal("5")
    """Inference spend is a real cost. An agent that reasons itself broke has
    still lost money."""

    # --- mode -------------------------------------------------------------
    fast_path_direct_mirror_bypass_portfolio_guards: bool = False
    """Escape hatch the original system had for mirrored fills. Default off:
    a bypass that defaults to on is not a rail."""

    paper_trading: bool = True
    """On by default. Live execution is an explicit decision, never a default."""

    def __post_init__(self) -> None:
        if self.drawdown_reduce_pct >= self.drawdown_block_pct:
            raise ValueError("drawdown_reduce_pct must be below drawdown_block_pct")
        if self.drawdown_block_pct >= self.drawdown_kill_pct:
            raise ValueError("drawdown_block_pct must be below drawdown_kill_pct")
        if self.min_position_usd > self.max_position_usd:
            raise ValueError("min_position_usd cannot exceed max_position_usd")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
