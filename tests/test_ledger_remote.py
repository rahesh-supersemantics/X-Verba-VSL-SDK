"""
Phase 4 — remote ledger backend.

Uses the real vsl_core_ledger_client.RemoteLedgerStore over an httpx
MockTransport that fronts a real VSL-Core InMemoryLedgerStore, so
hash-chain sealing is genuine; only the network is faked.

Skipped (not failed) when the optional httpx / vsl-core-ledger-client
packages are not installed.
"""

from __future__ import annotations

import json
import sys

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("vsl_core_ledger_client")

from vsl import (  # noqa: E402
    Governance,
    LedgerConfig,
    LedgerStorageError,
    build_ledger,
    remote_ledger,
)
from vsl.ledger import ENV_LEDGER_KEY, ENV_LEDGER_URL  # noqa: E402

from governance.policy import build_policy  # noqa: E402
from ledger_helpers import AGENT, entries, ok_ctx  # noqa: E402
from vsl_core.ledger import (  # noqa: E402
    GENESIS_HASH,
    InMemoryLedgerStore,
    LedgerEntry,
    LedgerEntryType,
    VerbaLedger,
)


def monitor(ledger: VerbaLedger, n: int = 1):
    for _ in range(n):
        ledger.write_monitor(
            identity_key=AGENT,
            instance_id="i",
            drift_detected=False,
        )


# ================================================================
# REMOTE (real RemoteLedgerStore, faked HTTP transport)
# ================================================================

API_KEY = "test-key"
BASE_URL = "https://ledger.example.com"


class FakeLedgerApi:
    """An in-process ledger-api: POST /v1/entries, GET /v1/entries."""

    def __init__(self) -> None:
        self.store = InMemoryLedgerStore()
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text="boom")

        if request.headers.get("X-API-Key") != API_KEY:
            return httpx.Response(401, text="unauthorised")

        if request.method == "POST" and request.url.path == "/v1/entries":
            body = json.loads(request.content)

            sealed = self.store.append(
                LedgerEntry(
                    entry_type=LedgerEntryType(body["entry_type"]),
                    identity_key=body["identity_key"],
                    cluster_key=body.get("cluster_key"),
                    instance_id=body.get("instance_id"),
                    decision_id=body.get("decision_id"),
                    caused_by=body.get("caused_by"),
                    schema_version=body.get("schema_version"),
                    payload=body.get("payload", {}),
                )
            )

            return httpx.Response(200, json=sealed.to_dict())

        if request.method == "GET" and request.url.path == "/v1/entries":
            params = request.url.params
            limit = int(params.get("limit", 100))
            after = params.get("after")
            identity = params.get("identity_key")

            matching = [
                e
                for e in self.store.all_entries()
                if (identity is None or e.identity_key == identity)
                and (after is None or e.sequence > int(after))
            ]

            return httpx.Response(
                200, json=[e.to_dict() for e in matching[:limit]]
            )

        return httpx.Response(404, text="not found")

    def client(self) -> "httpx.Client":
        return httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": API_KEY},
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def api() -> FakeLedgerApi:
    return FakeLedgerApi()


def remote(api: FakeLedgerApi, **kwargs) -> VerbaLedger:
    return remote_ledger(
        base_url=BASE_URL,
        api_key=API_KEY,
        client=api.client(),
        **kwargs,
    )


def test_remote_ledger_wraps_the_real_remote_ledger_store(api):
    from vsl_core_ledger_client import RemoteLedgerStore

    ledger = remote(api)

    assert isinstance(ledger, VerbaLedger)
    assert isinstance(ledger.store, RemoteLedgerStore)


def test_remote_ledger_round_trips_a_real_hash_chain(api):
    ledger = remote(api)

    monitor(ledger, 3)

    chain = list(ledger.store.all_entries())

    assert [e.sequence for e in chain] == [0, 1, 2]
    assert chain[0].prev_hash == GENESIS_HASH
    assert ledger.verify_integrity() is True
    assert ledger.current_checkpoint().sequence == 2


def test_remote_ledger_sends_the_api_key_header_only(api):
    ledger = remote(api)

    monitor(ledger, 1)

    request = api.requests[0]

    assert request.headers["X-API-Key"] == API_KEY
    assert API_KEY not in str(request.url)


def test_remote_ledger_paginates(api):
    ledger = remote(api, page_size=2)

    monitor(ledger, 5)

    assert [e.sequence for e in ledger.store.all_entries()] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_governance_runs_end_to_end_over_the_remote_store(api):
    governance = Governance(policy=build_policy(), ledger=remote(api))

    await governance.authorize("approve_expense", ok_ctx())
    await governance.authorize("approve_expense", ok_ctx(confidence=0.51))

    assert governance.verify_integrity() is True
    assert governance.audit().all_passed
    assert governance.validate_caused_by().valid
    assert len(entries(governance)) == 6


@pytest.mark.asyncio
async def test_remote_failure_propagates_and_blocks_the_effect(api):
    from vsl_core_ledger_client import RemoteLedgerStoreError

    governance = Governance(policy=build_policy(), ledger=remote(api))
    api.fail_with = 503
    calls: list[str] = []

    with pytest.raises(RemoteLedgerStoreError):
        await governance.run(
            "approve_expense", ok_ctx(), effect=lambda: calls.append("paid")
        )

    assert calls == []


def test_remote_bad_credentials_raise_the_stores_own_error(api):
    from vsl_core_ledger_client import RemoteLedgerStoreError

    ledger = remote_ledger(
        base_url=BASE_URL,
        api_key="wrong",
        client=httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": "wrong"},
            transport=httpx.MockTransport(api.handler),
        ),
    )

    with pytest.raises(RemoteLedgerStoreError):
        monitor(ledger, 1)


def test_remote_reads_credentials_from_the_environment():
    ledger = remote_ledger(
        env={ENV_LEDGER_URL: BASE_URL, ENV_LEDGER_KEY: API_KEY}
    )

    assert ledger.store._base_url == BASE_URL


@pytest.mark.parametrize(
    "env, name",
    [
        ({ENV_LEDGER_KEY: "k"}, ENV_LEDGER_URL),
        ({ENV_LEDGER_URL: BASE_URL}, ENV_LEDGER_KEY),
        ({}, ENV_LEDGER_URL),
        ({ENV_LEDGER_URL: "", ENV_LEDGER_KEY: "k"}, ENV_LEDGER_URL),
    ],
)
def test_remote_requires_url_and_key(env, name):
    with pytest.raises(LedgerStorageError, match=name):
        remote_ledger(env=env)


@pytest.mark.parametrize(
    "url", ["http://ledger.example.com", "ftp://x", "ledger.example.com"]
)
def test_remote_requires_https(url):
    with pytest.raises(LedgerStorageError, match="https"):
        remote_ledger(base_url=url, api_key="k")


def test_remote_errors_never_contain_the_api_key():
    with pytest.raises(LedgerStorageError) as info:
        remote_ledger(base_url="http://insecure.example.com", api_key="s3cret-key")

    assert "s3cret-key" not in str(info.value)


def test_remote_without_the_client_package_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "vsl_core_ledger_client", None)

    with pytest.raises(LedgerStorageError, match="vsl-core-ledger-client"):
        remote_ledger(base_url=BASE_URL, api_key="k")


# ================================================================
# SELECTION
# ================================================================


def test_build_ledger_selects_the_remote_backend():
    from vsl_core_ledger_client import RemoteLedgerStore

    built = build_ledger(
        LedgerConfig("remote"),
        env={ENV_LEDGER_URL: BASE_URL, ENV_LEDGER_KEY: API_KEY},
    )

    assert isinstance(built.store, RemoteLedgerStore)


def test_build_ledger_remote_without_credentials_fails(monkeypatch):
    monkeypatch.delenv(ENV_LEDGER_URL, raising=False)
    monkeypatch.delenv(ENV_LEDGER_KEY, raising=False)

    with pytest.raises(LedgerStorageError, match=ENV_LEDGER_URL):
        build_ledger(LedgerConfig("remote"), env={})


def test_policy_ledger_section_selects_remote_backend(tmp_path):
    from test_policy_yaml import base_policy, dump, write
    from vsl_core_ledger_client import RemoteLedgerStore

    data = base_policy()
    data["ledger"] = {"backend": "remote"}

    governance = Governance.from_yaml(
        write(tmp_path, dump(data)),
        env={ENV_LEDGER_URL: BASE_URL, ENV_LEDGER_KEY: API_KEY},
    )

    assert isinstance(governance.ledger.store, RemoteLedgerStore)


def test_policy_file_cannot_carry_remote_credentials(tmp_path):
    from test_policy_yaml import base_policy, dump, write
    from vsl import PolicyConfigError

    data = base_policy()
    data["ledger"] = {"backend": "remote", "api_key": "hunter2"}

    with pytest.raises(PolicyConfigError, match="Forbidden key"):
        Governance.from_yaml(write(tmp_path, dump(data)), env={})
