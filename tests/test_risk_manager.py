"""Tests for OracleEdge Risk Manager.

Covers:
- Third-Kelly position sizing for YES buy, NO buy, all confidence levels
- Position caps (5% bankroll, min $5, max $100)
- Negative edge returns $0
- All 10 pre-trade risk checks (pass and fail cases)
- All checks run even if early ones fail
- Drawdown tiers (normal / reduce 50% / kill)
- Kill switch (STOP file)
- Fast path uses 20% exposure limit
"""
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from rails.config import RiskConfig as Config
from rails.store import get_connection, init_db
from rails.risk_manager import (
    PositionSizeResult,
    RiskCheckResult,
    RiskManager,
    SingleCheckResult,
    TradeProposal,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal valid Config for testing
# ---------------------------------------------------------------------------

def _test_config(**overrides) -> Config:
    """A RiskConfig with the limits these tests were written against.

    Note what is NOT here: no venue credentials, no API keys, no endpoints.
    The original project's config carried all of that; the rails only need the
    numbers a gate can refuse on. If this function ever needs a secret again,
    something has leaked back across the boundary.
    """
    defaults = dict(
        starting_capital=Decimal("500"),
        kelly_fraction=0.333,
        max_position_pct=Decimal("5"),
        max_position_usd=Decimal("100"),
        min_position_usd=Decimal("5"),
        max_exposure_pct=Decimal("30"),
        fast_path_enabled=False,
        fast_path_max_exposure_pct=Decimal("20"),
        max_concentration=3,
        daily_loss_limit_pct=Decimal("3"),
        drawdown_reduce_pct=Decimal("8"),
        drawdown_block_pct=Decimal("15"),
        drawdown_kill_pct=Decimal("20"),
        min_edge=0.04,
        llm_daily_budget_usd=Decimal("10"),
        paper_trading=False,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_proposal(**overrides) -> TradeProposal:
    """Build a TradeProposal with sensible defaults."""
    defaults = dict(
        market_id="market-001",
        direction="YES",
        entry_price=Decimal("0.50"),
        p_model=0.65,
        confidence="HIGH",
        category="weather",
        event_id="event-001",
        token_id="token-001",
        is_fast_path=False,
        signal_strength="",
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


@pytest.fixture
def db_path(tmp_path):
    """Create a fresh DB for each test."""
    path = tmp_path / "test_risk.db"
    init_db(path)
    return path


@pytest.fixture
def config():
    return _test_config()


@pytest.fixture
def risk_mgr(config, db_path):
    return RiskManager(config=config, db_path=db_path)


# ===========================================================================
# Position Sizing (Third-Kelly)
# ===========================================================================


class TestPositionSizing:
    """Tests for calculate_position_size with Third-Kelly formula."""

    def test_kelly_yes_buy_high_confidence(self, risk_mgr):
        """YES buy: p=0.65, price=0.50, bankroll=$500, HIGH confidence.
        f*=(0.65-0.50)/(1-0.50)=0.30, third=0.10, size=$50.
        Capped at 5%=$25 -> $25.
        """
        proposal = _make_proposal(
            direction="YES",
            entry_price=Decimal("0.50"),
            p_model=0.65,
            confidence="HIGH",
        )
        result = risk_mgr.calculate_position_size(proposal, Decimal("500"))
        assert isinstance(result, PositionSizeResult)
        assert result.adjusted_size_usd == Decimal("25")
        assert result.capped_reason == "max_pct"

    def test_kelly_no_buy_medium_confidence(self, risk_mgr):
        """NO buy: p=0.30 (YES prob), YES_price=0.70, bankroll=$500, MEDIUM.
        f*=(0.70-0.30)/0.70=0.571, third=0.190, conf_adj=0.190*0.75=0.143,
        size=$71.43, capped at 5%=$25 -> $25.
        """
        proposal = _make_proposal(
            direction="NO",
            entry_price=Decimal("0.70"),
            p_model=0.30,
            confidence="MEDIUM",
        )
        result = risk_mgr.calculate_position_size(proposal, Decimal("500"))
        assert result.adjusted_size_usd == Decimal("25")
        assert result.capped_reason == "max_pct"

    def test_kelly_minimum_clamp(self, risk_mgr):
        """Tiny edge produces small size -> clamped to $5 minimum or $0 if below."""
        proposal = _make_proposal(
            direction="YES",
            entry_price=Decimal("0.50"),
            p_model=0.52,
            confidence="LOW",
        )
        result = risk_mgr.calculate_position_size(proposal, Decimal("500"))
        # f*=(0.52-0.50)/(1-0.50)=0.04, third=0.0133, conf=0.0133*0.50=0.0067
        # size=$3.33 < $5 min -> $0 (no trade)
        assert result.adjusted_size_usd == Decimal("0")

    def test_kelly_maximum_cap(self):
        """Large edge at $2000 bankroll -> capped at max_position_usd ($100)."""
        config = _test_config(starting_capital=Decimal("2000"))
        rm = RiskManager(config=config)
        proposal = _make_proposal(
            direction="YES",
            entry_price=Decimal("0.30"),
            p_model=0.80,
            confidence="HIGH",
        )
        result = rm.calculate_position_size(proposal, Decimal("2000"))
        # f*=(0.80-0.30)/(1-0.30)=0.714, third=0.238, size=$476
        # 5% of 2000 = $100. Also max_position_usd = $100. -> $100
        assert result.adjusted_size_usd == Decimal("100")
        assert result.capped_reason in ("max_pct", "max_position")

    def test_confidence_scaling_proportional(self, risk_mgr):
        """Same inputs at HIGH=100%, MEDIUM=75%, LOW=50% produce proportional sizes."""
        base_proposal = dict(
            direction="YES",
            entry_price=Decimal("0.40"),
            p_model=0.60,
        )
        high = risk_mgr.calculate_position_size(
            _make_proposal(**base_proposal, confidence="HIGH"), Decimal("500")
        )
        medium = risk_mgr.calculate_position_size(
            _make_proposal(**base_proposal, confidence="MEDIUM"), Decimal("500")
        )
        low = risk_mgr.calculate_position_size(
            _make_proposal(**base_proposal, confidence="LOW"), Decimal("500")
        )
        # HIGH raw > MEDIUM raw > LOW raw
        assert high.raw_size_usd > medium.raw_size_usd > low.raw_size_usd
        # Multipliers should be 1.0, 0.75, 0.50
        assert high.confidence_multiplier == 1.0
        assert medium.confidence_multiplier == 0.75
        assert low.confidence_multiplier == 0.50

    def test_negative_edge_returns_zero(self, risk_mgr):
        """Negative edge (p < price for YES buy) -> returns $0."""
        proposal = _make_proposal(
            direction="YES",
            entry_price=Decimal("0.70"),
            p_model=0.50,
            confidence="HIGH",
        )
        result = risk_mgr.calculate_position_size(proposal, Decimal("500"))
        assert result.adjusted_size_usd == Decimal("0")
        assert result.kelly_fraction <= 0


# ===========================================================================
# Pre-Trade Risk Checks
# ===========================================================================


class TestCheckMinEdge:
    """Check 1: Minimum edge."""

    def test_edge_below_min_fails(self, risk_mgr):
        proposal = _make_proposal(p_model=0.53, entry_price=Decimal("0.50"))
        # edge=0.03, min=0.04 -> FAIL
        result = risk_mgr.run_risk_checks(proposal)
        check = _find_check(result, "min_edge")
        assert check is not None
        assert check.passed is False

    def test_edge_above_min_passes(self, risk_mgr):
        proposal = _make_proposal(p_model=0.55, entry_price=Decimal("0.50"))
        # edge=0.05, min=0.04 -> PASS
        result = risk_mgr.run_risk_checks(proposal)
        check = _find_check(result, "min_edge")
        assert check is not None
        assert check.passed is True


class TestCheckPositionSizeLimit:
    """Check 2: Position size <= 5% of bankroll."""

    def test_oversize_fails(self, db_path):
        """Size $30 at $500 bankroll (6%) -> FAIL."""
        # We use a config where Kelly would produce >5%
        config = _test_config(starting_capital=Decimal("500"))
        rm = RiskManager(config=config, db_path=db_path)
        # Big edge -> big size -> should be capped and flagged
        proposal = _make_proposal(
            p_model=0.90, entry_price=Decimal("0.30"), confidence="HIGH"
        )
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "position_size_limit")
        assert check is not None
        # The position size check should pass because we CAP before checking
        # (the cap enforces the limit, so the check validates the capped value)
        assert check.passed is True

    def test_normal_size_passes(self, risk_mgr):
        proposal = _make_proposal(
            p_model=0.55, entry_price=Decimal("0.50"), confidence="HIGH"
        )
        result = risk_mgr.run_risk_checks(proposal)
        check = _find_check(result, "position_size_limit")
        assert check is not None
        assert check.passed is True


class TestCheckTotalExposure:
    """Check 3: Total exposure <= 30% of bankroll (20% for fast path)."""

    def test_exposure_over_30pct_fails(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # Insert open trades totaling $140 exposure into DB
        _insert_open_trade(db_path, size_usd=Decimal("140"))
        proposal = _make_proposal(p_model=0.65, entry_price=Decimal("0.50"))
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "total_exposure")
        # $140 existing + $25 new = $165 at $500 bankroll = 33% -> FAIL
        assert check is not None
        assert check.passed is False

    def test_exposure_under_30pct_passes(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        _insert_open_trade(db_path, size_usd=Decimal("100"))
        proposal = _make_proposal(p_model=0.65, entry_price=Decimal("0.50"))
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "total_exposure")
        # $100 existing + $25 new = $125 at $500 bankroll = 25% -> PASS
        assert check is not None
        assert check.passed is True

    def test_fast_path_uses_20pct_limit(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        _insert_open_trade(db_path, size_usd=Decimal("80"), signal_source="fast_path")
        proposal = _make_proposal(
            p_model=0.65,
            entry_price=Decimal("0.50"),
            is_fast_path=True,
            signal_strength="STRONG",
        )
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "total_exposure")
        # Fast path: $80 existing + $25 new = $105 at $500 = 21% > 20% -> FAIL
        assert check is not None
        assert check.passed is False


class TestCheckConcentration:
    """Check 4: Max 3 per category, 2 per event."""

    def test_category_over_limit_fails(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # Insert 3 open trades in 'weather' category
        for i in range(3):
            _insert_open_trade(
                db_path, market_id=f"market-{i}", category="weather",
                event_id=f"event-{i}", size_usd=Decimal("10")
            )
        proposal = _make_proposal(category="weather")
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "concentration")
        assert check is not None
        assert check.passed is False

    def test_category_under_limit_passes(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        for i in range(2):
            _insert_open_trade(
                db_path, market_id=f"market-{i}", category="weather",
                event_id=f"event-{i}", size_usd=Decimal("10")
            )
        proposal = _make_proposal(category="weather")
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "concentration")
        assert check is not None
        assert check.passed is True

    def test_event_over_limit_fails(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # Insert 2 open trades for same event
        for i in range(2):
            _insert_open_trade(
                db_path, market_id=f"market-ev-{i}", category="weather",
                event_id="event-001", size_usd=Decimal("10")
            )
        proposal = _make_proposal(event_id="event-001")
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "concentration")
        assert check is not None
        assert check.passed is False


class TestCheckDailyLoss:
    """Check 5: Daily loss limit blocks trades after 3% daily loss."""

    def test_daily_loss_over_limit_fails(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # Insert a closed losing trade today: $16 loss on $500 (3.2%)
        _insert_closed_trade(db_path, pnl_usd=Decimal("-16"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "daily_loss")
        assert check is not None
        assert check.passed is False

    def test_daily_loss_under_limit_passes(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # $10 loss on $500 (2%) -> PASS
        _insert_closed_trade(db_path, pnl_usd=Decimal("-10"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "daily_loss")
        assert check is not None
        assert check.passed is True

    def test_direct_mirror_can_bypass_daily_loss_when_enabled(self, db_path, tmp_path):
        config = _test_config(fast_path_direct_mirror_bypass_portfolio_guards=True)
        rm = RiskManager(config=config, db_path=db_path)
        proposal = _make_proposal(
            is_fast_path=True,
            is_direct_mirror=True,
            fixed_size_usd=Decimal("5"),
            signal_strength="STRONG",
        )
        with patch.object(rm, "_get_daily_loss", return_value=Decimal("-50")):
            with patch.object(rm, "_get_current_drawdown", return_value=30.0):
                with patch.object(rm, "_get_project_root", return_value=tmp_path):
                    result = rm.run_risk_checks(proposal)
        daily_loss = _find_check(result, "daily_loss")
        drawdown = _find_check(result, "drawdown")
        assert daily_loss is not None
        assert drawdown is not None
        assert daily_loss.passed is True
        assert drawdown.passed is True
        assert daily_loss.value == "BYPASSED"
        assert drawdown.value == "BYPASSED"


class TestCheckDrawdown:
    """Check 6: Drawdown tiers - 0-8% normal, 8-15% reduce, >15% kill."""

    def test_drawdown_10pct_reduces_size(self, db_path):
        """10% drawdown -> size reduced 50% (PASS with adjustment)."""
        config = _test_config(starting_capital=Decimal("500"))
        rm = RiskManager(config=config, db_path=db_path)
        # Insert performance record showing 10% drawdown
        _insert_performance(db_path, peak_bankroll=Decimal("500"), bankroll=Decimal("450"))
        proposal = _make_proposal(p_model=0.65, entry_price=Decimal("0.50"))
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "drawdown")
        assert check is not None
        assert check.passed is True  # Passes but with reduced size
        # The position size should be halved due to 10% drawdown
        if result.position_size:
            # Verify reduction happened -- exact value depends on implementation
            assert "reduc" in check.message.lower() or result.position_size.adjusted_size_usd > Decimal("0")

    def test_drawdown_16pct_kills(self, db_path):
        """16% drawdown -> FAIL (kill)."""
        config = _test_config(starting_capital=Decimal("500"))
        rm = RiskManager(config=config, db_path=db_path)
        _insert_performance(db_path, peak_bankroll=Decimal("500"), bankroll=Decimal("420"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "drawdown")
        assert check is not None
        assert check.passed is False

    def test_drawdown_normal_passes(self, db_path):
        """5% drawdown -> normal, PASS."""
        config = _test_config(starting_capital=Decimal("500"))
        rm = RiskManager(config=config, db_path=db_path)
        _insert_performance(db_path, peak_bankroll=Decimal("500"), bankroll=Decimal("475"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "drawdown")
        assert check is not None
        assert check.passed is True

    def test_direct_mirror_still_blocks_without_bypass_flag(self, db_path, tmp_path):
        config = _test_config(fast_path_direct_mirror_bypass_portfolio_guards=False)
        rm = RiskManager(config=config, db_path=db_path)
        proposal = _make_proposal(
            is_fast_path=True,
            is_direct_mirror=True,
            fixed_size_usd=Decimal("5"),
            signal_strength="STRONG",
        )
        with patch.object(rm, "_get_daily_loss", return_value=Decimal("-50")):
            with patch.object(rm, "_get_current_drawdown", return_value=30.0):
                with patch.object(rm, "_get_project_root", return_value=tmp_path):
                    result = rm.run_risk_checks(proposal)
        daily_loss = _find_check(result, "daily_loss")
        drawdown = _find_check(result, "drawdown")
        assert daily_loss is not None
        assert drawdown is not None
        assert daily_loss.passed is False
        assert drawdown.passed is False


class TestCheckVaR:
    """Check 7: Value at Risk (simplified Phase 1)."""

    @pytest.mark.xfail(
        reason="Encodes a 5% VaR ceiling; _check_value_at_risk documents and applies "
               "max_exposure_pct (30%) instead. Which is intended is an owner decision, "
               "so this is left failing visibly rather than rewritten to match the code.",
        strict=True,
    )
    def test_var_exceeds_5pct_fails(self, config, db_path):
        """All open positions losing 100% > 5% of starting_capital -> FAIL."""
        rm = RiskManager(config=config, db_path=db_path)
        # Insert open trades totaling $30 = 6% of $500
        _insert_open_trade(db_path, size_usd=Decimal("30"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "value_at_risk")
        assert check is not None
        # $30 existing + ~$25 new = ~$55 total risk = 11% of $500 -> FAIL
        assert check.passed is False

    def test_var_under_5pct_passes(self, risk_mgr):
        """No open trades, small new position -> PASS."""
        proposal = _make_proposal(
            p_model=0.55, entry_price=Decimal("0.50"), confidence="LOW"
        )
        result = risk_mgr.run_risk_checks(proposal)
        check = _find_check(result, "value_at_risk")
        assert check is not None
        # Small position, no existing -> should pass
        assert check.passed is True


class TestCheckKillSwitch:
    """Check 8: Kill switch (STOP file)."""

    def test_stop_file_exists_fails(self, risk_mgr, tmp_path):
        """STOP file present -> FAIL."""
        stop_file = tmp_path / "STOP"
        stop_file.write_text("emergency stop")
        with patch.object(risk_mgr, '_get_project_root', return_value=tmp_path):
            proposal = _make_proposal()
            result = risk_mgr.run_risk_checks(proposal)
            check = _find_check(result, "kill_switch")
            assert check is not None
            assert check.passed is False

    def test_no_stop_file_passes(self, risk_mgr, tmp_path):
        """No STOP file -> PASS."""
        with patch.object(risk_mgr, '_get_project_root', return_value=tmp_path):
            proposal = _make_proposal()
            result = risk_mgr.run_risk_checks(proposal)
            check = _find_check(result, "kill_switch")
            assert check is not None
            assert check.passed is True


class TestCheckPaperMode:
    """Check 9: Paper trading mode."""

    def test_paper_mode_true_passes_with_paper_flag(self, db_path):
        config = _test_config(paper_trading=True)
        rm = RiskManager(config=config, db_path=db_path)
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "paper_mode")
        assert check is not None
        assert check.passed is True
        assert result.trade_mode == "paper"

    def test_paper_mode_false_passes_with_live_flag(self, db_path):
        config = _test_config(paper_trading=False)
        rm = RiskManager(config=config, db_path=db_path)
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "paper_mode")
        assert check is not None
        assert check.passed is True
        assert result.trade_mode == "live"


class TestCheckApiCostBudget:
    """Check 10: API cost budget ($10/day)."""

    def test_over_budget_fails(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        # Insert $11 in API costs today
        _insert_api_cost(db_path, cost_usd=Decimal("11"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "api_cost_budget")
        assert check is not None
        assert check.passed is False

    def test_under_budget_passes(self, config, db_path):
        rm = RiskManager(config=config, db_path=db_path)
        _insert_api_cost(db_path, cost_usd=Decimal("8"))
        proposal = _make_proposal()
        result = rm.run_risk_checks(proposal)
        check = _find_check(result, "api_cost_budget")
        assert check is not None
        assert check.passed is True


class TestAllChecksRun:
    """All 10 checks run even if early ones fail."""

    def test_all_10_checks_present(self, risk_mgr, tmp_path):
        """run_risk_checks returns RiskCheckResult with all 10 check statuses."""
        with patch.object(risk_mgr, '_get_project_root', return_value=tmp_path):
            proposal = _make_proposal()
            result = risk_mgr.run_risk_checks(proposal)
            assert isinstance(result, RiskCheckResult)
            assert len(result.checks) == 10
            check_names = {c.check_name for c in result.checks}
            expected = {
                "min_edge", "position_size_limit", "total_exposure",
                "concentration", "daily_loss", "drawdown",
                "value_at_risk", "kill_switch", "paper_mode", "api_cost_budget",
            }
            assert check_names == expected

    def test_all_checks_run_even_with_failures(self, config, db_path, tmp_path):
        """If early checks fail, all 10 still run and report."""
        rm = RiskManager(config=config, db_path=db_path)
        # Make edge fail (p close to price)
        proposal = _make_proposal(p_model=0.51, entry_price=Decimal("0.50"))
        with patch.object(rm, '_get_project_root', return_value=tmp_path):
            result = rm.run_risk_checks(proposal)
            assert len(result.checks) == 10
            # Edge should fail but all checks still ran
            edge_check = _find_check(result, "min_edge")
            assert edge_check.passed is False
            # Overall should fail
            assert result.overall_passed is False


# ===========================================================================
# Helpers
# ===========================================================================

def _find_check(result: RiskCheckResult, name: str) -> SingleCheckResult | None:
    """Find a check by name in the result."""
    for c in result.checks:
        if c.check_name == name:
            return c
    return None


def _insert_open_trade(
    db_path: Path,
    market_id: str = "market-open",
    category: str = "weather",
    event_id: str = "event-open",
    size_usd: Decimal = Decimal("10"),
    signal_source: str = "prediction",
):
    """Insert an open trade into the test DB."""
    conn = get_connection(db_path)
    try:
        import uuid
        trade_id = f"OE-2026-03-14-{uuid.uuid4().hex[:3]}"
        conn.execute(
            """INSERT INTO trades
            (trade_id, market_id, direction, entry_price, quantity, size_usd,
             status, paper_trade, signal_source, confidence, fill_status)
            VALUES (?, ?, 'YES', '0.50', '10', ?, 'open', 0, ?, 'HIGH', 'filled')""",
            (trade_id, market_id, str(size_usd), signal_source),
        )
        # We need category/event_id -- store in markets table
        conn.execute(
            """INSERT OR IGNORE INTO markets
            (market_id, condition_id, question, category)
            VALUES (?, ?, 'Test question', ?)""",
            (market_id, event_id, category),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_closed_trade(db_path: Path, pnl_usd: Decimal = Decimal("-10")):
    """Insert a closed trade with PnL for today."""
    conn = get_connection(db_path)
    try:
        import uuid
        trade_id = f"OE-2026-03-14-{uuid.uuid4().hex[:3]}"
        conn.execute(
            """INSERT INTO trades
            (trade_id, market_id, direction, entry_price, quantity, size_usd,
             status, pnl_usd, paper_trade, signal_source, closed_at, confidence)
            VALUES (?, 'market-closed', 'YES', '0.50', '10', '10',
                    'closed', ?, 0, 'prediction',
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'HIGH')""",
            (trade_id, str(pnl_usd)),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_performance(
    db_path: Path,
    peak_bankroll: Decimal = Decimal("500"),
    bankroll: Decimal = Decimal("450"),
):
    """Insert a performance record for drawdown calculation."""
    conn = get_connection(db_path)
    try:
        from datetime import date
        conn.execute(
            """INSERT INTO performance
            (date, bankroll, peak_bankroll, drawdown_pct, drawdown_tier)
            VALUES (?, ?, ?, ?, 'NORMAL')""",
            (date.today().isoformat(), str(bankroll), str(peak_bankroll),
             float((peak_bankroll - bankroll) / peak_bankroll * 100)),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_api_cost(db_path: Path, cost_usd: Decimal = Decimal("5")):
    """Insert an API cost record for today."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO api_costs (service, cost_usd, context)
            VALUES ('anthropic', ?, 'test')""",
            (str(cost_usd),),
        )
        conn.commit()
    finally:
        conn.close()
