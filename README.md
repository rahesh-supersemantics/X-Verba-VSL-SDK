# X-Verba VSL SDK

A thin developer layer over [VSL-Core](https://github.com/supersemantics/VSL-Core) that lets an
application govern consequential actions: authorise first, record the evidence in a tamper-evident
ledger, and run the side effect only when governance permits it.

The SDK adds **no governance semantics of its own**. PreNodes, Invariants, terminal states, assurance,
verification, the ledger and human re-enablement all come from VSL-Core. Where this SDK and the VSL
Governance Specification v2.0 disagree, the Specification wins.

> **Status:** Phases 1–9 are implemented and tested (see [Project status](#project-status)). The SDK has
> not been run against a live remote ledger service or a live model provider, and this README makes no
> claim that it is production-ready.

## Contents

[Architecture](#architecture) · [Features](#features) · [Installation](#installation) ·
[Quick start](#quick-start) · [YAML policies](#yaml-policies) · [Ledger](#ledger) ·
[Governance lifecycle](#governance-lifecycle) · [CLI](#command-line) ·
[Framework integrations](#framework-integrations) · [X-Verba Scan](#x-verba-scan) · [Testing](#testing) ·
[Security and safety](#security-and-safety) · [Development](#development) ·
[Compatibility](#compatibility) · [Project status](#project-status)

## Architecture

```text
  Application / Agent
        │
        │   LangGraph             LangChain              Microsoft Agent Framework
        │   vsl.integrations      vsl.integrations       vsl.integrations
        │   .langgraph            .langchain             .maf            (each optional)
        ▼         │                    │                      │
              X-Verba SDK  (vsl)  ◄────┴──────────────────────┘
        │   Governance · YAML policy · ledger helpers · CLI
        ▼
     VSL-Core  (vsl_core)
        │   PreNode · Invariant · TerminalState · assurance · VerbaLedger
        ▼
Governance + VerbaLedger   (JSONL file · in memory · remote)

  X-Verba Scan (x-verba, separate product) ── run only through `vsl scan`, no shared data model
```

Authority runs downwards: **VSL Specification → VSL-Core → SDK → framework adapters**. Each layer calls
the one below it and does not re-implement it. The core SDK never imports a framework; each adapter is a
separate subpackage that imports its framework only when you import the adapter.

## Features

| Area | What exists |
|---|---|
| Governed execution | `Governance.authorize()` returns a `Decision`; `Governance.run()` runs a side effect only on PROCEED |
| Suspension | An Invariant violation suspends automation; `active_suspension()`, `reenable()` (named human authority plus evidence), `record_specification_update()` |
| YAML policy | `Governance.from_yaml()` with schema validation, relationship checks, environment interpolation, a monitor/rule registry, no executable code |
| Ledger | JSONL, in-memory and remote ledgers; `verify_integrity()`; the five VSL audit checks; `caused_by` validation; checkpoints that detect truncation; certificates |
| CLI | `vsl verify`, `vsl audit`, `vsl certify` with meaningful exit codes; `vsl scan` forwards to X-Verba Scan |
| Testing framework | `vsl.testing`: harness, observable side effects, outcome and evidence assertions, tamper helpers, policy helpers, and a reusable adapter contract |
| LangGraph | `vsl.integrations.langgraph`: `governed_node`, `add_governed_node`, `governed_router` |
| LangChain | `vsl.integrations.langchain`: `GovernedToolMiddleware` for `create_agent` agents |
| Microsoft Agent Framework | `vsl.integrations.maf`: `GovernedFunctionMiddleware` for `Agent` tools |
| X-Verba Scan | `vsl.integrations.scan.run_scan` and `vsl scan`: run the separate scanner unchanged |

## Installation

```bash
pip install -e .                      # core SDK
pip install -e ".[langgraph]"         # + LangGraph integration
pip install -e ".[langchain]"         # + LangChain integration
pip install -e ".[maf]"               # + Microsoft Agent Framework integration
pip install -e ".[scan]"              # + X-Verba Scan, for `vsl scan` (from git)
pip install -e ".[remote-ledger]"     # + remote ledger client
pip install -e ".[dev,langgraph,langchain,maf]"   # contributors
```

Installing the package provides the `vsl` command (also `python -m vsl`). The core install installs no
framework. VSL-Core, the remote-ledger client and X-Verba Scan come from git; if the client's own pins
conflict with your environment, install it with `pip install --no-deps`.

## Quick start

Policy file (`examples/vsl.yaml` is the full reference policy):

```python
import asyncio
from vsl import Governance

governance = Governance.from_yaml("examples/vsl.yaml")


async def main():
    result = await governance.run(
        "approve_expense",
        {"confidence": 0.95, "amount": 150, "currency": "NZD"},
        effect=lambda: "expense approved",     # the consequential operation
    )

    print(result.decision.outcome, result.performed, result.result)


asyncio.run(main())
```

`run()` calls `authorize()` first and executes `effect` only if the outcome is `Outcome.PROCEED`.
Exceptions raised by `effect` propagate to the caller. The ledger path comes from the policy's
`ledger:` section (override with `VSL_LEDGER_PATH`).

`authorize()` alone only makes the decision; the side effect then remains the caller's responsibility.

## YAML policies

```python
governance = Governance.from_yaml("vsl.yaml")                      # ledger from the policy
governance = Governance.from_yaml("vsl.yaml", ledger=my_ledger)    # inject a ledger
```

A policy has `agent`, `policy`, `assurance_bases`, `terminal_states`, `prenodes`, `invariants`,
`actions` and `ledger` sections (`schema_version: 1`; see `examples/vsl.yaml` and
`vsl/config/schema.json`). Loading is strict and fails closed:

- Safe YAML only; duplicate keys, Python tags and files over 1 MiB are rejected.
- JSON Schema (Draft 2020-12) validation, then relationship checks (unknown references and the like).
- Keys named `api_key`, `password`, `secret` or `token` are rejected: secrets never go in policy files.
- `${VAR}` and `${VAR:-default}` are interpolated in `ledger.*` and construct `params` only.
- Monitors and rules are **names** looked up in a registry (built in: `confidence_gamma`, `max_value`);
  YAML cannot introduce code. Register your own in Python and pass `registry=`.
- `assurance_bases` takes F1/F2 facts only. The assurance level is derived by VSL-Core and cannot be set.
- A policy with no `ledger:` section and no injected ledger is an error.

Errors are `PolicyConfigError`, `PolicySchemaError`, `PolicyValidationError` or `RegistryError`, all
subclasses of `SDKError`.

## Ledger

| Constructor | Backend |
|---|---|
| `jsonl_ledger(path)` | JSONL file, created on first write |
| `open_jsonl_ledger(path)` | existing JSONL file; never creates one; fails if missing or invalid |
| `memory_ledger()` | in memory |
| `remote_ledger(...)` | VSL-Core's remote ledger client; https only; credentials from `LEDGER_API_URL` and `LEDGER_API_KEY` |

### Two different questions

- `verify_integrity()` asks **was anything edited?** It recomputes hashes, links and sequence.
- `audit()` asks **do the records describe a governed process?** It runs the five VSL audit checks;
  `validate_caused_by()` adds the causal check the audit's fallback heuristic can miss.

Neither answers the other's question: an audit does not detect tampering, and an intact ledger can still
describe an ungoverned process.

### Truncation

A hash chain cut short is still a valid chain, so integrity verification cannot detect truncation
(Specification, Known constraint 5). Anchor a checkpoint somewhere the ledger writer cannot modify, then
verify against it later:

```python
checkpoint = governance.checkpoint()               # store it out of the writer's reach
...
result = governance.verify_checkpoint(checkpoint)  # CheckpointVerification; .ok, .code
```

A checkpoint is only as strong as the place it is stored.

## Governance lifecycle

```text
authorize(action, context)
  ├─ active suspension?  ──► SUSPENDED   (MONITOR entry only; nothing else runs)
  ├─ MONITOR written
  ├─ PreNode evaluated
  │    ├─ denied  ──► VERIFICATION(SUFFICIENT) ──► HUMAN_QUEUE
  │    └─ allowed
  ├─ Invariants evaluated
  │    ├─ violated ──► VERIFICATION(INSUFFICIENT) ──► TERMINAL ──► SUSPENDED
  │    └─ all hold
  └─ VERIFICATION(SUFFICIENT) ──► PROCEED
```

| Outcome | Side effect runs? | Meaning |
|---|---|---|
| `PROCEED` | yes | PreNode and Invariants satisfied |
| `HUMAN_QUEUE` | no | A PreNode denied: route to human review. This is governance working, so verification is SUFFICIENT, **not** INSUFFICIENT |
| `SUSPENDED` | no | An Invariant was violated (INSUFFICIENT verification, then a TERMINAL entry) or automation is already suspended |

`INSUFFICIENT` is a ledger verification result, not a fourth `Outcome`. Nothing retries or approves
automatically. Leaving suspension takes a human:

```python
governance.reenable(authority=authority, authorised_by="j.doe", evidence=evidence)
governance.record_specification_update(
    verification_entry_id=insufficient_entry_id,
    new_policy_version="1.1.0", summary="...", approved_by="Finance Controls",
)
```

Every entry is linked by `caused_by` (PRE_NODE → MONITOR, VERIFICATION → PRE_NODE, TERMINAL →
INSUFFICIENT VERIFICATION, and so on), and `validate_caused_by()` checks the chain.

## Command line

```bash
vsl verify  --ledger ledger.jsonl [--checkpoint cp.json] [--write-checkpoint cp.json]
vsl audit   --ledger ledger.jsonl [--max-monitor-gap SECONDS]
vsl certify --ledger ledger.jsonl --max-monitor-gap SECONDS [--checkpoint cp.json]
vsl scan    [ARGS...]            # runs: x-verba scan ARGS...   (needs the [scan] extra)
```

| Exit | Meaning |
|---|---|
| 0 | everything requested passed |
| 1 | a check failed (integrity, audit, causal chain, checkpoint, certification requirements) |
| 2 | bad arguments or checkpoint file; refusing to overwrite a checkpoint |
| 3 | the ledger could not be read (missing, malformed, structurally invalid) |
| 127 | `vsl scan` only: the `x-verba` executable is not installed |

`vsl scan` returns the scanner's own exit status unchanged. The other commands only read ledger files; they never create, repair or rewrite them. Without
`--max-monitor-gap`, `audit` reports check 1 as SKIPPED rather than passed. `--write-checkpoint` runs
only after a clean verification and never overwrites an existing file. The CLI reads JSONL files only.

## Framework integrations

All three adapters share the same rules, which come from the governance lifecycle above:

- The tool or node runs **only** after `Governance.authorize()` returns PROCEED.
- HUMAN_QUEUE and SUSPENDED are never executed, retried or approved by the adapter. The framework is told
  the outcome as a value and carries on.
- Every governed call leaves MONITOR / PRE_NODE / VERIFICATION (and TERMINAL) ledger evidence linked by
  `caused_by`; the `decision_id` in the adapter's `GovernanceRecord` joins to it.
- A failure to obtain a decision (unknown action, ledger failure, a failing `context_fn`) means the effect
  does not run.
- All three are async-only, because `authorize()` is async.

The shared `GovernanceRecord`, `GovernedTool` binding and refusal text live in `vsl.integrations.common`,
which imports no framework. `vsl-langchain` and `vsl-maf` (Super Semantics' standalone adapters) are **not**
dependencies of this SDK: they gate with compiled gates and write no ledger entries, whereas these adapters
go through `Governance` so the evidence exists.

### LangGraph

```bash
pip install -e ".[langgraph]"
```

```python
import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from vsl import Governance
from vsl.integrations.langgraph import (
    GovernanceRecord, add_governed_node, governed_router,
)

governance = Governance.from_yaml("examples/vsl.yaml")


class State(TypedDict, total=False):
    expense: dict
    status: str
    governance: GovernanceRecord          # declare the key in the state schema


def approve(state: State) -> dict:
    # The consequential operation. It runs only if governance returns PROCEED.
    return {"status": "approved"}


graph = StateGraph(State)
add_governed_node(
    graph, "approve", governance, "approve_expense",
    context_fn=lambda s: s["expense"],    # what governance evaluates
    effect=approve,
)
graph.add_node("human_queue", lambda s: {"status": "human_review"})
graph.add_node("suspended", lambda s: {"status": "suspended"})
graph.add_edge(START, "approve")
graph.add_conditional_edges(
    "approve",
    governed_router(proceed=END, human_queue="human_queue", suspended="suspended"),
)
graph.add_edge("human_queue", END)
graph.add_edge("suspended", END)

app = graph.compile()
final = asyncio.run(app.ainvoke({"expense": {"confidence": 0.95, "amount": 150, "currency": "NZD"}}))
print(final["status"], final["governance"]["outcome"])
```

**What the node does.** `governed_node` builds an async node that calls `context_fn(state)`, then
`governance.authorize(...)`, then calls `effect(state)` only on PROCEED. It returns the effect's state
update plus a `GovernanceRecord` under the `governance` key (change it with `key=`; use distinct keys for
several governed nodes so each decision survives in state). The effect never runs before authorisation, and
it never runs for HUMAN_QUEUE or SUSPENDED.

**The record** is plain serialisable data (`action`, `outcome`, `decision_id`, `allowed`,
`requires_human_review`, `suspended`, `performed`, `reason`, `terminal_state`), so it survives LangGraph
checkpointing. `decision_id` joins it to the ledger evidence. `governed_router` reads the record and routes
by outcome; all three targets are required, and a missing or malformed record raises instead of routing.

**State.** The node returns a new update and does not mutate its input state. No global state structure is
imposed beyond the one key you declare. `add_governed_node` refuses to build a graph whose state schema does
not declare the key, because LangGraph silently drops undeclared keys, which would run the effect and lose
the record.

**Errors stay distinct.**

| Situation | Behaviour |
|---|---|
| Governance denies | A value: `outcome` is `human_queue` or `suspended`. Nothing is raised |
| Exception inside `effect` | Propagates unchanged |
| Ledger or other governance failure, unknown action | Propagates unchanged; `effect` has not run |
| Wiring mistakes (non-mapping context or update, key collision, missing record) | `GovernedNodeError` |

**Async only.** `Governance.authorize()` is async, so governed nodes are async: use `ainvoke` / `astream`.
Calling `invoke` makes LangGraph raise `TypeError` before anything executes, so nothing runs and nothing is
written. There is no synchronous wrapper.

**Human review.** HUMAN_QUEUE exposes `requires_human_review` and routes where you point it. The adapter
does not retry, approve or call `interrupt()`; what a reviewer does next is application logic.

### LangChain

```bash
pip install -e ".[langchain]"
```

`GovernedToolMiddleware` is a LangChain `AgentMiddleware` for `create_agent` (LangChain 1.x). It hooks
`awrap_tool_call`, which runs before the tool executes.

```python
from langchain.agents import create_agent
from vsl import Governance
from vsl.integrations.common import GovernedTool
from vsl.integrations.langchain import GovernedToolMiddleware

governance = Governance.from_yaml("examples/vsl.yaml")

agent = create_agent(
    model=my_chat_model,                   # any LangChain chat model
    tools=[approve_expense],               # a tool named "approve_expense"
    middleware=[
        GovernedToolMiddleware(
            governance,
            {"approve_expense": "approve_expense"},   # tool name -> governance action
            # or {"approve_expense": GovernedTool("approve_expense", context_fn=lambda request: {...})}
        )
    ],
)

result = await agent.ainvoke({"messages": [("user", "approve the 150 NZD taxi expense")]})
```

- **Context.** By default governance evaluates the tool call's arguments. Pass `GovernedTool(action,
  context_fn=...)` to choose what is evaluated; whatever it returns is written to the ledger's MONITOR entry,
  so keep secrets out of it.
- **PROCEED:** the real tool runs and its result is returned untouched.
- **HUMAN_QUEUE / SUSPENDED:** the tool does not run. The model receives a `ToolMessage` with
  `status="error"` stating the outcome, and the message's `artifact` carries the `GovernanceRecord`.
- **Unlisted tools** pass through ungoverned and write nothing.
- **Failures** (unknown action, ledger failure, failing `context_fn`) are raised out of `ainvoke`; the tool has
  not run. Exceptions raised by the tool itself are LangChain's to handle.
- **Async only.** A synchronous `agent.invoke` makes LangChain raise `NotImplementedError` before any tool runs.

### Microsoft Agent Framework

```bash
pip install -e ".[maf]"
```

`GovernedFunctionMiddleware` is a Microsoft Agent Framework `FunctionMiddleware` (package `agent-framework`).
Its `process(context, call_next)` runs before each tool call.

```python
from agent_framework import Agent
from vsl import Governance
from vsl.integrations.maf import GovernedFunctionMiddleware

governance = Governance.from_yaml("examples/vsl.yaml")

agent = Agent(
    client=my_chat_client,                 # any agent_framework chat client
    name="expenses",
    instructions="Approve expenses.",
    tools=approve_expense,                 # a tool named "approve_expense"
    middleware=[GovernedFunctionMiddleware(governance, {"approve_expense": "approve_expense"})],
)

response = await agent.run("approve the 150 NZD taxi expense")
```

- **PROCEED:** `call_next()` runs the tool. The record is also stored in
  `context.metadata["vsl_governance"]` for other middleware.
- **HUMAN_QUEUE / SUSPENDED:** the tool does not run and the model receives a text stating the outcome as the
  tool's result.
- **Fail closed.** Microsoft Agent Framework turns an *ordinary* exception from function middleware into a
  tool-error result and keeps the loop running, which would let the agent continue after governance failed.
  So a failure to obtain a decision is raised as the framework's `MiddlewareFailure` (original exception as
  `__cause__`), which aborts the run and reaches your `agent.run` call. The tool does not run.
- **Tool exceptions** are the framework's: it reports them to the model as a tool error. That is not a
  governance denial, and governance has already recorded PROCEED.
- Tools that use the framework's own `approval_mode` are separate from this; this middleware neither grants
  nor replaces framework approvals.

The examples above show the wiring, with `my_chat_model`, `my_chat_client` and the tools standing in for your
own. The test suites run the same wiring end to end against real `create_agent` and `Agent` objects with
scripted models; the SDK has not been run against a live model provider.

## X-Verba Scan

X-Verba Scan (`x-verba`, repository `X-verba-CLI`) is a separate Super Semantics tool: a static analyser that
reads **source code** and reports governance gaps. This SDK governs a **running** application and records
evidence. They answer different questions, and Scan's output (`.verba/governance.yaml`, reports, baselines)
is a different format from `vsl.yaml` and is not ledger evidence.

So the SDK does not import, wrap or reimplement Scan, and does not convert its files. The only integration is
running it:

```bash
pip install -e ".[scan]"
vsl scan . --format json        # identical to: x-verba scan . --format json
```

```python
from vsl.integrations.scan import run_scan
exit_status = run_scan(["."])
```

Everything after `scan` is forwarded unchanged and the scanner's exit status is returned unchanged (127 if
`x-verba` is not installed). Scan's compile and VSL-bundle features are not implemented upstream, so there is
nothing further to connect.

## Testing

```bash
python -m pytest -q -rs
```

LangGraph, LangChain and Microsoft Agent Framework tests skip themselves if that framework is not installed,
the real-scanner test skips if `x-verba` is not installed, and the remote-ledger tests need `httpx` and the
ledger client. `-rs` prints the reason for every skip.

### The testing framework (`vsl.testing`)

Reusable infrastructure so a project or adapter can *prove* governance behaviour. It uses the real SDK,
real policies and real ledgers; nothing is mocked away.

| Piece | Purpose |
|---|---|
| `build_harness(tmp_path)` | A real `Governance` from the reference YAML policy plus a JSONL (or `ledger="memory"`) ledger |
| `harness.side_effect()` / `async_side_effect()` | An observable consequential operation: records calls, can raise, and snapshots the ledger at the moment it runs |
| `ok_context()`, `low_confidence_context()`, `over_cap_context()` | Contexts for PROCEED, HUMAN_QUEUE and SUSPENDED |
| `assert_proceeded`, `assert_human_queued`, `assert_suspended` | Decision assertions |
| `assert_decision_evidence`, `assert_insufficient_evidence` | The exact ledger entries and `caused_by` links for a decision |
| `assert_integrity_ok`, `assert_audit_passes`, `assert_causal_chain_valid` | Whole-ledger checks that delegate to the SDK |
| `suspend`, `resolve_suspension` | Drive and resolve a suspension through the real human flow |
| `tamper_payload`, `tamper_entry_hash`, `break_link`, `drop_entry`, `truncate` | Edit a JSONL ledger behind VSL-Core's back (hashes are never recomputed) |
| `policy_error`, `load_policy_text`, `replace_in_policy` | Test YAML policies, valid and invalid |
| `vsl.testing.contract.AdapterContract` | The contract every adapter must satisfy |
| `vsl.testing.pytest_plugin` | Optional fixtures `vsl_harness`, `vsl_memory_harness`; enable with `pytest_plugins = ["vsl.testing.pytest_plugin"]` |

A governed test:

```python
import asyncio
from vsl.testing import (
    EXPENSE_ACTION, build_harness, low_confidence_context, ok_context,
    assert_proceeded, assert_human_queued, assert_decision_evidence,
)


def test_permitted_action_executes_and_leaves_evidence(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    result = asyncio.run(harness.run(EXPENSE_ACTION, ok_context(), effect))

    assert_proceeded(result.decision)
    effect.assert_executed(1)
    assert_decision_evidence(harness.governance, result.decision)
    # Governance came first: the evidence was already in the ledger.
    assert effect.observations == [["MONITOR", "PRE_NODE", "VERIFICATION"]]


def test_denied_action_does_not_execute(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    result = asyncio.run(harness.run(EXPENSE_ACTION, low_confidence_context(), effect))

    assert_human_queued(result.decision)
    effect.assert_not_executed()
```

Ledger integrity:

```python
from vsl.testing import assert_integrity_failed, tamper_payload

tamper_payload(harness.ledger_path, 0, amount=1)
assert_integrity_failed(harness.governance)
```

**Testing an adapter.** Subclass `AdapterContract` and implement `invoke`; pytest then collects one test per
guarantee (effect only after governance, nothing runs on HUMAN_QUEUE or SUSPENDED, suspension persists until a
human re-enables, effect and governance errors propagate rather than becoming denials, the caller's context is
untouched, the ledger stays verifiable):

```python
from vsl import Outcome
from vsl.testing.contract import AdapterContract


class TestMyAdapter(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        ...  # run `effect` through your adapter; return the Outcome it saw
```

The contract is run against `Governance.run` itself (`tests/test_contract_core.py`) and against the LangGraph,
LangChain and Microsoft Agent Framework adapters. An adapter whose framework itself absorbs tool exceptions, or
wraps governance failures, declares that with the `effect_errors_propagate` and `governance_failure_wrapper`
class attributes instead of weakening the other guarantees. The framework's own tests also run it against deliberately
broken adapters (effect before governance, ignoring the decision, swallowing errors, approving during
suspension) to prove it catches them.

## Security and safety

What the implementation provides, each covered by tests:

- **Governance before side effects.** `run()`, `governed_node` and the LangChain and Microsoft Agent Framework
  middleware call the effect only after PROCEED;
  tests observe the ledger evidence already present when the effect executes.
- **Fail closed.** Only an unambiguous PROCEED runs an effect. A failing ledger or governance error stops the
  effect and propagates; it is not converted into a denial. A missing JSONL ledger is never silently created as
  an empty one.
- **No automatic approval.** HUMAN_QUEUE and SUSPENDED are never retried or overridden by the SDK or adapter.
- **Human authorisation to resume.** Leaving suspension requires `reenable()` with a named authority and
  evidence, and the specification update is recorded against the INSUFFICIENT verification.
- **Ledger integrity.** Edited payloads, hashes, links and sequence gaps are detected by VSL-Core; truncation
  is detected only against an externally stored checkpoint.
- **Secrets.** Policy files may not contain secret-like keys; remote ledger credentials come from the
  environment; remote URLs must be https.
- **No code from policy.** YAML cannot carry executable logic, and the SDK source contains no dynamic code
  execution (checked by a test).

What it does not provide: authentication of callers or reviewers, protection of ledger files or checkpoints
from someone who can write to them, or any claim about the quality of the underlying model.

## Development

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,langgraph,langchain,maf,remote-ledger]"
python -m pytest -q -rs                              # -rs explains any skips
python -m pip wheel . --no-deps -w dist              # build a wheel
```

No linter or type checker is configured in this repository. Generated files (`__pycache__/`, `.pytest_cache/`,
virtual environments, `dist/`, coverage output, `.env*`, local ledgers and checkpoints named `*.ledger.jsonl` /
`*.checkpoint.json`, and Scan's generated `.verba/governance-report.*` and `.verba/governance-history/`) are
git-ignored; other `.jsonl` files are deliberately not ignored.

Layout:

```text
vsl/                      core SDK (governance, config, policy, registry, ledger, checkpoint, inspection, audit, cli)
vsl/testing/              reusable testing framework
vsl/integrations/common.py    framework-free helpers shared by the adapters
vsl/integrations/langgraph/   LangGraph adapter (optional)
vsl/integrations/langchain/   LangChain adapter (optional)
vsl/integrations/maf/         Microsoft Agent Framework adapter (optional)
vsl/integrations/scan/        runs X-Verba Scan (optional)
governance/policy.py      programmatic reference policy (equivalence tests)
examples/vsl.yaml         reference YAML policy
tests/                    regression, framework and adapter tests
```

## Compatibility

| Component | Tested with |
|---|---|
| Python | 3.10.12 (declared `>=3.10`; other versions not tested) |
| VSL-Core | the pinned git commit used during development |
| LangGraph | 1.2.12 (extra declares `>=1.2,<2`; other versions not tested) |
| LangChain | 1.4.3 (extra declares `>=1.0,<2`; other versions not tested) |
| Microsoft Agent Framework | `agent-framework` 1.19.0 (extra declares `>=1.19,<2`; other versions not tested) |
| X-Verba Scan | `x-verba` 0.6.0, used only through the `vsl scan` passthrough |
| Platforms | Linux (the Windows working tree was edited, but tests ran on Linux) |

## Project status

| Phase | Scope | Status |
|---|---|---|
| 1–2 | Governed execution, suspension, re-enablement, specification update | Implemented, tested |
| 2.5 | `verify_integrity()`, `audit()`, `caused_by` validation | Implemented, tested |
| 3 | YAML policy, registry, schema | Implemented, tested |
| 4 | Ledger backends, checkpoints, CLI | Implemented, tested |
| 5 | Testing framework (`vsl.testing`) | Implemented, tested |
| 6 | LangGraph adapter | Implemented, tested |
| 7 | LangChain adapter | Implemented, tested |
| 8 | Microsoft Agent Framework adapter | Implemented, tested |
| 9 | X-Verba Scan boundary (`vsl scan`) | Implemented, tested |

Not implemented: build-time governance, `vsl init`, converting Scan output into a VSL policy.

Known limitations:

- The policy hash is not recorded in the ledger (VSL-Core has no entry type for it).
- The CLI reads JSONL files only; remote ledgers are used from Python.
- The remote-ledger path is tested only against an in-process fake API backed by a real VSL-Core store.
- The JSONL store re-reads the file on each operation, so cost grows with ledger size.
- Concurrent writers are protected only by VSL-Core's own file locking.
- `Governance.authorize()` and therefore every adapter is async-only; there is no synchronous wrapper.
- The LangChain and Microsoft Agent Framework adapters were tested with scripted models and clients, and
  only on the versions in the table above. Streaming paths and live providers (for example Azure AI Foundry)
  were not exercised.
- The adapters do not depend on Super Semantics' `vsl-langchain` or `vsl-maf` packages, and do not run
  against them.
- `x-verba` is installed from git (it is not on PyPI); `vsl scan` has been checked only as far as the
  passthrough, plus a real `--help` run if the tool was installed when the tests ran.
- Fallback parameters on a PreNode are declarative in VSL-Core; the SDK does not execute them.
