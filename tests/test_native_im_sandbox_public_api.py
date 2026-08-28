from __future__ import annotations

import quantum_entanglement
from quantum_entanglement import (
    native_im_sandbox,
    native_im_sandbox_approval,
    native_im_sandbox_approval_store,
    native_im_sandbox_authority,
    native_im_sandbox_composition,
    native_im_sandbox_lifecycle,
    native_im_sandbox_observability,
    native_im_sandbox_provenance,
)
from quantum_entanglement.service.native_im_config import NativeIMDisabledConfigV1


def test_sandbox_modules_are_exactly_reexported_from_the_package_api() -> None:
    modules = (
        native_im_sandbox,
        native_im_sandbox_approval,
        native_im_sandbox_approval_store,
        native_im_sandbox_authority,
        native_im_sandbox_composition,
        native_im_sandbox_lifecycle,
        native_im_sandbox_observability,
        native_im_sandbox_provenance,
    )
    expected_names = tuple(name for module in modules for name in module.__all__)
    assert len(expected_names) == len(set(expected_names))
    for module in modules:
        for name in module.__all__:
            assert name in quantum_entanglement.__all__
            assert getattr(quantum_entanglement, name) is getattr(module, name)


def test_package_export_does_not_register_an_enabled_transport() -> None:
    adapter = quantum_entanglement.compose_default_native_im_sandbox_v1(
        NativeIMDisabledConfigV1(schema_version=1, enabled=False)
    )
    assert type(adapter) is quantum_entanglement.NativeIMDisabledSandboxAdapter
    assert not hasattr(quantum_entanglement, "NativeIMHTTPTransport")
    assert not hasattr(quantum_entanglement, "NativeIMWebSocketTransport")
