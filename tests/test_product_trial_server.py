from __future__ import annotations

import http.client
import json
import queue
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from examples.product_trial_server import build_server


class _FakeTrialRuntime:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.secret = "sk-test-secret-that-must-never-leak"
        self.run_error: Exception | None = None
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()

    def describe(self) -> dict[str, object]:
        return {
            "mode": "model-generated",
            "provider": "test-openai-compatible",
            "model": "test-gpt-model",
            "status": "ready",
        }

    def run(self, instruction: str) -> dict[str, object]:
        self.instructions.append(instruction)
        self.started.set()
        if self.block and not self.release.wait(timeout=5):
            raise TimeoutError("fake runtime was not released")
        if self.run_error is not None:
            raise self.run_error
        return {
            "instruction": instruction,
            "run": {
                "sessionId": "fake-session",
                "statuses": {"research": "completed"},
            },
            "events": [],
        }


class LocalProductTrialServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "bounded-local-trial-token"
        self.runtime = _FakeTrialRuntime()
        self.server = build_server(port=0, token=self.token, runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = self.server.public_origin

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        token: str | None = None,
        origin: str | None = None,
        fetch_site: str | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        headers = {}
        if token is not None:
            headers["X-QE-Trial-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        if fetch_site is not None:
            headers["Sec-Fetch-Site"] = fetch_site
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(self.origin + path, method=method, headers=headers, data=data)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, dict(response.headers.items())

    def post_json(
        self,
        payload: object,
        *,
        token: str | None = None,
        origin: str | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        return self.request_json(
            "/api/demo",
            method="POST",
            token=self.token if token is None else token,
            origin=origin,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    def test_static_page_is_offline_and_hardened(self) -> None:
        with urlopen(self.origin + "/", timeout=5) as response:
            body = response.read().decode("utf-8")
            headers = response.headers
        self.assertEqual(response.status, 200)
        self.assertIn("群聊不是入口", body)
        self.assertNotIn("https://", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_static_page_contains_the_product_views_and_inline_diagrams(self) -> None:
        with urlopen(self.origin + "/", timeout=5) as response:
            body = response.read().decode("utf-8")
        for element_id in (
            "workbench",
            "orchestrator-status",
            "task-research",
            "artifact-list",
            "artifact-preview",
            "artifact-preview-name",
            "artifact-preview-content",
            "artifact-download",
            "needs-you-list",
            "event-timeline",
            "architecture-diagram",
            "sequence-diagram",
            "state-diagram",
            "gate-ladder",
            "raw-output",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', body)
        self.assertGreaterEqual(body.count("<svg"), 3)
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("eval(", body)
        self.assertNotIn("<script src=", body)
        self.assertNotIn("<link rel=", body)
        self.assertIn("URL.createObjectURL", body)
        self.assertIn("download", body)

    def test_favicon_is_deliberately_empty_instead_of_logging_a_404(self) -> None:
        with urlopen(self.origin + "/favicon.ico", timeout=5) as response:
            body = response.read()
        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")

    def test_health_and_demo_require_the_ephemeral_token(self) -> None:
        status, payload, _ = self.request_json("/api/health")
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "trial_token_invalid"})

        status, payload, _ = self.request_json(
            "/api/demo",
            method="POST",
            data=b'{"instruction":"do not run"}',
            content_type="application/json",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "trial_token_invalid"})
        self.assertEqual(self.runtime.instructions, [])

    def test_health_exposes_only_non_sensitive_runtime_identity(self) -> None:
        status, payload, _ = self.request_json("/api/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "status": "ready",
                "runtime": {
                    "mode": "model-generated",
                    "provider": "test-openai-compatible",
                    "model": "test-gpt-model",
                    "status": "ready",
                },
                "externalMessaging": False,
            },
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.runtime.secret, encoded)
        self.assertNotIn("api_key", encoded.lower())

    def test_json_instruction_is_passed_unchanged_to_the_injected_runtime(self) -> None:
        instruction = "研究 Agent 与人协同办公，并给出可核验的 Markdown 产物。"
        status, payload, _ = self.post_json({"instruction": instruction})

        self.assertEqual(status, 200)
        self.assertEqual(self.runtime.instructions, [instruction])
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["mode"], "model-generated")
        self.assertEqual(payload["runtime"], self.runtime.describe())
        boundary = payload["boundary"]
        self.assertIsInstance(boundary, dict)
        assert isinstance(boundary, dict)
        self.assertEqual(
            boundary,
            {
                "externalMessaging": False,
                "gateStatus": "A-E closed",
                "persistentStorage": False,
                "productionApproved": False,
            },
        )
        self.assertEqual(
            payload["result"],
            {
                "instruction": instruction,
                "run": {
                    "sessionId": "fake-session",
                    "statuses": {"research": "completed"},
                },
                "events": [],
            },
        )

    def test_cross_origin_and_wrong_host_fail_closed(self) -> None:
        status, payload, _ = self.request_json(
            "/api/demo",
            method="POST",
            token=self.token,
            origin="https://example.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "origin_not_allowed"})

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "example.invalid")
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 421)
        self.assertEqual(body, {"error": "host_not_allowed"})

    def test_instruction_must_be_non_blank(self) -> None:
        for instruction in ("", "   \n\t", 42, None):
            with self.subTest(instruction=repr(instruction)):
                status, payload, _ = self.post_json({"instruction": instruction})
                self.assertEqual(status, 422)
                self.assertEqual(payload, {"error": "instruction_invalid"})
        self.assertEqual(self.runtime.instructions, [])

    def test_request_json_shape_is_strictly_validated(self) -> None:
        invalid_bodies = (
            b"",
            b"   \n\t",
            b'{"instruction":',
            b"[]",
            b'{"instruction":"valid","debug":true}',
            b"{}",
            b"\xff\xfe",
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                status, payload, _ = self.request_json(
                    "/api/demo",
                    method="POST",
                    token=self.token,
                    data=body,
                    content_type="application/json; charset=utf-8",
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": "request_body_invalid"})
        self.assertEqual(self.runtime.instructions, [])

    def test_default_runtime_remains_the_offline_synthetic_fixture(self) -> None:
        server = build_server(port=0, token="synthetic-compatibility-token")
        try:
            payload = server.trial_state.run("verify the offline fixture")
        finally:
            server.server_close()

        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["mode"], "synthetic")
        self.assertEqual(
            payload["runtime"],
            {
                "mode": "synthetic",
                "provider": "local",
                "model": "deterministic-fixture",
                "status": "ready",
            },
        )
        result = payload["result"]
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["instruction"], "verify the offline fixture")
        run = result["run"]
        self.assertIsInstance(run, dict)
        assert isinstance(run, dict)
        self.assertEqual(
            run["statuses"],
            {"research": "completed", "design": "completed", "review": "completed"},
        )
        artifacts = {
            artifact["name"]: artifact
            for artifact in run["artifacts"]
            if isinstance(artifact, dict)
        }
        self.assertEqual(
            set(artifacts),
            {"architecture.md", "protocol-notes.md", "review.md"},
        )
        for artifact in artifacts.values():
            self.assertEqual(artifact["mediaType"], "text/markdown")
            self.assertTrue(str(artifact["digest"]).startswith("sha256:"))
            self.assertIsInstance(artifact["content"], str)
            self.assertTrue(artifact["content"])
        self.assertEqual(len(result["events"]), 25)

    def test_request_requires_json_content_type(self) -> None:
        body = b'{"instruction":"must not run"}'
        for content_type in (
            None,
            "text/plain",
            "application/x-www-form-urlencoded",
            "application/json; charset=iso-8859-1",
        ):
            with self.subTest(content_type=content_type):
                status, payload, _ = self.request_json(
                    "/api/demo",
                    method="POST",
                    token=self.token,
                    data=body,
                    content_type=content_type,
                )
                self.assertEqual(status, 415)
                self.assertEqual(payload, {"error": "content_type_invalid"})
        self.assertEqual(self.runtime.instructions, [])

    def test_instruction_character_limit_is_inclusive(self) -> None:
        maximum_instruction = "界" * 12_000
        status, payload, _ = self.post_json({"instruction": maximum_instruction})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["instruction"], maximum_instruction)  # type: ignore[index]

        status, payload, _ = self.post_json({"instruction": "界" * 12_001})
        self.assertEqual(status, 413)
        self.assertEqual(payload, {"error": "request_too_large"})
        self.assertEqual(self.runtime.instructions, [maximum_instruction])

    def test_encoded_request_body_is_bounded_before_runtime_execution(self) -> None:
        # JSON escaping makes this body exceed 65,536 bytes while the decoded
        # instruction remains comfortably below the 12,000-character limit.
        body = json.dumps({"instruction": "😀" * 6_000}).encode("utf-8")
        self.assertGreater(len(body), 65_536)
        status, payload, _ = self.request_json(
            "/api/demo",
            method="POST",
            token=self.token,
            data=body,
            content_type="application/json",
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload, {"error": "request_too_large"})
        self.assertEqual(self.runtime.instructions, [])

    def test_runtime_failure_is_redacted_from_the_http_response(self) -> None:
        self.runtime.run_error = RuntimeError(f"upstream rejected credential {self.runtime.secret}")
        status, payload, headers = self.post_json({"instruction": "trigger failure"})
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "trial_run_failed"})
        public_response = json.dumps(
            {"payload": payload, "headers": headers},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(self.runtime.secret, public_response)
        self.assertNotIn("upstream rejected credential", public_response)

    def test_fetch_metadata_accepts_same_origin_and_rejects_other_sites(self) -> None:
        status, payload, _ = self.request_json(
            "/api/health",
            token=self.token,
            origin=self.origin,
            fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")

        for fetch_site in ("same-site", "cross-site"):
            with self.subTest(fetch_site=fetch_site):
                status, payload, _ = self.request_json(
                    "/api/health",
                    token=self.token,
                    origin=self.origin,
                    fetch_site=fetch_site,
                )
                self.assertEqual(status, 403)
                self.assertEqual(payload, {"error": "fetch_site_not_allowed"})

    def test_transfer_encoding_is_rejected_before_demo_execution(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=5,
        )
        try:
            connection.putrequest("POST", "/api/demo")
            connection.putheader("X-QE-Trial-Token", self.token)
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(body, {"error": "request_framing_invalid"})

    def test_only_one_demo_run_is_admitted_at_a_time(self) -> None:
        self.runtime.block = True
        first_outcome: queue.Queue[
            tuple[int, dict[str, object], dict[str, str]] | BaseException
        ] = queue.Queue()

        def run_first_request() -> None:
            try:
                first_outcome.put(self.post_json({"instruction": "first instruction"}))
            except BaseException as error:
                first_outcome.put(error)

        first_thread = threading.Thread(target=run_first_request, daemon=True)
        first_thread.start()
        self.assertTrue(self.runtime.started.wait(timeout=2), "first runtime call did not start")
        try:
            status, payload, _ = self.post_json({"instruction": "second instruction"})
        finally:
            self.runtime.release.set()
            first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "trial_run_busy"})

        completed = first_outcome.get_nowait()
        if isinstance(completed, BaseException):
            raise completed
        self.assertIsInstance(completed, tuple)
        first_status, first_payload, _first_headers = completed
        self.assertEqual(first_status, 200)
        first_result = first_payload["result"]
        self.assertIsInstance(first_result, dict)
        assert isinstance(first_result, dict)
        self.assertEqual(first_result["instruction"], "first instruction")
        self.assertEqual(self.runtime.instructions, ["first instruction"])


if __name__ == "__main__":
    unittest.main()
