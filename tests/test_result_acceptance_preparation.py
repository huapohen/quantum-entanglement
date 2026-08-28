from __future__ import annotations

import copy
import pickle
import sqlite3
import traceback
import unittest
from dataclasses import replace
from unittest.mock import patch

import quantum_entanglement
from quantum_entanglement._result_acceptance import (
    _prepare_scoped_invocation_result_acceptance_v2,
    _PreparedScopedInvocationResultAcceptanceV2,
)
from quantum_entanglement._result_artifact_transaction import (
    _PreparedResultArtifactBatch,
)
from quantum_entanglement.invocation_execution import ScopedInvocationStartClaimedV3
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
)
from tests.test_invocation_result_acceptance_request import request_for
from tests.test_scoped_invocation_execution import LEASE_TOKEN, valid_scoped_claim


class ResultAcceptancePreparationTests(unittest.TestCase):
    def test_preparation_detaches_exact_request_claim_and_artifacts(self) -> None:
        request = request_for()
        claimed = valid_scoped_claim()

        prepared = _prepare_scoped_invocation_result_acceptance_v2(request, claimed)

        self.assertIs(type(prepared), _PreparedScopedInvocationResultAcceptanceV2)
        self.assertIs(type(prepared.request), ScopedInvocationResultAcceptanceRequestV2)
        self.assertIs(type(prepared.claimed), ScopedInvocationStartClaimedV3)
        self.assertIs(type(prepared.artifact_batch), _PreparedResultArtifactBatch)
        self.assertIsNot(prepared.request, request)
        self.assertIsNot(prepared.claimed, claimed)
        self.assertIsNot(prepared.claimed.receipt, claimed.receipt)
        self.assertIsNot(prepared.claimed.lease, claimed.lease)
        self.assertEqual(prepared.request.start_receipt, prepared.claimed.receipt)
        self.assertEqual(
            tuple(item.descriptor for item in prepared.artifact_batch.items),
            prepared.request.manifest.artifacts,
        )
        prepared.verify()

    def test_preparation_rejects_request_for_a_different_start_receipt(self) -> None:
        claimed = valid_scoped_claim()
        other_claim = ScopedInvocationStartClaimedV3(
            receipt=replace(claimed.receipt, event_id="event-other-start"),
            lease=claimed.lease,
        )

        with self.assertRaisesRegex(ValueError, "does not match the claimed start"):
            _prepare_scoped_invocation_result_acceptance_v2(request_for(), other_claim)

    def test_preparation_requires_exact_public_input_classes(self) -> None:
        class RequestSubclass(ScopedInvocationResultAcceptanceRequestV2):
            pass

        class ClaimSubclass(ScopedInvocationStartClaimedV3):
            pass

        request_subclass = object.__new__(RequestSubclass)
        claim_subclass = object.__new__(ClaimSubclass)
        with self.assertRaisesRegex(TypeError, "exact ScopedInvocationResultAcceptanceRequestV2"):
            _prepare_scoped_invocation_result_acceptance_v2(
                request_subclass,
                valid_scoped_claim(),
            )
        with self.assertRaisesRegex(TypeError, "exact ScopedInvocationStartClaimedV3"):
            _prepare_scoped_invocation_result_acceptance_v2(
                request_for(),
                claim_subclass,
            )

    def test_preparation_reads_no_clock_id_provider_or_sqlite(self) -> None:
        with (
            patch(
                "quantum_entanglement.protocol.new_id",
                side_effect=AssertionError("ID provider was called"),
            ) as new_id,
            patch(
                "quantum_entanglement.protocol.utc_now",
                side_effect=AssertionError("clock was called"),
            ) as clock,
            patch.object(
                sqlite3,
                "connect",
                side_effect=AssertionError("SQLite was opened"),
            ) as connect,
        ):
            prepared = _prepare_scoped_invocation_result_acceptance_v2(
                request_for(),
                valid_scoped_claim(),
            )
        prepared.verify()
        new_id.assert_not_called()
        clock.assert_not_called()
        connect.assert_not_called()

    def test_plaintext_lease_is_absent_from_repr_and_errors(self) -> None:
        prepared = _prepare_scoped_invocation_result_acceptance_v2(
            request_for(),
            valid_scoped_claim(),
        )
        self.assertNotIn(LEASE_TOKEN, repr(prepared))
        self.assertNotIn(LEASE_TOKEN, str(prepared))

        other_claim = ScopedInvocationStartClaimedV3(
            receipt=replace(prepared.claimed.receipt, event_id="event-other-start"),
            lease=prepared.claimed.lease,
        )
        try:
            _prepare_scoped_invocation_result_acceptance_v2(
                prepared.request,
                other_claim,
            )
        except ValueError as error:
            error_text = str(error)
            error_repr = repr(error)
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        else:  # pragma: no cover - the mismatch above is exact.
            self.fail("mismatched claim unexpectedly passed")
        self.assertNotIn(LEASE_TOKEN, error_text)
        self.assertNotIn(LEASE_TOKEN, error_repr)
        self.assertNotIn(LEASE_TOKEN, rendered)

    def test_preparation_remains_stable_after_caller_mutation(self) -> None:
        request = request_for()
        claimed = valid_scoped_claim()
        prepared = _prepare_scoped_invocation_result_acceptance_v2(request, claimed)

        object.__setattr__(request, "acceptance_idempotency_key", "caller-mutated")
        object.__setattr__(claimed.lease, "lease_token", "caller-mutated-token")

        prepared.verify()
        self.assertNotEqual(
            prepared.request.acceptance_idempotency_key,
            request.acceptance_idempotency_key,
        )
        self.assertNotEqual(prepared.claimed.lease.lease_token, claimed.lease.lease_token)

    def test_prepared_authority_cannot_be_copied_or_serialized(self) -> None:
        prepared = _prepare_scoped_invocation_result_acceptance_v2(
            request_for(),
            valid_scoped_claim(),
        )
        for operation in (
            lambda: copy.copy(prepared),
            lambda: copy.deepcopy(prepared),
            lambda: pickle.dumps(prepared),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(TypeError, "cannot be (copied|serialized)"):
                    operation()

    def test_private_preparation_adds_no_package_writer_or_accepted_surface(self) -> None:
        for name in (
            "_PreparedScopedInvocationResultAcceptanceV2",
            "_prepare_scoped_invocation_result_acceptance_v2",
            "ScopedInvocationResultAcceptedV2",
            "accept_scoped_invocation_result_v2",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, quantum_entanglement.__all__)
                self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
