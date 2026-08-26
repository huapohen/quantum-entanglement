from __future__ import annotations

import unittest
from asyncio import CancelledError
from typing import Callable, NoReturn

import quantum_entanglement.store as store_module
from quantum_entanglement.store import (
    InvocationAdmissionCommitAmbiguityError,
    InvocationStartCommitAmbiguityError,
    InvocationStartConflictError,
    InvocationStartTransactionError,
)


def store_traceback_locals(error: BaseException) -> str:
    """Render only store-owned traceback locals, where caller secrets must be absent."""

    pending = [error]
    seen: set[int] = set()
    values: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_code.co_filename == store_module.__file__:
                values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(values)


def trusted_start_control(
    kind: str,
    *,
    ambiguity: bool,
    system_exit_code: object = None,
) -> Callable[[str], NoReturn]:
    descriptor = store_module._EventStoreControlDescriptor(kind, system_exit_code)

    @store_module._sanitize_invocation_start_controls
    def operation(secret_canary: str) -> NoReturn:
        retained_only_in_unwound_frame = secret_canary
        if not retained_only_in_unwound_frame:  # pragma: no cover - test input is non-empty.
            raise AssertionError("missing canary")
        raise store_module._EventStoreStartControlSignal(
            descriptor,
            ambiguity=ambiguity,
            token=store_module._EVENT_STORE_START_CONTROL_TOKEN,
        ) from None

    return operation


class InvocationStartControlBoundaryTests(unittest.TestCase):
    def test_fixed_start_errors_have_stable_codes_and_no_variable_constructor(self) -> None:
        cases = (
            (
                InvocationStartConflictError,
                "invocation_start_conflict",
                "invocation start is not bound to one canonical durable state",
            ),
            (
                InvocationStartTransactionError,
                "invocation_start_transaction_failed",
                "invocation start transaction was rolled back",
            ),
            (
                InvocationStartCommitAmbiguityError,
                "invocation_start_commit_ambiguous",
                "invocation start commit outcome is unknown; reopen the store and observe "
                "the durable start receipt",
            ),
        )
        for error_type, code, message in cases:
            with self.subTest(error_type=error_type.__name__):
                error = error_type()
                self.assertEqual(error.code, code)
                self.assertEqual(str(error), message)
                with self.assertRaises(TypeError):
                    error_type("caller-controlled detail")  # type: ignore[call-arg]

    def test_ambiguous_controls_use_fresh_signal_and_start_scoped_direct_cause(self) -> None:
        cases: tuple[tuple[str, type[BaseException], object], ...] = (
            ("keyboard_interrupt", KeyboardInterrupt, None),
            ("generator_exit", GeneratorExit, None),
            ("cancelled", CancelledError, None),
            ("system_exit", SystemExit, 37),
        )
        for kind, control_type, system_exit_code in cases:
            with self.subTest(kind=kind):
                canary = f"raw-lease-{kind}-secret-canary"
                operation = trusted_start_control(
                    kind,
                    ambiguity=True,
                    system_exit_code=system_exit_code,
                )
                with self.assertRaises(control_type) as caught:
                    operation(canary)
                self.assertEqual(str(caught.exception), "37" if kind == "system_exit" else "")
                self.assertIsNone(caught.exception.__context__)
                self.assertIs(type(caught.exception.__cause__), InvocationStartCommitAmbiguityError)
                self.assertIsNone(caught.exception.__cause__.__traceback__)
                self.assertIsNot(
                    type(caught.exception.__cause__),
                    InvocationAdmissionCommitAmbiguityError,
                )
                self.assertNotIn(canary, store_traceback_locals(caught.exception))

    def test_rolled_back_controls_have_no_ambiguity_cause(self) -> None:
        canary = "rolled-back-control-raw-lease-secret-canary"
        operation = trusted_start_control("keyboard_interrupt", ambiguity=False)

        with self.assertRaises(KeyboardInterrupt) as caught:
            operation(canary)

        self.assertEqual(str(caught.exception), "")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(canary, store_traceback_locals(caught.exception))

    def test_direct_control_is_reissued_without_private_message_or_frame(self) -> None:
        canary = "direct-control-raw-lease-secret-canary"

        @store_module._sanitize_invocation_start_controls
        def operation(secret_canary: str) -> NoReturn:
            raise KeyboardInterrupt(secret_canary)

        with self.assertRaises(KeyboardInterrupt) as caught:
            operation(canary)

        self.assertEqual(str(caught.exception), "")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(canary, store_traceback_locals(caught.exception))

    def test_fixed_error_signal_unwinds_capability_frame_before_publication(self) -> None:
        cases: tuple[tuple[str, type[BaseException]], ...] = (
            ("conflict", InvocationStartConflictError),
            ("transaction", InvocationStartTransactionError),
            ("ambiguous", InvocationStartCommitAmbiguityError),
        )
        for kind, error_type in cases:
            with self.subTest(kind=kind):
                canary = f"fixed-{kind}-raw-lease-secret-canary"

                @store_module._sanitize_invocation_start_controls
                def operation(secret_canary: str, error_kind: str = kind) -> NoReturn:
                    retained_only_in_unwound_frame = secret_canary
                    if not retained_only_in_unwound_frame:  # pragma: no cover
                        raise AssertionError("missing canary")
                    raise store_module._EventStoreStartErrorSignal(
                        error_kind,
                        token=store_module._EVENT_STORE_START_CONTROL_TOKEN,
                    ) from None

                with self.assertRaises(error_type) as caught:
                    operation(canary)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(canary, store_traceback_locals(caught.exception))

    def test_direct_fixed_error_is_recreated_after_its_inner_frame_unwinds(self) -> None:
        canary = "direct-fixed-error-raw-lease-secret-canary"

        @store_module._sanitize_invocation_start_controls
        def operation(secret_canary: str) -> NoReturn:
            retained_only_in_unwound_frame = secret_canary
            if not retained_only_in_unwound_frame:  # pragma: no cover
                raise AssertionError("missing canary")
            raise InvocationStartConflictError()

        with self.assertRaises(InvocationStartConflictError) as caught:
            operation(canary)

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(canary, store_traceback_locals(caught.exception))

    def test_forged_private_signals_are_never_upgraded_to_public_outcomes(self) -> None:
        @store_module._sanitize_invocation_start_controls
        def forged_error() -> NoReturn:
            raise store_module._EventStoreStartErrorSignal(
                "conflict",
                token=object(),
            )

        @store_module._sanitize_invocation_start_controls
        def forged_control() -> NoReturn:
            raise store_module._EventStoreStartControlSignal(
                store_module._EventStoreControlDescriptor("keyboard_interrupt"),
                ambiguity=False,
                token=object(),
            )

        with self.assertRaises(store_module._EventStoreStartErrorSignal):
            forged_error()
        with self.assertRaises(store_module._EventStoreStartControlSignal):
            forged_control()


if __name__ == "__main__":
    unittest.main()
