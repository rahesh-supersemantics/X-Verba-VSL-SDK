# X-Verba VSL SDK

A thin developer layer above VSL-Core. It adds no governance semantics of its own: every
gate, ledger entry, verification result and certificate is produced by VSL-Core. Where the
SDK and the VSL Governance Specification v2.0 disagree, the Specification wins.

This README documents what is implemented and covered by tests. It makes no claim that
the SDK is production-ready.

## Install

```bash
pip install -e ".[dev]"            # core + test tools
pip install -e ".[remote-ledger]"  # only if you use a remote ledger
```

This installs the `vsl` command (also available as `python -m vsl`).

## Phase 3: YAML policy

```python
from vsl import Governance

governance = Governance.from_yaml("vsl.yaml")
result = await governance.authorize("approve_expense", context)
```

`examples/vsl.yaml` is the reference policy. It matches the programmatic expense policy in
`governance/policy.py`, except that the built-in `max_value` rule checks the amount only,
not the currency.

What the loader does:

- Reads YAML with a safe loader that rejects duplicate keys. Files over 1 MiB are rejected.
- Validates against a JSON Schema (`vsl/config/schema.json`, Draft 2020-12), then checks
  relationships: unknown references, a PreNode attached to more than one action, and so on.
- Rejects keys named `api_key`, `password`, `secret` or `token`. Secrets never go in
  policy files.
- Interpolates `${VAR}` and `${VAR:-default}`, in `ledger.*` and construct `params` only.
  It runs in a single pass. A whole-value result is coerced to bool, int or finite float.
  An empty variable counts as missing.
- Takes only F1/F2 facts for `assurance_bases`. The assurance level is derived by VSL-Core
  and cannot be set in YAML.
- Contains no executable logic. Monitors and rules are looked up by name in a registry. The
  built-ins are `confidence_gamma` and `max_value`. Register your own in Python with
  `Registry.monitor(name)` or `Registry.rule(name)` and pass `registry=` to `from_yaml`.
- Compiles to real VSL-Core constructs (`PreNode`, `Invariant`, `TerminalState`,
  `Fallback`, `AssuranceBasis`). `PolicyRegistry` keys policies by (name, version).

Invalid configuration raises `PolicyConfigError`, `PolicySchemaError` or
`PolicyValidationError`, each with the list of problems.

## Phase 4: ledgers, checkpoints and the CLI

Ledger helpers in `vsl.ledger`:

| Helper | Backend |
|---|---|
| `jsonl_ledger(path)` / `open_jsonl_ledger(path)` | JSONL file (the second never creates a file and fails if it is missing or invalid) |
| `memory_ledger()` | in-memory |
| `remote_ledger(...)` | VSL-Core ledger client; https only; credentials from `LEDGER_API_URL` and `LEDGER_API_KEY` |

`ledger:` in `vsl.yaml` selects `jsonl`, `remote` or `memory`.

### Two different questions

- `verify` asks whether records were edited. It recomputes the hash chain, the links and
  the sequence through VSL-Core.
- `audit` asks whether the records describe a governed process. It runs the five VSL
  audit checks and validates `caused_by`.

Neither answers the other's question. `audit` does not detect tampering. `verify` does not
detect truncation (see below).

### CLI

```bash
vsl verify  --ledger ledger.jsonl [--checkpoint cp.json] [--write-checkpoint cp.json]
vsl audit   --ledger ledger.jsonl [--max-monitor-gap SECONDS]
vsl certify --ledger ledger.jsonl --max-monitor-gap SECONDS [--checkpoint cp.json]
```

| Exit | Meaning |
|---|---|
| 0 | everything requested passed |
| 1 | a check failed (integrity, audit, causal chain, checkpoint, or certification requirements) |
| 2 | bad arguments or checkpoint file; refusing to overwrite a checkpoint |
| 3 | the ledger could not be read (missing, malformed, structurally invalid) |

The CLI only reads ledgers. It never creates, repairs or rewrites them. Without
`--max-monitor-gap`, `audit` marks check 1 as SKIPPED rather than passing it. `certify`
requires the threshold, a non-empty ledger and no failures, and the certificate is issued
by VSL-Core.

### Truncation and checkpoints

A hash chain cut short is still a valid chain, so `verify_integrity()` cannot detect
truncation (Specification, Known constraint 5). To detect it, anchor a checkpoint
somewhere the ledger writer cannot modify:

```bash
vsl verify --ledger ledger.jsonl --write-checkpoint cp.json   # after a clean verify; never overwrites
vsl verify --ledger ledger.jsonl --checkpoint cp.json         # later: fails if the ledger is shorter or differs
```

A checkpoint file holds exactly the three `LedgerCheckpoint` fields (`sequence`,
`entry_hash`, `checked_at`). Its value comes entirely from where you store it. A copy kept
beside the ledger gives no protection against someone who can edit both.

In Python: `Governance.verify_integrity()`, `.audit()`, `.validate_caused_by()`,
`.checkpoint()`, `.verify_checkpoint(cp)`, `.certify(max_monitor_gap_seconds=...)`.

## Known limitations

- The policy hash is not recorded in the ledger. VSL-Core has no entry type for it, and none
  was invented.
- There is no `--remote` option on the CLI. The CLI reads JSONL files. Remote ledgers are
  used from Python.
- The JSONL store re-reads the file on each operation, so cost grows with ledger size.
- Concurrent writers are protected only by VSL-Core's own file locking.
- Not implemented: framework adapters, a codebase scan, `vsl init`, and a testing-framework
  package.

## Tests

```bash
python -m pytest -q
```

The 84 original and Phase 2.5 tests are unchanged. Remote-ledger tests use an in-process
fake API over `httpx.MockTransport`, backed by a real in-memory VSL-Core store. They need
`httpx` and the ledger client and are skipped if those are not installed. Nothing has been
run against a live remote ledger service.
