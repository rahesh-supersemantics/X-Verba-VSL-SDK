"""
Phase 3 — YAML policy.

Real VSL-Core everywhere: nothing on the compile path is mocked.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from vsl import (
    CompiledPolicy,
    Governance,
    Outcome,
    PolicyConfigError,
    PolicyRegistry,
    PolicySchemaError,
    PolicyValidationError,
    Registry,
    RegistryError,
    compile_policy,
    default_registry,
    load_policy,
)
from vsl.config import (
    SCHEMA_PATH,
    interpolate_env,
    load_policy_config,
    load_schema,
    parse_policy_config,
)
from vsl.registry import confidence_gamma, max_value

from vsl_core.constructs import (
    Fallback,
    Invariant,
    PreNode,
    TerminalState,
)
from vsl_core.ledger import (
    InMemoryLedgerStore,
    JsonlLedgerStore,
    LedgerEntryType,
    VerbaLedger,
)
from vsl_core.metrics import (
    AssuranceBasis,
    AssuranceLevel,
    GammaEstimate,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "vsl.yaml"


# ================================================================
# HELPERS
# ================================================================


def base_policy() -> dict:
    return {
        "schema_version": 1,
        "agent": {"name": "expense-agent"},
        "policy": {"name": "expense-policy", "version": "1.0.0"},
        "assurance_bases": {
            "output_layer": {
                "f1_pre_commitment": True,
                "f2_modification": "NONE",
            }
        },
        "terminal_states": [
            {
                "name": "expense-approvals-suspended",
                "description": "Halted.",
                "entry_conditions": ["expense-cap violated"],
            }
        ],
        "prenodes": [
            {
                "name": "approval-confidence",
                "description": "Confidence gate.",
                "monitor": "confidence_gamma",
                "params": {
                    "decision_threshold": 0.5,
                    "calibration_error": 0.1,
                },
                "assurance_basis": "output_layer",
                "gamma_threshold": 1.1,
                "fallback": "human_review",
            }
        ],
        "invariants": [
            {
                "name": "expense-cap",
                "description": "Cap.",
                "rule": "max_value",
                "params": {"field": "amount", "max": 2000},
                "assurance_basis": "output_layer",
                "on_violation": "expense-approvals-suspended",
            }
        ],
        "actions": {
            "approve_expense": {
                "prenodes": ["approval-confidence"],
                "invariants": ["expense-cap"],
            }
        },
        "ledger": {"backend": "memory"},
    }


def dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def parse(data: dict, env: dict | None = None):
    return parse_policy_config(dump(data), env=env or {})


def mutated(fn) -> dict:
    data = copy.deepcopy(base_policy())
    fn(data)
    return data


def write(tmp_path: Path, text: str, name: str = "vsl.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def ok_ctx(confidence=0.95, amount=150) -> dict:
    return {"confidence": confidence, "amount": amount, "currency": "NZD"}


def entries(governance: Governance) -> list:
    return list(governance.ledger.store.all_entries())


# ================================================================
# YAML LOADING
# ================================================================


def test_loads_valid_policy_file(tmp_path):
    path = write(tmp_path, dump(base_policy()))

    config = load_policy_config(path, env={})

    assert config.data["policy"]["name"] == "expense-policy"
    assert config.source == str(path)
    assert len(config.policy_hash) == 64


def test_example_policy_loads_and_compiles():
    policy = load_policy(EXAMPLE, env={})

    assert isinstance(policy, CompiledPolicy)
    assert policy.name == "expense-policy"
    assert "approve_expense" in policy.actions


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(PolicyConfigError, match="not found"):
        load_policy_config(tmp_path / "nope.yaml")


def test_directory_is_config_error(tmp_path):
    with pytest.raises(PolicyConfigError, match="not a file"):
        load_policy_config(tmp_path)


def test_malformed_yaml_is_config_error(tmp_path):
    path = write(tmp_path, "schema_version: 1\nagent: [unclosed\n")

    with pytest.raises(PolicyConfigError, match="Malformed YAML"):
        load_policy_config(path, env={})


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "{}"])
def test_empty_configuration_is_config_error(tmp_path, text):
    with pytest.raises(PolicyConfigError, match="empty"):
        load_policy_config(write(tmp_path, text), env={})


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
def test_non_mapping_top_level_is_config_error(tmp_path, text):
    with pytest.raises(PolicyConfigError, match="mapping"):
        load_policy_config(write(tmp_path, text), env={})


def test_invalid_structure_is_schema_error(tmp_path):
    data = base_policy()
    data["surprise"] = {"x": 1}

    with pytest.raises(PolicySchemaError, match="surprise"):
        load_policy_config(write(tmp_path, dump(data)), env={})


def test_duplicate_keys_are_rejected(tmp_path):
    text = dump(base_policy()).replace(
        "max: 2000", "max: 2000\n    max: 999999"
    )

    with pytest.raises(PolicyConfigError, match="Duplicate key"):
        load_policy_config(write(tmp_path, text), env={})


def test_non_utf8_file_is_config_error(tmp_path):
    path = tmp_path / "vsl.yaml"
    path.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(PolicyConfigError, match="UTF-8"):
        load_policy_config(path, env={})


def test_oversized_file_is_config_error(tmp_path):
    path = write(tmp_path, "# " + "x" * (1024 * 1024 + 10))

    with pytest.raises(PolicyConfigError, match="too large"):
        load_policy_config(path, env={})


def test_non_string_key_is_config_error():
    with pytest.raises(PolicyConfigError, match="Non-string key"):
        parse_policy_config("1: a\n", env={})


# ================================================================
# SCHEMA VALIDATION
# ================================================================


def test_schema_file_is_a_valid_json_schema():
    Draft202012Validator.check_schema(load_schema())

    assert SCHEMA_PATH.name == "schema.json"


def test_valid_configuration_passes_schema():
    assert parse(base_policy()).data["schema_version"] == 1


@pytest.mark.parametrize(
    "section",
    [
        "schema_version",
        "agent",
        "policy",
        "assurance_bases",
        "terminal_states",
        "actions",
    ],
)
def test_missing_required_section(section):
    data = base_policy()
    del data[section]

    with pytest.raises(PolicySchemaError, match=section):
        parse(data)


@pytest.mark.parametrize(
    "fn, fragment",
    [
        (lambda d: d["agent"].pop("name"), "name"),
        (lambda d: d["policy"].pop("version"), "version"),
        (lambda d: d["prenodes"][0].pop("monitor"), "monitor"),
        (lambda d: d["prenodes"][0].pop("fallback"), "fallback"),
        (lambda d: d["invariants"][0].pop("rule"), "rule"),
        (lambda d: d["invariants"][0].pop("on_violation"), "on_violation"),
        (lambda d: d["terminal_states"][0].pop("description"), "description"),
        (lambda d: d["ledger"].pop("backend"), "backend"),
        (lambda d: d["assurance_bases"]["output_layer"].pop("f1_pre_commitment"), "f1_pre_commitment"),
    ],
)
def test_missing_required_fields(fn, fragment):
    with pytest.raises(PolicySchemaError, match=fragment):
        parse(mutated(fn))


@pytest.mark.parametrize(
    "fn, fragment",
    [
        (lambda d: d["policy"].__setitem__("version", 1.0), "version"),
        (lambda d: d["assurance_bases"]["output_layer"].__setitem__("f1_pre_commitment", "yes"), "f1_pre_commitment"),
        (lambda d: d["prenodes"][0].__setitem__("gamma_threshold", "high"), "gamma_threshold"),
        (lambda d: d["prenodes"][0].__setitem__("gamma_threshold", -1), "gamma_threshold"),
        (lambda d: d["prenodes"][0].__setitem__("gamma_threshold", 0), "gamma_threshold"),
        (lambda d: d["terminal_states"][0].__setitem__("entry_conditions", "oops"), "entry_conditions"),
        (lambda d: d.__setitem__("actions", ["approve_expense"]), "actions"),
        (lambda d: d["ledger"].__setitem__("fsync", "sometimes"), "fsync"),
        (lambda d: d.__setitem__("schema_version", "1"), "schema_version"),
    ],
)
def test_invalid_field_types(fn, fragment):
    with pytest.raises(PolicySchemaError, match=fragment):
        parse(mutated(fn))


@pytest.mark.parametrize(
    "fn, fragment",
    [
        (lambda d: d.__setitem__("schema_version", 2), "schema_version"),
        (lambda d: d["assurance_bases"]["output_layer"].__setitem__("f2_modification", "BOGUS"), "f2_modification"),
        (lambda d: d["ledger"].__setitem__("backend", "sqlite"), "backend"),
        (lambda d: d["prenodes"][0].__setitem__("fallback", "auto_approve"), "fallback"),
        (lambda d: d["policy"].__setitem__("name", "has space"), "name"),
        (lambda d: d["agent"].__setitem__("name", "${AGENT}"), "name"),
    ],
)
def test_unsupported_values(fn, fragment):
    with pytest.raises(PolicySchemaError, match=fragment):
        parse(mutated(fn))


def test_action_with_two_prenodes_is_rejected_by_schema():
    def fn(d):
        d["actions"]["approve_expense"]["prenodes"] = ["a", "b"]

    with pytest.raises(PolicySchemaError, match="prenodes"):
        parse(mutated(fn))


@pytest.mark.parametrize("bad", [[1, 2], {"a": 1}])
def test_params_must_be_scalars(bad):
    def fn(d):
        d["invariants"][0]["params"]["extra"] = bad

    with pytest.raises(PolicySchemaError, match="params"):
        parse(mutated(fn))


def test_schema_errors_report_every_problem():
    def fn(d):
        d["policy"]["version"] = 5
        d["ledger"]["backend"] = "nope"

    with pytest.raises(PolicySchemaError) as info:
        parse(mutated(fn))

    assert len(info.value.errors) >= 2


@pytest.mark.parametrize("level_key", ["level", "assurance_level"])
def test_assurance_level_cannot_be_set(level_key):
    def fn(d):
        d["assurance_bases"]["output_layer"][level_key] = "HIGH"

    with pytest.raises(PolicyValidationError, match="cannot be set"):
        parse(mutated(fn))


@pytest.mark.parametrize(
    "key", ["api_key", "password", "secret", "token", "API_KEY", "Api-Key"]
)
def test_secret_keys_are_rejected_anywhere(key):
    def fn(d):
        d["ledger"][key] = "hunter2"

    with pytest.raises(PolicyConfigError, match="Forbidden key"):
        parse(mutated(fn))


def test_secret_key_rejected_when_nested_in_params():
    def fn(d):
        d["prenodes"][0]["params"]["token"] = "abc"

    with pytest.raises(PolicyConfigError, match="Forbidden key"):
        parse(mutated(fn))


# ================================================================
# POLICY VALIDATION RULES (relationships)
# ================================================================


@pytest.mark.parametrize(
    "fn, fragment",
    [
        (lambda d: d["prenodes"][0].__setitem__("assurance_basis", "ghost"), "unknown assurance basis"),
        (lambda d: d["invariants"][0].__setitem__("assurance_basis", "ghost"), "unknown assurance basis"),
        (lambda d: d["invariants"][0].__setitem__("on_violation", "ghost"), "not a declared terminal state"),
        (lambda d: d["actions"]["approve_expense"].__setitem__("prenodes", ["ghost"]), "unknown prenode"),
        (lambda d: d["actions"]["approve_expense"].__setitem__("invariants", ["ghost"]), "unknown invariant"),
        (lambda d: d["actions"].__setitem__("empty_action", {}), "declares no gates"),
        (lambda d: d["terminal_states"].append(dict(d["terminal_states"][0])), "Duplicate terminal state"),
        (lambda d: d["invariants"].append(dict(d["invariants"][0])), "Duplicate invariant"),
        (lambda d: d["prenodes"].append(dict(d["prenodes"][0])), "Duplicate prenode"),
        (lambda d: d["ledger"].update(backend="jsonl"), "ledger.path: required"),
        (lambda d: d["ledger"].update(path="x.jsonl"), "only valid when"),
        (lambda d: d["ledger"].update(backend="remote", fsync=True), "only valid when"),
    ],
)
def test_policy_relationship_rules(fn, fragment):
    with pytest.raises(PolicyValidationError, match=fragment):
        parse(mutated(fn))


def test_invariant_without_terminal_state_is_rejected():
    def fn(d):
        del d["invariants"][0]["on_violation"]

    with pytest.raises(PolicySchemaError, match="on_violation"):
        parse(mutated(fn))


def test_relationship_errors_are_all_reported():
    def fn(d):
        d["invariants"][0]["on_violation"] = "ghost"
        d["prenodes"][0]["assurance_basis"] = "ghost"

    with pytest.raises(PolicyValidationError) as info:
        parse(mutated(fn))

    assert len(info.value.errors) == 2


def test_action_with_only_invariants_has_no_prenode():
    def fn(d):
        d["actions"]["approve_expense"].pop("prenodes")

    policy = compile_policy(parse(mutated(fn)))

    assert policy.actions["approve_expense"].pre_node is None


# ================================================================
# ENVIRONMENT INTERPOLATION
# ================================================================


def test_interpolates_existing_variable():
    assert interpolate_env("${A}", {"A": "hello"}) == "hello"


def test_missing_variable_without_default_raises():
    with pytest.raises(PolicyConfigError, match="MISSING_VAR"):
        interpolate_env("${MISSING_VAR}", {})


def test_missing_variable_error_does_not_leak_other_values():
    with pytest.raises(PolicyConfigError) as info:
        interpolate_env("${MISSING_VAR}", {"OTHER": "s3cret"})

    assert "s3cret" not in str(info.value)


def test_missing_variable_uses_default():
    assert interpolate_env("${A:-fallback}", {}) == "fallback"


def test_empty_variable_counts_as_missing():
    with pytest.raises(PolicyConfigError, match="unset or empty"):
        interpolate_env("${A}", {"A": ""})

    assert interpolate_env("${A:-dflt}", {"A": ""}) == "dflt"


def test_explicit_empty_default_is_allowed():
    assert interpolate_env("${A:-}", {}) == ""


def test_set_variable_beats_default():
    assert interpolate_env("${A:-dflt}", {"A": "real"}) == "real"


def test_multiple_interpolations_in_one_value():
    assert (
        interpolate_env("${A}/${B:-x}/${C}", {"A": "1", "C": "3"})
        == "1/x/3"
    )


def test_substituted_text_is_not_expanded_again():
    assert (
        interpolate_env("${A}", {"A": "${B}", "B": "boom"}) == "${B}"
    )


@pytest.mark.parametrize("bad", ["${", "${1BAD}", "${A", "pre ${ post", "${A:-x"])
def test_invalid_interpolation_syntax_raises(bad):
    with pytest.raises(PolicyConfigError, match="syntax"):
        interpolate_env(bad, {"A": "1"})


def test_plain_text_is_untouched():
    assert interpolate_env("no variables here", {}) == "no variables here"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.75", 0.75),
        ("2000", 2000),
        ("-3", -3),
        ("true", True),
        ("false", False),
        ("1e3", 1000.0),
        ("nan", "nan"),
        ("inf", "inf"),
        ("[1, 2]", "[1, 2]"),
        ("{a: 1}", "{a: 1}"),
        ("True", "True"),
        ("0x10", "0x10"),
        ("hello", "hello"),
    ],
)
def test_whole_value_coercion_is_narrow(raw, expected):
    result = interpolate_env("${V}", {"V": raw}, coerce=True)

    assert result == expected
    assert type(result) is type(expected)


def test_mixed_text_stays_a_string_even_with_coerce():
    assert interpolate_env("${V}0", {"V": "1"}, coerce=True) == "10"


def test_uses_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("VSL_TEST_VAR", "from-os")

    assert interpolate_env("${VSL_TEST_VAR}") == "from-os"


def test_interpolation_applies_to_params_and_ledger():
    data = base_policy()
    data["prenodes"][0]["params"]["decision_threshold"] = "${T:-0.25}"
    data["invariants"][0]["params"]["max"] = "${CAP}"
    data["ledger"] = {"backend": "jsonl", "path": "${LEDGER_DIR}/l.jsonl", "fsync": "${FSYNC:-true}"}

    config = parse(data, {"CAP": "500", "LEDGER_DIR": "/var/vsl"})

    assert config.data["prenodes"][0]["params"]["decision_threshold"] == 0.25
    assert config.data["invariants"][0]["params"]["max"] == 500
    assert config.data["ledger"]["path"] == "/var/vsl/l.jsonl"
    assert config.data["ledger"]["fsync"] is True


def test_ledger_path_is_never_coerced_to_a_number():
    data = base_policy()
    data["ledger"] = {"backend": "jsonl", "path": "${P}"}

    assert parse(data, {"P": "2024"}).data["ledger"]["path"] == "2024"


def test_interpolation_is_not_applied_to_descriptions():
    def fn(d):
        d["prenodes"][0]["description"] = "literal ${NOT_EXPANDED} stays"

    config = parse(mutated(fn))

    assert "${NOT_EXPANDED}" in config.data["prenodes"][0]["description"]


def test_missing_variable_in_params_fails_load():
    def fn(d):
        d["invariants"][0]["params"]["max"] = "${CAP_NOT_SET}"

    with pytest.raises(PolicyConfigError, match="CAP_NOT_SET"):
        parse(mutated(fn))


def test_environment_value_that_is_not_a_number_is_rejected_by_the_construct():
    def fn(d):
        d["invariants"][0]["params"]["max"] = "${CAP}"

    config = parse(mutated(fn), {"CAP": "lots"})

    with pytest.raises(PolicyValidationError, match="max"):
        compile_policy(config)


def test_policy_hash_reflects_interpolated_values():
    def fn(d):
        d["invariants"][0]["params"]["max"] = "${CAP:-2000}"

    one = parse(mutated(fn), {})
    two = parse(mutated(fn), {"CAP": "3000"})
    again = parse(mutated(fn), {})

    assert one.policy_hash == again.policy_hash
    assert one.policy_hash != two.policy_hash


# ================================================================
# MONITOR / RULE REGISTRY
# ================================================================


def test_default_registry_has_only_builtins():
    registry = default_registry()

    assert registry.monitors == ("confidence_gamma",)
    assert registry.rules == ("max_value",)


def test_default_registry_is_not_shared_global_state():
    first, second = default_registry(), default_registry()

    first.register_rule("only_in_first", lambda: None)

    assert "only_in_first" not in second.rules


def test_register_and_resolve_custom_rule():
    registry = default_registry()

    @registry.rule("always_ok")
    def always_ok():
        async def rule(ctx):
            return True

        return rule

    assert registry.get_rule("always_ok") is always_ok


def test_register_and_resolve_custom_monitor():
    registry = default_registry()
    registry.register_monitor("m", lambda: None)

    assert callable(registry.get_monitor("m"))


def test_duplicate_registration_raises():
    registry = default_registry()

    with pytest.raises(RegistryError, match="already registered"):
        registry.register_rule("max_value", lambda **k: None)

    with pytest.raises(RegistryError, match="already registered"):
        registry.register_monitor("confidence_gamma", lambda **k: None)


def test_same_name_may_exist_as_monitor_and_rule():
    registry = Registry()
    registry.register_monitor("shared", lambda: None)
    registry.register_rule("shared", lambda: None)


@pytest.mark.parametrize("name", ["", "has space", "$bad", None, 5])
def test_invalid_registry_name_raises(name):
    with pytest.raises(RegistryError, match="Invalid registry name"):
        Registry().register_rule(name, lambda: None)


def test_non_callable_factory_raises():
    with pytest.raises(RegistryError, match="callable"):
        Registry().register_rule("r", "not callable")


def test_unknown_monitor_and_rule_raise():
    registry = default_registry()

    with pytest.raises(RegistryError, match="Unknown monitor"):
        registry.get_monitor("nope")

    with pytest.raises(RegistryError, match="Unknown rule"):
        registry.get_rule("nope")


def test_unknown_monitor_in_policy_is_registry_error():
    def fn(d):
        d["prenodes"][0]["monitor"] = "made_up"

    with pytest.raises(RegistryError, match="made_up"):
        compile_policy(parse(mutated(fn)))


def test_unknown_rule_in_policy_is_registry_error():
    def fn(d):
        d["invariants"][0]["rule"] = "made_up"

    with pytest.raises(RegistryError, match="made_up"):
        compile_policy(parse(mutated(fn)))


def test_policy_can_use_custom_registered_rule():
    registry = default_registry()

    @registry.rule("business_hours_only")
    def business_hours_only(*, start, end):
        async def check(ctx):
            return start <= ctx["hour"] < end

        return check

    def fn(d):
        d["invariants"][0]["rule"] = "business_hours_only"
        d["invariants"][0]["params"] = {"start": 9, "end": 17}

    policy = compile_policy(parse(mutated(fn)), registry)

    assert isinstance(policy.invariant("expense-cap"), Invariant)


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({}, "decision_threshold"),
        ({"decision_threshold": 0.5, "surprise": 1}, "surprise"),
        ({"decision_threshold": 1.5}, "between 0 and 1"),
        ({"decision_threshold": 0}, "between 0 and 1"),
        ({"decision_threshold": "high"}, "finite number"),
        ({"decision_threshold": True}, "finite number"),
        ({"decision_threshold": 0.5, "calibration_error": -0.1}, ">= 0"),
        ({"decision_threshold": 0.5, "field": ""}, "field"),
    ],
)
def test_bad_monitor_params_are_policy_validation_errors(params, fragment):
    def fn(d):
        d["prenodes"][0]["params"] = params

    with pytest.raises(PolicyValidationError, match=fragment):
        compile_policy(parse(mutated(fn)))


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({}, "field"),
        ({"field": "amount"}, "max"),
        ({"field": "amount", "max": "lots"}, "finite number"),
        ({"field": "amount", "max": 10, "surprise": 1}, "surprise"),
        ({"field": "", "max": 10}, "field"),
    ],
)
def test_bad_rule_params_are_policy_validation_errors(params, fragment):
    def fn(d):
        d["invariants"][0]["params"] = params

    with pytest.raises(PolicyValidationError, match=fragment):
        compile_policy(parse(mutated(fn)))


# ================================================================
# BUILT-IN MONITOR / RULE BEHAVIOUR
# ================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("p", [0.01, 0.3, 0.51, 0.55, 0.75, 0.95, 0.999999])
async def test_confidence_gamma_matches_the_programmatic_policy(p):
    from governance import policy as reference

    estimate = await confidence_gamma(decision_threshold=0.5)(
        {"confidence": p}
    )
    expected = reference.confidence_gamma(p)

    assert estimate.gamma_hat == pytest.approx(expected.gamma_hat)
    assert estimate.delta_estimation_error == expected.delta_estimation_error
    assert estimate.energy_gap_estimate == pytest.approx(
        expected.energy_gap_estimate
    )


@pytest.mark.asyncio
async def test_confidence_gamma_uses_boundary_odds_not_half():
    """Spec 7.1: with a boundary of 0.25, p=0.3 is a real positive.
    Dividing by 0.5 would deny it."""

    estimate = await confidence_gamma(decision_threshold=0.25)(
        {"confidence": 0.3}
    )

    assert estimate.sufficient(1.1)

    naive = (0.3 / 0.7) / (0.5 / 0.5)

    assert naive < 1.1


@pytest.mark.asyncio
async def test_confidence_gamma_custom_field_and_error():
    monitor = confidence_gamma(
        decision_threshold=0.5, calibration_error=0.2, field="score"
    )

    estimate = await monitor({"score": 0.9})

    assert estimate.delta_estimation_error == 0.2


@pytest.mark.asyncio
async def test_confidence_gamma_missing_field_raises():
    with pytest.raises(KeyError):
        await confidence_gamma(decision_threshold=0.5)({"amount": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0.9", True, None, [0.9]])
async def test_confidence_gamma_rejects_non_numbers(bad):
    with pytest.raises(TypeError):
        await confidence_gamma(decision_threshold=0.5)(
            {"confidence": bad}
        )


@pytest.mark.asyncio
async def test_confidence_gamma_nan_is_never_sufficient():
    estimate = await confidence_gamma(decision_threshold=0.5)(
        {"confidence": math.nan}
    )

    assert not estimate.sufficient(1.1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount, expected",
    [(0, True), (1999.99, True), (2000, True), (2000.01, False), (5000, False), (-5, True)],
)
async def test_max_value_boundary(amount, expected):
    rule = max_value(field="amount", max=2000)

    assert await rule({"amount": amount}) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [math.nan])
async def test_max_value_nan_is_a_violation(amount):
    assert await max_value(field="amount", max=2000)({"amount": amount}) is False


@pytest.mark.asyncio
async def test_max_value_infinity_is_a_violation():
    assert await max_value(field="amount", max=2000)({"amount": math.inf}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["100", True, None, [1]])
async def test_max_value_rejects_non_numbers(bad):
    with pytest.raises(TypeError):
        await max_value(field="amount", max=2000)({"amount": bad})


@pytest.mark.asyncio
async def test_max_value_missing_field_raises():
    with pytest.raises(KeyError):
        await max_value(field="amount", max=2000)({"other": 1})


# ================================================================
# COMPILATION TO REAL VSL-CORE CONSTRUCTS
# ================================================================


def compiled() -> CompiledPolicy:
    return compile_policy(parse(base_policy()))


def test_compiles_to_real_vsl_core_classes():
    policy = compiled()

    assert isinstance(policy.pre_node("approval-confidence"), PreNode)
    assert isinstance(policy.invariant("expense-cap"), Invariant)
    assert isinstance(
        policy.terminal_state("expense-approvals-suspended"),
        TerminalState,
    )
    assert isinstance(
        policy.assurance_bases["output_layer"], AssuranceBasis
    )


def test_prenode_is_built_from_the_yaml_values():
    pre_node = compiled().pre_node("approval-confidence")

    assert pre_node.name == "approval-confidence"
    assert pre_node.gamma_threshold == 1.1
    assert pre_node.fallback == Fallback(
        on_failure="ROUTE_TO_HUMAN_QUEUE", max_retries=0
    )
    assert callable(pre_node.monitor)


def test_gamma_threshold_defaults_to_vsl_core_default():
    from vsl_core.metrics import ROBUST_GAMMA_DEFAULT_THRESHOLD

    def fn(d):
        del d["prenodes"][0]["gamma_threshold"]

    pre_node = compile_policy(parse(mutated(fn))).pre_node("approval-confidence")

    assert pre_node.gamma_threshold == ROBUST_GAMMA_DEFAULT_THRESHOLD


def test_invariant_points_at_its_terminal_state():
    policy = compiled()

    assert (
        policy.invariant("expense-cap").on_violation
        is policy.terminal_state("expense-approvals-suspended")
    )


def test_terminal_state_carries_entry_conditions():
    state = compiled().terminal_state("expense-approvals-suspended")

    assert state.entry_conditions == ("expense-cap violated",)
    assert state.requires_human_authorised_transition is True


def test_assurance_level_is_derived_by_vsl_core():
    policy = compiled()

    assert policy.pre_node("approval-confidence").assurance_level is AssuranceLevel.LOW
    assert policy.invariant("expense-cap").assurance_level is AssuranceLevel.LOW


@pytest.mark.parametrize(
    "f1, f2, level",
    [
        (True, "FULL", AssuranceLevel.HIGH),
        (True, "PARTIAL", AssuranceLevel.MEDIUM),
        (True, "INDIRECT", AssuranceLevel.MEDIUM),
        (True, "NONE", AssuranceLevel.LOW),
        (False, "FULL", AssuranceLevel.LOW),
    ],
)
def test_levels_follow_the_spec_derivation_table(f1, f2, level):
    def fn(d):
        d["assurance_bases"]["output_layer"] = {
            "f1_pre_commitment": f1,
            "f2_modification": f2,
        }

    pre_node = compile_policy(parse(mutated(fn))).pre_node("approval-confidence")

    assert pre_node.assurance_level is level


def test_actions_bind_the_right_gates():
    action = compiled().actions["approve_expense"]

    assert action.pre_node.name == "approval-confidence"
    assert [i.name for i in action.invariants] == ["expense-cap"]


def test_agent_defaults_to_name_and_identity_key_overrides():
    assert compiled().agent == "expense-agent"

    def fn(d):
        d["agent"]["identity_key"] = "agent-key-1"

    assert compile_policy(parse(mutated(fn))).agent == "agent-key-1"


def test_compiled_policy_is_immutable():
    policy = compiled()

    with pytest.raises(TypeError):
        policy.actions["new"] = None

    with pytest.raises(AttributeError):
        policy.name = "other"


def test_unknown_construct_lookup_raises_key_error():
    policy = compiled()

    for lookup in (policy.pre_node, policy.invariant, policy.terminal_state):
        with pytest.raises(KeyError):
            lookup("ghost")


def test_policy_hash_is_stable_and_content_sensitive():
    assert compiled().policy_hash == compiled().policy_hash

    def fn(d):
        d["invariants"][0]["params"]["max"] = 1

    assert compile_policy(parse(mutated(fn))).policy_hash != compiled().policy_hash


def test_ledger_section_is_carried_through():
    def fn(d):
        d["ledger"] = {"backend": "jsonl", "path": "x.jsonl"}

    ledger = compile_policy(parse(mutated(fn))).ledger

    assert (ledger.backend, ledger.path, ledger.fsync) == ("jsonl", "x.jsonl", True)


# ================================================================
# POLICY REGISTRY
# ================================================================


def policy_named(name: str = "p", version: str = "1.0.0") -> CompiledPolicy:
    def fn(d):
        d["policy"] = {"name": name, "version": version}

    return compile_policy(parse(mutated(fn)))


def test_policy_registry_registers_and_retrieves():
    registry = PolicyRegistry()
    policy = registry.register(policy_named("a"))

    assert registry.get("a") is policy
    assert "a" in registry
    assert registry.names == ("a",)
    assert len(registry) == 1


def test_policy_registry_duplicate_name_and_version_raises():
    registry = PolicyRegistry()
    registry.register(policy_named("a", "1.0.0"))

    with pytest.raises(RegistryError, match="already registered"):
        registry.register(policy_named("a", "1.0.0"))


def test_policy_registry_unknown_policy_raises():
    registry = PolicyRegistry()

    with pytest.raises(RegistryError, match="Unknown policy"):
        registry.get("ghost")

    with pytest.raises(RegistryError, match="Unknown policy"):
        registry.versions("ghost")


def test_policy_registry_multiple_versions():
    registry = PolicyRegistry()
    one = registry.register(policy_named("a", "1.0.0"))
    two = registry.register(policy_named("a", "1.1.0"))

    assert registry.get("a", "1.0.0") is one
    assert registry.get("a", "1.1.0") is two
    assert registry.versions("a") == ("1.0.0", "1.1.0")
    assert len(registry) == 2


def test_policy_registry_refuses_to_guess_between_versions():
    registry = PolicyRegistry()
    registry.register(policy_named("a", "1.0.0"))
    registry.register(policy_named("a", "1.1.0"))

    with pytest.raises(RegistryError, match="several versions"):
        registry.get("a")


def test_policy_registry_unknown_version_raises():
    registry = PolicyRegistry()
    registry.register(policy_named("a", "1.0.0"))

    with pytest.raises(RegistryError, match="no version"):
        registry.get("a", "9.9.9")


def test_policy_registry_loads_yaml_files(tmp_path):
    registry = PolicyRegistry()
    path = write(tmp_path, dump(base_policy()))

    policy = registry.load_yaml(path, env={})

    assert registry.get("expense-policy") is policy

    with pytest.raises(RegistryError, match="already registered"):
        registry.load_yaml(path, env={})


def test_policy_registries_are_independent():
    one, two = PolicyRegistry(), PolicyRegistry()
    one.register(policy_named("a"))

    assert "a" not in two


# ================================================================
# Governance.from_yaml — END TO END ON REAL VSL-CORE
# ================================================================


def yaml_governance(tmp_path, **overrides) -> Governance:
    data = base_policy()
    data["ledger"] = {"backend": "jsonl", "path": str(tmp_path / "ledger.jsonl")}
    data.update(overrides)

    return Governance.from_yaml(write(tmp_path, dump(data)), env={})


@pytest.mark.asyncio
async def test_from_yaml_proceed(tmp_path):
    governance = yaml_governance(tmp_path)

    decision = await governance.authorize("approve_expense", ok_ctx())

    assert decision.outcome is Outcome.PROCEED
    assert [e.entry_type for e in entries(governance)] == [
        LedgerEntryType.MONITOR,
        LedgerEntryType.PRE_NODE,
        LedgerEntryType.VERIFICATION,
    ]


@pytest.mark.asyncio
async def test_from_yaml_prenode_denial_routes_to_human(tmp_path):
    governance = yaml_governance(tmp_path)

    decision = await governance.authorize("approve_expense", ok_ctx(confidence=0.51))

    assert decision.outcome is Outcome.HUMAN_QUEUE
    assert entries(governance)[-1].payload["result"] == "SUFFICIENT"


@pytest.mark.asyncio
async def test_from_yaml_invariant_violation_suspends(tmp_path):
    governance = yaml_governance(tmp_path)

    decision = await governance.authorize("approve_expense", ok_ctx(amount=5000))

    assert decision.outcome is Outcome.SUSPENDED
    assert decision.terminal_state == "expense-approvals-suspended"
    assert governance.active_suspension() is not None


@pytest.mark.asyncio
async def test_from_yaml_runs_effect_only_on_proceed(tmp_path):
    governance = yaml_governance(tmp_path)
    calls: list[str] = []

    denied = await governance.run(
        "approve_expense", ok_ctx(confidence=0.51), effect=lambda: calls.append("x")
    )
    allowed = await governance.run(
        "approve_expense", ok_ctx(), effect=lambda: calls.append("paid")
    )

    assert denied.performed is False
    assert allowed.performed is True
    assert calls == ["paid"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        ok_ctx(),
        ok_ctx(confidence=0.51),
        ok_ctx(confidence=0.55),
        ok_ctx(amount=2000),
        ok_ctx(amount=2000.01),
        ok_ctx(confidence=0.3, amount=5000),
    ],
)
async def test_yaml_policy_decides_like_the_programmatic_policy(tmp_path, context):
    from governance.policy import build_policy

    programmatic = Governance(
        policy=build_policy(), ledger=VerbaLedger(InMemoryLedgerStore())
    )
    declarative = yaml_governance(tmp_path)

    expected = await programmatic.authorize("approve_expense", context)
    actual = await declarative.authorize("approve_expense", context)

    assert actual.outcome is expected.outcome
    assert actual.terminal_state == expected.terminal_state
    assert [e.entry_type for e in entries(declarative)] == [
        e.entry_type for e in entries(programmatic)
    ]


@pytest.mark.asyncio
async def test_from_yaml_ledger_is_audit_clean_and_causally_valid(tmp_path):
    governance = yaml_governance(tmp_path)

    await governance.authorize("approve_expense", ok_ctx())
    await governance.authorize("approve_expense", ok_ctx(confidence=0.51))

    assert governance.verify_integrity()
    assert governance.audit().all_passed
    assert governance.validate_caused_by().valid


@pytest.mark.asyncio
async def test_from_yaml_unknown_action_is_still_rejected(tmp_path):
    governance = yaml_governance(tmp_path)

    with pytest.raises(ValueError, match="Unknown action"):
        await governance.authorize("wire_transfer", ok_ctx())


@pytest.mark.asyncio
async def test_from_yaml_missing_context_field_propagates_without_effect(tmp_path):
    governance = yaml_governance(tmp_path)
    calls: list[str] = []

    with pytest.raises(KeyError):
        await governance.run(
            "approve_expense",
            {"amount": 10, "currency": "NZD"},
            effect=lambda: calls.append("paid"),
        )

    assert calls == []


def test_from_yaml_injected_ledger_wins(tmp_path):
    ledger = VerbaLedger(InMemoryLedgerStore())
    data = base_policy()
    data["ledger"] = {"backend": "jsonl", "path": str(tmp_path / "unused.jsonl")}

    governance = Governance.from_yaml(
        write(tmp_path, dump(data)), ledger=ledger, env={}
    )

    assert governance.ledger is ledger
    assert not (tmp_path / "unused.jsonl").exists()


def test_from_yaml_without_any_ledger_is_an_error(tmp_path):
    data = base_policy()
    del data["ledger"]

    with pytest.raises(PolicyConfigError, match="no 'ledger' section"):
        Governance.from_yaml(write(tmp_path, dump(data)), env={})


def test_from_yaml_without_ledger_section_accepts_injected_ledger(tmp_path):
    data = base_policy()
    del data["ledger"]

    governance = Governance.from_yaml(
        write(tmp_path, dump(data)),
        ledger=VerbaLedger(InMemoryLedgerStore()),
        env={},
    )

    assert governance.policy.ledger is None


def test_from_yaml_memory_backend(tmp_path):
    governance = yaml_governance(tmp_path, ledger={"backend": "memory"})

    assert isinstance(governance.ledger.store, InMemoryLedgerStore)


def test_from_yaml_jsonl_backend_uses_configured_path(tmp_path):
    governance = yaml_governance(tmp_path)

    assert isinstance(governance.ledger.store, JsonlLedgerStore)
    assert governance.ledger.store.path == tmp_path / "ledger.jsonl"
    assert governance.ledger.store.fsync is True


def test_from_yaml_interpolates_ledger_path_from_env(tmp_path):
    data = base_policy()
    data["ledger"] = {"backend": "jsonl", "path": "${LEDGER_FILE}", "fsync": False}

    governance = Governance.from_yaml(
        write(tmp_path, dump(data)),
        env={"LEDGER_FILE": str(tmp_path / "env.jsonl")},
    )

    assert governance.ledger.store.path == tmp_path / "env.jsonl"
    assert governance.ledger.store.fsync is False


def test_from_yaml_example_file_end_to_end(tmp_path):
    governance = Governance.from_yaml(
        EXAMPLE, env={"VSL_LEDGER_PATH": str(tmp_path / "example.jsonl")}
    )

    assert governance.policy.version == "1.0.0"


@pytest.mark.asyncio
async def test_from_yaml_threshold_comes_from_env(tmp_path):
    """0.3 clears a 0.25 boundary but not the default 0.5 one."""

    data = base_policy()
    data["prenodes"][0]["params"]["decision_threshold"] = "${DECISION_THRESHOLD:-0.50}"
    path = write(tmp_path, dump(data))

    default = Governance.from_yaml(path, env={})
    custom = Governance.from_yaml(path, env={"DECISION_THRESHOLD": "0.25"})

    assert (await default.authorize("approve_expense", ok_ctx(confidence=0.3))).outcome is Outcome.HUMAN_QUEUE
    assert (await custom.authorize("approve_expense", ok_ctx(confidence=0.3))).outcome is Outcome.PROCEED


@pytest.mark.parametrize(
    "fn",
    [
        lambda d: d["invariants"][0].__setitem__("on_violation", "ghost"),
        lambda d: d["prenodes"][0].__setitem__("monitor", "ghost"),
        lambda d: d["policy"].__setitem__("version", 1),
    ],
)
def test_invalid_policy_never_builds_a_governance(tmp_path, fn):
    path = write(tmp_path, dump(mutated(fn)))

    with pytest.raises(Exception) as info:
        Governance.from_yaml(path, env={})

    assert isinstance(info.value, (PolicyConfigError, RegistryError))


def test_invalid_policy_never_creates_a_ledger_file(tmp_path):
    data = base_policy()
    data["ledger"] = {"backend": "jsonl", "path": str(tmp_path / "ledger.jsonl")}
    data["invariants"][0]["on_violation"] = "ghost"

    with pytest.raises(PolicyValidationError):
        Governance.from_yaml(write(tmp_path, dump(data)), env={})

    assert not (tmp_path / "ledger.jsonl").exists()


def test_programmatic_construction_still_works():
    from governance.policy import build_policy

    governance = Governance(
        policy=build_policy(), ledger=VerbaLedger(InMemoryLedgerStore())
    )

    assert governance.active_suspension() is None


# ================================================================
# SECURITY: YAML CANNOT EXECUTE CODE
# ================================================================


@pytest.mark.parametrize(
    "tag",
    [
        "!!python/object/apply:os.system ['touch {marker}']",
        "!!python/object/apply:builtins.eval ['__import__(\"os\").system(\"touch {marker}\")']",
        "!!python/name:os.system",
        "!!python/object:os.PathLike {{}}",
    ],
)
def test_python_tags_are_rejected_and_never_executed(tmp_path, tag):
    marker = tmp_path / "pwned"
    text = dump(base_policy()).replace(
        "name: expense-agent",
        "name: " + tag.format(marker=marker),
        1,
    )

    with pytest.raises(PolicyConfigError, match="Malformed YAML"):
        load_policy_config(write(tmp_path, text), env={})

    assert not marker.exists()


def test_dotted_rule_name_is_never_imported(tmp_path):
    """`os.system` is a legal-looking name, but names only ever resolve
    through the registry, never through import."""

    def fn(d):
        d["invariants"][0]["rule"] = "os.system"
        d["invariants"][0]["params"] = {}

    with pytest.raises(RegistryError, match="Unknown rule"):
        compile_policy(parse(mutated(fn)))


def test_code_looking_params_are_plain_strings():
    payload = "__import__('os').system('echo pwned')"

    def fn(d):
        d["invariants"][0]["params"]["field"] = payload

    config = parse(mutated(fn))

    assert config.data["invariants"][0]["params"]["field"] == payload

    # The built-in rule only uses it as a context key.
    rule = compile_policy(config).invariant("expense-cap").rule

    import asyncio

    with pytest.raises(KeyError):
        asyncio.run(rule({"amount": 1}))


def test_environment_text_is_never_evaluated():
    config = parse(
        mutated(
            lambda d: d["invariants"][0]["params"].__setitem__("field", "${F}")
        ),
        {"F": "__import__('os').system('echo pwned')"},
    )

    assert (
        config.data["invariants"][0]["params"]["field"]
        == "__import__('os').system('echo pwned')"
    )


def test_sdk_source_has_no_dynamic_code_execution():
    """AST guard: no call to eval/exec/compile/__import__, no
    importlib.import_module, no pickle, and every yaml.load names an
    explicit safe loader."""

    import ast

    banned_builtins = {"eval", "exec", "compile", "__import__"}

    for path in (ROOT / "vsl").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )

                for name in names:
                    assert not name.startswith(("pickle", "marshal", "shelve")), (
                        f"{name} imported in {path}"
                    )

            if not isinstance(node, ast.Call):
                continue

            func = node.func

            if isinstance(func, ast.Name):
                assert func.id not in banned_builtins, (
                    f"{func.id}() called in {path}:{node.lineno}"
                )

            if isinstance(func, ast.Attribute):
                assert func.attr != "import_module", (
                    f"import_module called in {path}:{node.lineno}"
                )

                if func.attr in {"load", "load_all", "unsafe_load", "full_load"} and (
                    isinstance(func.value, ast.Name) and func.value.id == "yaml"
                ):
                    keywords = {k.arg: k.value for k in node.keywords}

                    assert func.attr == "load", (
                        f"yaml.{func.attr} called in {path}:{node.lineno}"
                    )
                    assert "Loader" in keywords, (
                        f"yaml.load without Loader in {path}:{node.lineno}"
                    )
                    assert (
                        isinstance(keywords["Loader"], ast.Name)
                        and keywords["Loader"].id == "_UniqueKeySafeLoader"
                    )
