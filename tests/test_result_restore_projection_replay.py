from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
from quantum_entanglement.result_backup import create_result_backup, restore_result_backup
from quantum_entanglement.store import SQLiteEventStore


class ResultRestoreProjectionReplayTests(unittest.TestCase):
    def test_restored_result_graph_replays_projection_in_a_clean_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            store = SQLiteEventStore(
                str(source),
                clock=lambda: "2026-08-27T10:00:00.000000Z",
                enable_result_acceptance_schema=True,
            )
            helper = durable_prerequisites.ResultAcceptanceDurablePrerequisiteTests(
                methodName="runTest"
            )
            helper.store = store
            prepared = helper.fresh_prepared()
            store._clock = lambda: "2026-08-27T10:00:02.000000Z"
            with patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_clean_replay",
                    "event_result_clean_replay",
                    "event_terminal_clean_replay",
                ),
            ):
                store.accept_scoped_invocation_result_v2(
                    prepared.request,
                    prepared.claimed,
                )
            store.close()

            create_result_backup(
                source,
                backup,
                clock=lambda: "2026-08-29T12:00:00.000000Z",
            )
            restore_result_backup(backup, restored)

            repository_root = Path(__file__).resolve().parents[1]
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import json
import sys
from quantum_entanglement.result_projection import SQLiteResultProjectionStore
from quantum_entanglement.store import SQLiteEventStore

store = SQLiteEventStore(sys.argv[1], enable_result_acceptance_schema=True)
projection = SQLiteResultProjectionStore(store, sys.argv[1], owner_id="clean-process-replay")
try:
    projection.run_once(limit=1000)
    identity = store._connection.execute(
        "SELECT tenant_id, workspace_id, invocation_id FROM invocation_result_receipts"
    ).fetchone()
    if identity is None:
        raise RuntimeError("restored result receipt is missing")
    view = projection.read(
        identity["tenant_id"], identity["workspace_id"], identity["invocation_id"]
    )
    if view is None:
        raise RuntimeError("restored result projection is missing")
    if view.status.value != "completed":
        raise RuntimeError("restored result projection is not terminal")
    print(json.dumps({"projected": True, "status": view.status.value, "replayed": True}))
finally:
    projection.close()
    store.close()
""",
                    str(restored),
                ],
                cwd=repository_root,
                env={**os.environ, "PYTHONPATH": str(repository_root / "src")},
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(
                json.loads(child.stdout),
                {"projected": True, "status": "completed", "replayed": True},
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
