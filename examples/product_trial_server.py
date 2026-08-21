"""Serve the synthetic coordination demo on a token-bound loopback endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import secrets
import threading
import webbrowser
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from quantum_entanglement.cli import run_demo as run_kernel_demo

_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765
_MAX_PATH_BYTES = 2_048
_INDEX_PATH = Path(__file__).with_name("product_trial") / "index.html"
_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'"
)


class _TrialBusyError(RuntimeError):
    pass


class _TrialState:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("trial token must not be blank")
        self.token = token
        self.run_lock = threading.Lock()

    def run(self) -> dict[str, object]:
        if not self.run_lock.acquire(blocking=False):
            raise _TrialBusyError
        try:
            result = asyncio.run(run_kernel_demo())
        finally:
            self.run_lock.release()
        return {
            "schemaVersion": 1,
            "mode": "local-synthetic-demo",
            "boundary": {
                "externalMessaging": False,
                "persistentStorage": False,
                "productionApproved": False,
                "gateStatus": "A-E closed",
            },
            "result": result,
        }


class LocalTrialServer(ThreadingHTTPServer):
    """A loopback-only HTTP server carrying one ephemeral browser token."""

    daemon_threads = True

    def __init__(self, port: int, token: str) -> None:
        if not 0 <= port <= 65_535:
            raise ValueError("port must be between 0 and 65535")
        self.trial_state = _TrialState(token)
        super().__init__((_LOOPBACK_HOST, port), LocalTrialRequestHandler)

    @property
    def public_origin(self) -> str:
        return f"http://{_LOOPBACK_HOST}:{self.server_port}"


class LocalTrialRequestHandler(BaseHTTPRequestHandler):
    """Serve the static trial page and one bounded demo operation."""

    protocol_version = "HTTP/1.0"
    server_version = "QuantumEntanglementTrial/1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_metadata_is_allowed():
            return
        route = self._route()
        if route in ("/", "/index.html"):
            try:
                body = _INDEX_PATH.read_bytes()
            except OSError:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "trial_page_unavailable"},
                )
                return
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if route == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if route == "/api/health":
            if not self._token_is_valid():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "trial_token_invalid"})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ready",
                    "mode": "local-synthetic-demo",
                    "externalMessaging": False,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_metadata_is_allowed():
            return
        if self._route() != "/api/demo":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        if not self._token_is_valid():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "trial_token_invalid"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_framing_invalid"})
            return
        length_text = self.headers.get("Content-Length", "0")
        try:
            content_length = int(length_text)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_length_invalid"})
            return
        if content_length != 0:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body_not_allowed"})
            return
        try:
            payload = self._trial_server().trial_state.run()
        except _TrialBusyError:
            self._send_json(HTTPStatus.CONFLICT, {"error": "trial_run_busy"})
            return
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "trial_run_failed"})
            return
        self._send_json(HTTPStatus.OK, payload)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _trial_server(self) -> LocalTrialServer:
        return cast(LocalTrialServer, self.server)

    def _route(self) -> str:
        if len(self.path.encode("utf-8", errors="ignore")) > _MAX_PATH_BYTES:
            return ""
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            return ""
        return parsed.path

    def _request_metadata_is_allowed(self) -> bool:
        server = self._trial_server()
        expected_host = f"{_LOOPBACK_HOST}:{server.server_port}"
        if self.headers.get("Host") != expected_host:
            self._send_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "host_not_allowed"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != server.public_origin:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "origin_not_allowed"})
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site not in (None, "none", "same-origin"):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "fetch_site_not_allowed"})
            return False
        return True

    def _token_is_valid(self) -> bool:
        supplied = self.headers.get("X-QE-Trial-Token", "")
        return hmac.compare_digest(supplied, self._trial_server().trial_state.token)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def build_server(*, port: int, token: str) -> LocalTrialServer:
    """Build a loopback-only trial server; port 0 selects an ephemeral test port."""

    return LocalTrialServer(port, token)


def _positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_positive_port, default=_DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the trial page in a browser")
    args = parser.parse_args(argv)

    token = secrets.token_urlsafe(32)
    server = build_server(port=args.port, token=token)
    trial_url = f"{server.public_origin}/#token={token}"
    print("Quantum Entanglement 本地体验已启动（仅合成数据，不连接任何聊天平台）")
    print(trial_url)
    print("按 Ctrl-C 停止。")
    if args.open:
        threading.Timer(0.25, webbrowser.open, args=(trial_url,)).start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
