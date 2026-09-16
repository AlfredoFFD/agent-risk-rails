# Supervising an agent that can spend money

This is the operating protocol from a system where an LLM agent proposed trades
against a live account. It is not theory. Every rule here exists because the
absence of it cost something.

The short version: **an agent's summary of the world is not evidence about the
world.** Summaries drift, and they drift in the direction of the answer the agent
expects you want. Raw output does not.

## 1. Second-hand summaries are not evidence

When an agent reports a fact that could drive a real-money decision, it must
produce the verbatim output of the thing it looked at, not a paraphrase.

This applies to any claim about: process state, database rows, open positions,
balances, service status, which configuration is actually running, fill history,
PnL, kill-switch state, and — the one that bites most often — **whether a fix was
applied to the running process or only to the repository**.

| Claim | Required evidence |
|---|---|
| Process X is running | Process-manager status output plus the last 20 log lines |
| A stored value is Y | The raw query output, verbatim |
| Config contains Z | The relevant line, secrets redacted |
| A position is open | The venue SDK's own state dump, or a screenshot of the account page |
| Trade count or PnL | The aggregate query, verbatim |
| A fix is deployed | The diff, the restart output, **and** the first log line of the restarted process |
| A file matches the repo | A `diff` between the deployed file and the repo file |

Not acceptable: "I checked and it shows X." "Based on the logs, Y is running."
A paraphrase of a query result. Inference about deployed state from repository
state. A summary of a long output instead of the relevant rows quoted.

## 2. See it twice, by different routes

For anything that lives in an external system, or where a local read could be
stale or cached, confirm through a second independent path. Query the venue SDK
*and* the local ledger and compare. Or read the account page a human would read.

Two sources that agree are a fact. One source is a hypothesis.

## 3. The blast radius of a restart

An agent asked to restart "the paper trading loop" will happily issue the command
that bounces every process in the group, including the ones holding real
positions. Prefer the narrowest command that achieves the goal, and afterwards
verify that every process you expected to stay running has an uptime predating
your change.

Real-money processes stopping silently is how stop-losses get orphaned.

## 4. Rails the agent cannot widen

Every limit in `rails/config.py` is read-only for the process lifetime and lives
outside the agent's reachable surface. The agent proposes; the gate disposes. An
agent that can edit its own risk limits has no risk limits.

The same logic applies to the kill switch: tripping it is automatic, **clearing
it is manual**. Anything the agent can undo is not a stop.

## 5. Defaults refuse

A fresh install runs in paper mode with conservative limits. Live execution is an
explicit, human decision — never inherited from a config file someone copied.
Bypass flags default to off. A bypass that defaults to on is not a rail.

## 6. Know where each gate fails

`RiskManager.run_risk_checks` runs every check and aggregates, rather than
short-circuiting on the first refusal, so an operator sees all the reasons at
once rather than discovering them one restart at a time.

Where a gate depends on something that can be unavailable, decide deliberately
whether it fails **closed** (refuse when uncertain) or **open** (proceed when
uncertain), write that decision down, and make sure the log says which happened.
An undocumented fail-open is the most dangerous thing in a risk system, because
it looks identical to a pass.
