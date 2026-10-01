from __future__ import annotations

import inspect
import uuid
from typing import Any, Awaitable, Callable

from vsl_core.conformance.reference_adapter import (
    PlainPythonReferenceAdapter,
)

from vsl_core.exceptions import (
    AutomationDeniedException,
    InvariantViolation,
)

from vsl_core.governance import (
    GovernanceAuthority,
    request_re_enablement,
)

from vsl_core.identity import (
    IdentityKey,
    Instance,
)

from vsl_core.ledger import (
    LedgerAuditReport,
    LedgerEntryType,
    VerificationResult,
)

from pathlib import Path
from typing import Mapping

from vsl_core.ledger import LedgerCheckpoint, VerbaCertificate

from .audit import CausedByReport, validate_caused_by
from .checkpoint import CheckpointVerification, verify_checkpoint
from .config import load_policy_config
from .decision import Decision
from .exceptions import PolicyConfigError
from .inspection import inspect_ledger, validate_gap
from .ledger import build_ledger
from .policy import compile_policy
from .registry import Registry
from .outcome import Outcome
from .result import GovernedResult


class Governance:
    """
    X-Verba VSL SDK governance façade.

    Phase 1:
    - accepts an already-constructed VSL policy
    - accepts a VerbaLedger
    - compiles VSL-Core constructs once
    - evaluates consequential actions
    - records causal ledger evidence
    - never performs the actual side effect

    Phase 2:
    - verifies governance through the ledger
    - derives suspension state from the ledger
    - blocks execution while suspended
    - supports human-authorised re-enablement
    - records specification updates
    - supports the complete suspension -> re-enable ->
      specification update -> execution lifecycle

    Phase 2.5:
    - verifies ledger integrity (were records edited?)
    - audits the ledger against the five VSL audit checks
      (do the records describe a governed process?)
    - validates the caused_by causal chain
    """

    def __init__(self, policy: Any, ledger: Any):
        self.policy = policy
        self.ledger = ledger

        # ---------------------------------------------------------
        # VSL-Core adapter
        # ---------------------------------------------------------

        self._adapter = PlainPythonReferenceAdapter()

        # ---------------------------------------------------------
        # Compile governance gates once
        # ---------------------------------------------------------

        self._compiled_actions: dict[str, dict[str, Any]] = {}

        self._compile_policy()

        # ---------------------------------------------------------
        # Agent identity
        # ---------------------------------------------------------

        agent_name = getattr(
            policy,
            "agent",
            getattr(
                policy,
                "agent_name",
                "x-verba-agent",
            ),
        )

        self._identity = IdentityKey(
            value=str(agent_name)
        )

        self._instance = Instance.new(
            self._identity
        )

    # =============================================================
    # PHASE 3
    # YAML POLICY
    # =============================================================

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        registry: Registry | None = None,
        ledger: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> "Governance":
        """
        Build a Governance from a YAML policy file.

        The file is loaded safely, interpolated, validated against the
        JSON Schema and the policy rules, then compiled to real
        VSL-Core constructs. A Governance is never built from a policy
        that fails any of those steps.

        ledger: an already-built VerbaLedger. If omitted, the ledger
        described by the policy's `ledger:` section is opened. A policy
        with no `ledger:` section and no injected ledger is an error:
        governance without evidence is not governance.

        registry: monitors/rules available to the policy (default:
        the built-ins). env: environment mapping for interpolation
        (default: os.environ).
        """

        policy = compile_policy(
            load_policy_config(path, env=env),
            registry,
        )

        if ledger is None:
            if policy.ledger is None:
                raise PolicyConfigError(
                    "The policy has no 'ledger' section and no "
                    "ledger was supplied."
                )

            ledger = build_ledger(policy.ledger, env=env)

        return cls(policy=policy, ledger=ledger)

    # =============================================================
    # POLICY COMPILATION
    # =============================================================

    def _compile_policy(self) -> None:
        """
        Compile VSL-Core PreNode and Invariant constructs once.

        The SDK does not implement governance semantics itself.
        VSL-Core performs the actual gate enforcement.
        """

        actions = getattr(
            self.policy,
            "actions",
            None,
        )

        if actions is None:
            raise ValueError(
                "Policy must expose an 'actions' mapping in Phase 1."
            )

        for action_name, action_policy in actions.items():

            pre_node = getattr(
                action_policy,
                "pre_node",
                None,
            )

            invariants = getattr(
                action_policy,
                "invariants",
                (),
            )

            # -----------------------------------------------------
            # Compile PreNode
            # -----------------------------------------------------

            compiled_pre_node = None

            if pre_node is not None:
                compiled_pre_node = (
                    self._adapter.compile_pre_node(
                        pre_node
                    )
                )

            # -----------------------------------------------------
            # Compile Invariants
            # -----------------------------------------------------

            compiled_invariants = [
                self._adapter.compile_invariant(
                    invariant
                )
                for invariant in invariants
            ]

            self._compiled_actions[action_name] = {
                "policy": action_policy,
                "pre_node": pre_node,
                "pre_node_gate": compiled_pre_node,
                "invariants": tuple(invariants),
                "invariant_gates": tuple(
                    compiled_invariants
                ),
            }

    # =============================================================
    # LEDGER HELPERS
    # =============================================================

    def _all_entries(self) -> list[Any]:
        """
        Return all ledger entries as a concrete list.

        The VSL-Core ledger store exposes entries as an iterable /
        generator, so converting once makes lifecycle inspection
        deterministic.
        """

        return list(
            self.ledger.store.all_entries()
        )

    def _find_entry(
        self,
        entry_id: str,
    ) -> Any | None:
        """
        Find one ledger entry by entry ID.
        """

        for entry in self._all_entries():
            if entry.entry_id == entry_id:
                return entry

        return None

    # =============================================================
    # PHASE 2.2
    # LEDGER-DERIVED SUSPENSION
    # =============================================================

    def active_suspension(self) -> str | None:
        """
        Determine the active suspension exclusively from the ledger.

        A TERMINAL entry represents an active suspension unless a
        HUMAN_AUTHORISED_TRANSITION explicitly resolves that terminal.

        Returns:
            The unresolved TERMINAL entry ID.

        Returns None when no active suspension exists.

        Important:
        Suspension is NOT stored as mutable SDK state.
        The ledger remains the source of truth.
        """

        identity_key = self._identity.value

        terminal_entries: list[Any] = []
        resolved_terminal_ids: set[str] = set()

        for entry in self._all_entries():

            # -----------------------------------------------------
            # Only inspect entries belonging to this agent identity.
            # -----------------------------------------------------

            if getattr(
                entry,
                "identity_key",
                None,
            ) != identity_key:
                continue

            # -----------------------------------------------------
            # Human transition resolves the terminal entry referenced
            # by caused_by.
            # -----------------------------------------------------

            if (
                entry.entry_type
                == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION
                and entry.caused_by
            ):
                resolved_terminal_ids.add(
                    entry.caused_by
                )

            # -----------------------------------------------------
            # TERMINAL creates suspension.
            # -----------------------------------------------------

            elif (
                entry.entry_type
                == LedgerEntryType.TERMINAL
            ):
                terminal_entries.append(entry)

        # ---------------------------------------------------------
        # Find unresolved terminal entries.
        # ---------------------------------------------------------

        unresolved = [
            terminal
            for terminal in terminal_entries
            if terminal.entry_id
            not in resolved_terminal_ids
        ]

        if not unresolved:
            return None

        # The latest unresolved terminal is the active suspension.
        return unresolved[-1].entry_id

    # =============================================================
    # AUTHORIZE
    # =============================================================

    async def authorize(
        self,
        action: str,
        context: dict[str, Any],
        correlation_id: str | None = None,
    ) -> Decision:
        """
        Authorize a governed action.

        Flow:

        SUSPENSION CHECK
            ↓
        MONITOR
            ↓
        PRE_NODE
            ↓
        ┌───────────────┐
        │               │
        DENY           ALLOW
        │               │
        ↓               ↓
        VERIFICATION   INVARIANTS
        SUFFICIENT        │
        │            ┌────┴────┐
        ↓            │         │
        HUMAN       PASS      FAIL
        QUEUE         │         │
                      ↓         ↓
                 VERIFICATION  VERIFICATION
                 SUFFICIENT    INSUFFICIENT
                                  ↓
                               TERMINAL

        Successful governance returns PROCEED.

        This method only performs governance evaluation.
        The actual side effect remains the caller's responsibility.
        """

        # =========================================================
        # 0. VALIDATE ACTION
        # =========================================================

        if action not in self._compiled_actions:
            raise ValueError(
                f"Unknown action: {action}"
            )

        decision_id = (
            correlation_id
            if correlation_id is not None
            else str(uuid.uuid4())
        )

        # =========================================================
        # 0.1 PHASE 2.2 — CHECK ACTIVE SUSPENSION
        # =========================================================

        suspension_entry_id = (
            self.active_suspension()
        )

        if suspension_entry_id is not None:

            identity_key = self._identity.value
            instance_id = self._instance.instance_id

            suspension_entry = self._find_entry(
                suspension_entry_id
            )

            terminal_state = None

            if suspension_entry is not None:
                terminal_state = (
                    suspension_entry.payload.get(
                        "terminal_state"
                    )
                )

            # -----------------------------------------------------
            # A suspended request still gets a MONITOR record.
            #
            # It does NOT get a PRE_NODE or VERIFICATION because
            # execution is blocked before normal governance begins.
            # -----------------------------------------------------

            self.ledger.write_monitor(
                identity_key=identity_key,
                instance_id=instance_id,
                decision_id=decision_id,
                drift_detected=False,
                extra_payload={
                    "action": action,
                    **dict(context),
                    "suspended_by": suspension_entry_id,
                },
            )

            return Decision(
                outcome=Outcome.SUSPENDED,
                allowed=False,
                requires_human_review=False,
                suspended=True,
                decision_id=decision_id,
                reason=(
                    "Automation is suspended by terminal "
                    f"entry {suspension_entry_id}."
                ),
                terminal_state=terminal_state,
            )

        # =========================================================
        # PREPARE ACTION
        # =========================================================

        action_data = self._compiled_actions[action]

        pre_node = action_data["pre_node"]
        pre_node_gate = action_data["pre_node_gate"]

        invariants = action_data["invariants"]
        invariant_gates = action_data["invariant_gates"]

        identity_key = self._identity.value
        instance_id = self._instance.instance_id

        # =========================================================
        # 1. MONITOR
        # =========================================================

        monitor_entry = self.ledger.write_monitor(
            identity_key=identity_key,
            instance_id=instance_id,
            decision_id=decision_id,
            drift_detected=False,
            extra_payload={
                "action": action,
                **dict(context),
            },
        )

        # =========================================================
        # 2. PRE-NODE
        # =========================================================

        pre_node_denied = False

        pre_node_exception: (
            AutomationDeniedException | None
        ) = None

        if pre_node_gate is not None:

            try:

                await pre_node_gate(
                    dict(context)
                )

            except AutomationDeniedException as exc:

                pre_node_denied = True
                pre_node_exception = exc

        pre_node_entry = self.ledger.write(
            LedgerEntryType.PRE_NODE,
            identity_key=identity_key,
            instance_id=instance_id,
            decision_id=decision_id,
            caused_by=monitor_entry.entry_id,
            payload={
                "action": action,
                "pre_node": (
                    pre_node.name
                    if pre_node is not None
                    else None
                ),
                "evaluated": (
                    pre_node_gate is not None
                ),
                "denied": pre_node_denied,
            },
        )

        # =========================================================
        # 3. PRE-NODE DENIAL → HUMAN QUEUE
        # =========================================================

        if pre_node_denied:

            self.ledger.write_verification(
                identity_key=identity_key,
                instance_id=instance_id,
                decision_id=decision_id,
                caused_by=pre_node_entry.entry_id,
                result=VerificationResult.SUFFICIENT,
                extra_payload={
                    "action": action,
                    "check": "pre_node",
                    "outcome": (
                        "routed_to_human_queue"
                    ),
                },
            )

            return Decision(
                outcome=Outcome.HUMAN_QUEUE,
                allowed=False,
                requires_human_review=True,
                suspended=False,
                decision_id=decision_id,
                reason=str(
                    pre_node_exception
                ),
                terminal_state=None,
            )

        # =========================================================
        # 4. INVARIANTS
        # =========================================================

        for invariant, invariant_gate in zip(
            invariants,
            invariant_gates,
        ):

            try:

                await invariant_gate(
                    dict(context)
                )

            except InvariantViolation as exc:

                # -------------------------------------------------
                # Invariant failure is INSUFFICIENT verification.
                # -------------------------------------------------

                failed_verification = (
                    self.ledger.write_verification(
                        identity_key=identity_key,
                        instance_id=instance_id,
                        decision_id=decision_id,
                        caused_by=pre_node_entry.entry_id,
                        result=(
                            VerificationResult.INSUFFICIENT
                        ),
                        extra_payload={
                            "action": action,
                            "check": "invariant",
                            "invariant": invariant.name,
                            "outcome": "violation",
                        },
                    )
                )

                # -------------------------------------------------
                # Determine terminal state.
                # -------------------------------------------------

                terminal_state = getattr(
                    exc,
                    "terminal_state_name",
                    getattr(
                        invariant.on_violation,
                        "name",
                        str(
                            invariant.on_violation
                        ),
                    ),
                )

                # -------------------------------------------------
                # TERMINAL creates suspension.
                # -------------------------------------------------

                self.ledger.write(
                    LedgerEntryType.TERMINAL,
                    identity_key=identity_key,
                    instance_id=instance_id,
                    decision_id=decision_id,
                    caused_by=(
                        failed_verification.entry_id
                    ),
                    payload={
                        "action": action,
                        "invariant": invariant.name,
                        "terminal_state": (
                            terminal_state
                        ),
                    },
                )

                return Decision(
                    outcome=Outcome.SUSPENDED,
                    allowed=False,
                    requires_human_review=False,
                    suspended=True,
                    decision_id=decision_id,
                    reason=str(exc),
                    terminal_state=str(
                        terminal_state
                    ),
                )

        # =========================================================
        # 5. SUCCESSFUL VERIFICATION
        # =========================================================

        self.ledger.write_verification(
            identity_key=identity_key,
            instance_id=instance_id,
            decision_id=decision_id,
            caused_by=pre_node_entry.entry_id,
            result=VerificationResult.SUFFICIENT,
            extra_payload={
                "action": action,
                "outcome": "approved",
            },
        )

        # =========================================================
        # 6. PROCEED
        # =========================================================

        return Decision(
            outcome=Outcome.PROCEED,
            allowed=True,
            requires_human_review=False,
            suspended=False,
            decision_id=decision_id,
            reason=None,
            terminal_state=None,
        )

    # =============================================================
    # PHASE 2.1
    # GOVERNED EXECUTION
    # =============================================================

    async def run(
        self,
        action: str,
        context: dict[str, Any],
        effect: Callable[
            [],
            Any | Awaitable[Any],
        ],
        correlation_id: str | None = None,
    ) -> GovernedResult:
        """
        Execute a side effect only after governance returns PROCEED.

        Governance denial or suspension prevents the effect
        from executing.

        Exceptions raised by the actual side effect are deliberately
        propagated to the caller.
        """

        decision = await self.authorize(
            action=action,
            context=context,
            correlation_id=correlation_id,
        )

        # ---------------------------------------------------------
        # Governance did not permit execution.
        # ---------------------------------------------------------

        if decision.outcome is not Outcome.PROCEED:

            return GovernedResult(
                decision=decision,
                performed=False,
                result=None,
            )

        # ---------------------------------------------------------
        # Governance permitted execution.
        # ---------------------------------------------------------

        result = effect()

        # ---------------------------------------------------------
        # Support asynchronous effects.
        # ---------------------------------------------------------

        if inspect.isawaitable(result):
            result = await result

        return GovernedResult(
            decision=decision,
            performed=True,
            result=result,
        )

    # =============================================================
    # PHASE 2.3
    # HUMAN RE-ENABLEMENT
    # =============================================================

    def reenable(
        self,
        *,
        authority: GovernanceAuthority,
        authorised_by: str,
        evidence: Any,
    ) -> Any:
        """
        Re-enable automation after an active terminal state.

        The VSL-Core request_re_enablement() function remains the
        authority for the actual VSL-Core transition.

        The SDK records the transition into the VerbaLedger and
        replaces the current Instance with the new VSL-Core instance.

        Required:
        - an active terminal state
        - a named human authority
        - evidence
        """

        # ---------------------------------------------------------
        # 1. There must be an active terminal state.
        # ---------------------------------------------------------

        terminal_entry_id = (
            self.active_suspension()
        )

        if terminal_entry_id is None:
            raise ValueError(
                "No active terminal state to re-enable."
            )

        # ---------------------------------------------------------
        # 2. Human authority must be explicitly named.
        # ---------------------------------------------------------

        if not authorised_by:
            raise ValueError(
                "authorised_by must not be empty."
            )

        # ---------------------------------------------------------
        # 3. Ask VSL-Core to perform the re-enablement transition.
        # ---------------------------------------------------------

        old_instance = self._instance

        new_instance, transition = (
            request_re_enablement(
                old_instance,
                authority,
                authorised_by=authorised_by,
                evidence=evidence,
            )
        )

        # ---------------------------------------------------------
        # 4. Generate SDK ledger decision ID.
        #
        # Do not depend on a particular transition object shape.
        # ---------------------------------------------------------

        transition_decision_id = getattr(
            transition,
            "decision_id",
            str(uuid.uuid4()),
        )

        # ---------------------------------------------------------
        # 5. Extract transition metadata safely.
        # ---------------------------------------------------------

        authority_name = getattr(
            getattr(
                transition,
                "authority",
                authority,
            ),
            "name",
            str(
                getattr(
                    transition,
                    "authority",
                    authority,
                )
            ),
        )

        transition_authorised_by = getattr(
            transition,
            "authorised_by",
            authorised_by,
        )

        evidence_hash = getattr(
            transition,
            "authorisation_evidence_hash",
            None,
        )

        # ---------------------------------------------------------
        # 6. Extract evidence metadata safely.
        # ---------------------------------------------------------

        root_cause_analysis = getattr(
            evidence,
            "root_cause_analysis",
            None,
        )

        specification_update_proposal = getattr(
            evidence,
            "specification_update_proposal",
            None,
        )

        # ---------------------------------------------------------
        # 7. Record human-authorised transition.
        # ---------------------------------------------------------

        human_transition = self.ledger.write(
            LedgerEntryType.HUMAN_AUTHORISED_TRANSITION,
            identity_key=old_instance.identity_key.value,
            instance_id=old_instance.instance_id,
            decision_id=transition_decision_id,
            caused_by=terminal_entry_id,
            payload={
                "terminal_state_entry_id": (
                    terminal_entry_id
                ),
                "authority": authority_name,
                "authorised_by": (
                    transition_authorised_by
                ),
                "root_cause_analysis": (
                    root_cause_analysis
                ),
                "specification_update_proposal": (
                    specification_update_proposal
                ),
                "authorisation_evidence_hash": (
                    evidence_hash
                ),
            },
        )

        # ---------------------------------------------------------
        # 8. Record the new instance / re-enable event.
        # ---------------------------------------------------------

        new_instance_id = getattr(
            new_instance,
            "instance_id",
            None,
        )

        new_identity_key = getattr(
            getattr(
                new_instance,
                "identity_key",
                self._identity,
            ),
            "value",
            self._identity.value,
        )

        predecessor_instance_id = getattr(
            new_instance,
            "predecessor_instance_id",
            old_instance.instance_id,
        )

        generation = getattr(
            new_instance,
            "generation",
            None,
        )

        reenable_entry = self.ledger.write(
            LedgerEntryType.RE_ENABLEMENT,
            identity_key=old_instance.identity_key.value,
            instance_id=old_instance.instance_id,
            decision_id=transition_decision_id,
            caused_by=human_transition.entry_id,
            payload={
                "new_instance_id": new_instance_id,
                "new_identity_key": new_identity_key,
                "predecessor_instance_id": (
                    predecessor_instance_id
                ),
                "generation": generation,
            },
        )

        # ---------------------------------------------------------
        # 9. Update SDK runtime identity.
        #
        # The suspension itself is still resolved from the ledger.
        # No separate mutable "suspended" flag is introduced.
        # ---------------------------------------------------------

        self._instance = new_instance
        self._identity = new_instance.identity_key

        return reenable_entry

    # =============================================================
    # PHASE 2.4
    # SPECIFICATION UPDATE
    # =============================================================

    def record_specification_update(
        self,
        *,
        verification_entry_id: str,
        new_policy_version: str,
        summary: str,
        approved_by: str,
    ) -> Any:
        """
        Record an approved specification update caused by an
        INSUFFICIENT verification.

        The SDK does not silently modify the policy object here.

        Instead, it records the specification change as an explicit
        ledger event.

        This keeps governance evidence auditable and prevents a
        hidden policy mutation from occurring outside the ledger.
        """

        # ---------------------------------------------------------
        # 1. Locate verification entry.
        # ---------------------------------------------------------

        verification_entry = self._find_entry(
            verification_entry_id
        )

        if verification_entry is None:
            raise ValueError(
                "Verification entry not found."
            )

        # ---------------------------------------------------------
        # 2. It must actually be a VERIFICATION entry.
        # ---------------------------------------------------------

        if (
            verification_entry.entry_type
            != LedgerEntryType.VERIFICATION
        ):
            raise ValueError(
                "verification_entry_id must reference "
                "a VERIFICATION entry."
            )

        # ---------------------------------------------------------
        # 3. Specification changes must be caused by an
        #    INSUFFICIENT verification.
        # ---------------------------------------------------------

        if (
            verification_entry.payload.get("result")
            != "INSUFFICIENT"
        ):
            raise ValueError(
                "Specification updates must reference "
                "an INSUFFICIENT verification."
            )

        # ---------------------------------------------------------
        # 4. Validate required metadata.
        # ---------------------------------------------------------

        if not new_policy_version:
            raise ValueError(
                "new_policy_version must not be empty."
            )

        if not summary:
            raise ValueError(
                "summary must not be empty."
            )

        if not approved_by:
            raise ValueError(
                "approved_by must not be empty."
            )

        # ---------------------------------------------------------
        # 5. Record specification update.
        # ---------------------------------------------------------

        return self.ledger.write(
            LedgerEntryType.SPECIFICATION_UPDATE,
            identity_key=self._identity.value,
            instance_id=self._instance.instance_id,
            decision_id=verification_entry.decision_id,
            caused_by=verification_entry.entry_id,
            payload={
                "policy_version": new_policy_version,
                "summary": summary,
                "approved_by": approved_by,
                "status": "approved",
            },
        )

    # =============================================================
    # PHASE 2.5
    # LEDGER INTEGRITY & GOVERNANCE AUDIT
    # =============================================================

    def verify_integrity(self) -> bool:
        """
        Were the ledger records edited?

        Delegates to VSL-Core: every hash, prev_hash link and
        sequence number is recomputed. Never raises; returns False
        for an edited payload, a broken link or a sequence gap.

        Limitation (Spec, Known constraint 5): this cannot detect
        truncation, because a shortened chain is still a valid chain.
        Anchor a ledger checkpoint outside the operator's control to
        detect that.
        """

        return self.ledger.verify_integrity()

    def audit(
        self,
        *,
        max_monitor_gap_seconds: float | None = None,
    ) -> LedgerAuditReport:
        """
        Do the ledger records describe a governed process?

        Delegates to VSL-Core, which runs the five VSL audit checks:

        1. no_monitoring_gaps
        2. drift_flagged_monitor_has_pre_node
        3. pre_node_has_verification
        4. insufficient_verification_has_specification_update
        5. terminal_has_human_authorised_transition

        max_monitor_gap_seconds has no universal default. Passing
        None skips check 1, which is then reported as passed.

        audit() does NOT detect tampering; run verify_integrity() as
        well. It also trusts caused_by where present; run
        validate_caused_by() to confirm the causal chain itself.
        """

        return self.ledger.audit(
            max_monitor_gap_seconds=max_monitor_gap_seconds
        )

    def validate_caused_by(self) -> CausedByReport:
        """
        Validate the ledger's caused_by causal chain.

        Read-only. See vsl.audit.validate_caused_by for the rules.
        """

        return validate_caused_by(self._all_entries())

    # =============================================================
    # PHASE 4
    # CHECKPOINT AND CERTIFICATION
    # =============================================================

    def checkpoint(self) -> LedgerCheckpoint | None:
        """
        The ledger's current tip, for external anchoring.

        Delegates to VerbaLedger.current_checkpoint() (None for an
        empty ledger). Store the result somewhere the operator cannot
        rewrite: that is what makes truncation detectable. See
        vsl.checkpoint.
        """

        return self.ledger.current_checkpoint()

    def verify_checkpoint(
        self,
        checkpoint: LedgerCheckpoint,
    ) -> CheckpointVerification:
        """Compare the ledger against an anchored checkpoint."""

        return verify_checkpoint(self.ledger, checkpoint)

    def certify(
        self,
        *,
        max_monitor_gap_seconds: float,
        checkpoint: LedgerCheckpoint | None = None,
    ) -> VerbaCertificate | None:
        """
        Issue a VSL-Core VerbaCertificate, or None.

        A certificate is issued only if ALL of these hold:

        - verify_integrity() is True
        - the five audit checks pass
        - validate_caused_by() finds no issues
        - the ledger matches `checkpoint`, when one is given
        - the ledger is not empty

        max_monitor_gap_seconds is required: without a threshold audit
        check 1 is skipped and the certificate would imply continuous
        monitoring that was never measured.

        The certificate certifies the governance process, not the
        underlying system, model or domain.
        """

        if max_monitor_gap_seconds is None:
            raise ValueError(
                "max_monitor_gap_seconds is required to certify."
            )

        validate_gap(max_monitor_gap_seconds)

        return inspect_ledger(
            self.ledger,
            max_monitor_gap_seconds=max_monitor_gap_seconds,
            checkpoint=checkpoint,
            certify=True,
        ).certificate
