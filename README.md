# agent-risk-rails

A pre-execution gate for autonomous agents that can move real money.

An agent proposes an action. Ten independent checks run against the current state
of the world, every one of them, and each returns a pass or a refusal with the
number it refused on. The agent never sees the limits and cannot change them.

Extracted from a live prediction-market trading system, with every strategy,
venue credential and wallet removed. What remains is the part that says no.

```python
from decimal import Decimal
from rails.store import init_db
from rails.config import RiskConfig
from rails.risk_manager import RiskManager, TradeProposal

init_db("ledger.db")
rails = RiskManager(config=RiskConfig(), db_path="ledger.db")

proposal = TradeProposal(
    market_id="mkt-1", direction="YES", entry_price=Decimal("0.50"),
    p_model=0.52, confidence="HIGH", category="weather",
    event_id="evt-1", token_id="tok-1",
)

result = rails.run_risk_checks(proposal)
print(result.overall_passed, result.trade_mode)   # False paper

for check in result.checks:
    print(f"{'PASS  ' if check.passed else 'REFUSE'} {check.check_name}: {check.message}")
```

Real output from the defaults above — a 2% edge against a 4% minimum:

```text
False paper
REFUSE min_edge             Edge 0.0200 < 0.0400 minimum
PASS   position_size_limit  Position $13.32 <= $50 (5%)
PASS   total_exposure       Exposure $13.32 <= $300 (30%)
PASS   concentration        Category 0/3, Event 0/2
PASS   daily_loss           Daily loss $0 <= $30 (3%)
PASS   drawdown             Drawdown 0.0% in NORMAL tier (< 8.0%)
PASS   value_at_risk        Total risk $13.32 <= $300 (30% VaR limit)
PASS   kill_switch          No STOP file -- kill switch disengaged
PASS   paper_mode           Trade mode: paper
PASS   api_cost_budget      API costs $0 <= $5/day
```

Every gate ran and reported, including the nine that passed after one refused.

## The ten gates

| Gate | Refuses when |
|---|---|
| `min_edge` | The modeled edge is too thin to pay for fees and noise |
| `position_size_limit` | One position exceeds its percentage or absolute ceiling |
| `total_exposure` | Open risk across everything exceeds the portfolio limit |
| `concentration` | Too many simultaneous positions express the same view |
| `daily_loss` | Today's realized losses have passed the daily limit |
| `drawdown` | Drawdown from peak has entered the reduce, block, or kill tier |
| `value_at_risk` | Worst-case simultaneous loss exceeds the portfolio ceiling |
| `kill_switch` | A stop file is present. Tripped automatically, **cleared only by a human** |
| `paper_mode` | The proposal's execution mode contradicts the configured mode |
| `api_cost_budget` | Inference spend has passed its daily budget |

Every check runs even after one fails, so an operator sees all the reasons at
once instead of discovering them one restart at a time.

Position sizing is fractional Kelly, adjusted by stated confidence, then capped
by both a percentage of bankroll and an absolute dollar ceiling, then floored —
below the floor the fees eat the edge, so the answer is no rather than a
token-sized yes.

## Design

**The agent cannot widen its own limits.** `RiskConfig` is a frozen dataclass,
constructed once by the operator and passed in. It holds only numbers a gate can
refuse on: no endpoints, no credentials, no strategy parameters. A reviewer who
will never read the trading logic can still audit every limit in one file.

**Defaults refuse.** A fresh `RiskConfig()` is in paper mode with conservative
limits, and every bypass flag defaults to off. Live execution is an explicit
decision, never an inherited one.

**Nothing here knows what a venue is.** `risk_manager.py` imports no exchange
SDK. It reads a local ledger — five tables — and answers questions about what is
already at risk. Point it at your own store and it works unchanged.

**Paper fills do not consume live risk budget.** Every live-risk query filters on
`paper_trade = 0`, and exposure counts only orders that actually filled. A
resting order is not exposure.

## Setup

```bash
pip install -r requirements.txt
pytest -q                 # 32 passed, 1 xfailed — no network, no credentials
```

`init_db(path)` creates the five tables in `rails/schema.sql`: trades, markets,
performance, api_costs and portfolio_snapshots. Point it at your own store
instead if you already have one; the gates only read.

## Supervising the agent above the rails

Code gates what an agent may execute. It cannot gate what an agent *claims*.
[`docs/AGENT_SUPERVISION.md`](docs/AGENT_SUPERVISION.md) is the operating
protocol from running this for real: why a summary is not evidence, what counts
as proof for each kind of claim, and why tripping a kill switch is automatic
while clearing it is not.

## Honest notes

**The value-at-risk gate applies `max_exposure_pct`, not a separate VaR
percentage.** Its docstring says so deliberately, because the exposure gate
already covers position-level risk. A test encoding a 5% ceiling is left in the
suite as a strict `xfail` rather than rewritten to match the code, because which
behavior is intended is a decision for whoever operates it. Its message string
used to hardcode "5% VaR limit" while enforcing 30%; that was a real defect and
is fixed — a rail that misreports its own threshold is worse than no rail.

**These limits are not advice.** They encode one operator's tolerance at one
account size. Read `rails/config.py`, decide your own numbers, and expect to
defend them.

## What is deliberately not here

No strategies or signals. No venue clients, keys or wallet addresses. No
order-routing or execution code. The rails are the reusable part; the alpha is
not, and publishing it would destroy it anyway.

MIT licensed.
