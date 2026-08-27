from __future__ import annotations

import copy
import json
import pickle
import unittest

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultObservedV2,
    ScopedInvocationResultReceiptV2,
)
from tests.test_invocation_result_receipt import receipt_for


class ScopedInvocationResultObservedTests(unittest.TestCase):
    def test_wire_json_pickle_and_copy_round_trips_revalidate_the_receipt(self) -> None:
        receipt = receipt_for()
        observed = ScopedInvocationResultObservedV2(receipt)
        wire = observed.to_dict()
        json_wire = json.loads(
            json.dumps(
                wire,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        reconstructed = (
            ScopedInvocationResultObservedV2.from_dict(wire),
            ScopedInvocationResultObservedV2.from_dict(json_wire),
            pickle.loads(pickle.dumps(observed)),
            copy.copy(observed),
            copy.deepcopy(observed),
        )

        self.assertEqual(observed.receipt, receipt)
        self.assertIsNot(observed.receipt, receipt)
        for decoded in reconstructed:
            with self.subTest(decoded=type(decoded).__name__):
                self.assertIs(type(decoded), ScopedInvocationResultObservedV2)
                self.assertEqual(decoded, observed)
                self.assertIsNot(decoded, observed)
                self.assertIsNot(decoded.receipt, observed.receipt)
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(pickle_protocol=protocol):
                decoded = pickle.loads(pickle.dumps(observed, protocol=protocol))
                self.assertIs(type(decoded), ScopedInvocationResultObservedV2)
                self.assertEqual(decoded, observed)
                self.assertIsNot(decoded.receipt, observed.receipt)

    def test_exact_wire_shape_and_subclasses_fail_closed(self) -> None:
        wire = ScopedInvocationResultObservedV2(receipt_for()).to_dict()
        with self.assertRaisesRegex(ValueError, "exact schema"):
            ScopedInvocationResultObservedV2.from_dict({})
        with self.assertRaisesRegex(ValueError, "exact schema"):
            ScopedInvocationResultObservedV2.from_dict({**wire, "future": True})
        with self.assertRaises(TypeError):
            ScopedInvocationResultObservedV2.from_dict(tuple(wire.items()))

        class ObservedSubclass(ScopedInvocationResultObservedV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ObservedSubclass.from_dict(wire)
        with self.assertRaisesRegex(TypeError, "exact ScopedInvocationResultObservedV2"):
            ObservedSubclass(receipt_for())

    def test_constructor_and_codecs_ignore_instance_method_shadowing(self) -> None:
        receipt = receipt_for()
        baseline = ScopedInvocationResultObservedV2(receipt)
        baseline_wire = baseline.to_dict()

        object.__setattr__(receipt, "to_dict", lambda: {"forged": True})
        object.__setattr__(receipt, "canonical_digest", lambda: "0" * 64)
        observed = ScopedInvocationResultObservedV2(receipt)
        self.assertEqual(observed.to_dict(), baseline_wire)

        for field_name, value in (
            ("to_dict", lambda: {"forged": True}),
            ("__reduce__", lambda: (dict, (("forged", True),))),
            ("__reduce_ex__", lambda protocol: (dict, (("forged", True),))),
        ):
            with self.subTest(shadow=field_name):
                with self.assertRaises(AttributeError):
                    object.__setattr__(observed, field_name, value)
        self.assertFalse(hasattr(observed, "__dict__"))
        self.assertEqual(
            ScopedInvocationResultObservedV2.to_dict(observed),
            baseline_wire,
        )
        decoded = pickle.loads(pickle.dumps(observed))
        self.assertEqual(decoded, baseline)
        self.assertIs(type(decoded), ScopedInvocationResultObservedV2)

    def test_tampered_receipts_cannot_cross_any_observation_boundary(self) -> None:
        receipt = receipt_for()
        object.__setattr__(receipt, "receipt_digest", "0" * 64)
        with self.assertRaisesRegex(ValueError, "receiptDigest"):
            ScopedInvocationResultObservedV2(receipt)

        observed = ScopedInvocationResultObservedV2(receipt_for())
        object.__setattr__(observed.receipt, "receipt_digest", "0" * 64)
        for operation in (
            lambda: ScopedInvocationResultObservedV2.to_dict(observed),
            lambda: copy.copy(observed),
            lambda: copy.deepcopy(observed),
            lambda: pickle.dumps(observed),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "receiptDigest"):
                    operation()

    def test_object_new_cannot_turn_a_tampered_receipt_into_an_observation(self) -> None:
        receipt = receipt_for()
        object.__setattr__(receipt, "receipt_digest", "0" * 64)
        observed = object.__new__(ScopedInvocationResultObservedV2)
        object.__setattr__(observed, "receipt", receipt)

        for operation in (
            lambda: ScopedInvocationResultObservedV2.to_dict(observed),
            lambda: copy.copy(observed),
            lambda: copy.deepcopy(observed),
            lambda: pickle.dumps(observed),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "receiptDigest"):
                    operation()

    def test_observation_stays_internal_until_store_recovery_uses_it(self) -> None:
        name = "ScopedInvocationResultObservedV2"
        self.assertNotIn(name, invocation_results_module.__all__)
        self.assertNotIn(name, quantum_entanglement.__all__)
        self.assertFalse(hasattr(quantum_entanglement, name))
        self.assertFalse(hasattr(ScopedInvocationResultObservedV2(receipt_for()), "accepted"))
        self.assertIs(
            type(ScopedInvocationResultObservedV2(receipt_for()).receipt),
            ScopedInvocationResultReceiptV2,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
