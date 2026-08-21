from __future__ import annotations

import http.client
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from examples.product_trial_server import build_server


class LocalProductTrialServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "bounded-local-trial-token"
        self.server = build_server(port=0, token=self.token)
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
        data: bytes | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        headers = {}
        if token is not None:
            headers["X-QE-Trial-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(self.origin + path, method=method, headers=headers, data=data)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, dict(response.headers.items())

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
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "local-synthetic-demo")
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
        result = payload["result"]
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        run = result["run"]
        self.assertIsInstance(run, dict)
        assert isinstance(run, dict)
        self.assertEqual(
            run["statuses"],
            {"research": "completed", "design": "completed", "review": "completed"},
        )
        self.assertEqual(len(run["artifacts"]), 3)
        self.assertEqual(len(result["events"]), 25)

    def test_cross_origin_wrong_host_and_request_bodies_fail_closed(self) -> None:
        status, payload, _ = self.request_json(
            "/api/demo",
            method="POST",
            token=self.token,
            origin="https://example.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "origin_not_allowed"})

        status, payload, _ = self.request_json(
            "/api/demo",
            method="POST",
            token=self.token,
            data=b"{}",
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload, {"error": "body_not_allowed"})

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "example.invalid")
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 421)
        self.assertEqual(body, {"error": "host_not_allowed"})

    def test_only_one_demo_run_is_admitted_at_a_time(self) -> None:
        self.assertTrue(self.server.trial_state.run_lock.acquire(blocking=False))
        try:
            status, payload, _ = self.request_json(
                "/api/demo",
                method="POST",
                token=self.token,
            )
        finally:
            self.server.trial_state.run_lock.release()
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "trial_run_busy"})


if __name__ == "__main__":
    unittest.main()
