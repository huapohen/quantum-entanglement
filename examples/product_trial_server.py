"""Serve the local coordination trial on a token-bound loopback endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import sys
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from quantum_entanglement.cli import run_demo as run_kernel_demo
from quantum_entanglement.product_trial import ProductTrialRunError, run_custom_instruction

_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765
_MAX_PATH_BYTES = 2_048
_MAX_REQUEST_BYTES = 65_536
_MAX_INSTRUCTION_CHARS = 12_000
_INDEX_PATH = Path(__file__).with_name("product_trial") / "index.html"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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


class _TrialRuntime(Protocol):
    def describe(self) -> dict[str, object]: ...

    def run(self, instruction: str) -> dict[str, object]: ...


class _SyntheticTrialRuntime:
    def describe(self) -> dict[str, object]:
        return {
            "mode": "synthetic",
            "provider": "local",
            "model": "deterministic-fixture",
            "status": "ready",
        }

    def run(self, instruction: str) -> dict[str, object]:
        result = asyncio.run(run_kernel_demo())
        result["instruction"] = instruction
        return result


@dataclass(frozen=True)
class _ModelSettings:
    api_key: str = field(repr=False)
    base_url: str
    model: str


class _GptTrialRuntime:
    def __init__(self, settings: _ModelSettings) -> None:
        self._settings = settings

    def describe(self) -> dict[str, object]:
        return {
            "mode": "model",
            "provider": "openai-compatible",
            "model": self._settings.model,
            "status": "configured",
        }

    def run(self, instruction: str) -> dict[str, object]:
        from quantum_entanglement.adapters.openai_responses import (
            OpenAIResponsesConfig,
            OpenAIResponsesRuntime,
        )

        async def execute() -> dict[str, object]:
            # Python 3.9 binds asyncio synchronization primitives at construction.
            # Build the runtime after asyncio.run() owns this HTTP worker thread.
            runtime = OpenAIResponsesRuntime(
                OpenAIResponsesConfig(
                    api_key=self._settings.api_key,
                    base_url=self._settings.base_url,
                    model=self._settings.model,
                    timeout_seconds=300,
                    max_response_bytes=16 * 1_024 * 1_024,
                )
            )
            return await run_custom_instruction(instruction, runtime)

        return asyncio.run(execute())


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read a deliberately small dotenv subset without evaluating shell syntax."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    if len(raw) > 64 * 1_024:
        raise RuntimeError("model_configuration_file_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("model_configuration_file_invalid") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise RuntimeError("model_configuration_file_invalid")
        name, value = stripped.split("=", 1)
        if name not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"}:
            continue
        if name in values:
            raise RuntimeError("model_configuration_duplicate_field")
        values[name] = value
    return values


def _load_model_settings(
    *,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> _ModelSettings:
    file_values = _read_dotenv(dotenv_path or (_REPOSITORY_ROOT / ".env"))
    ambient = os.environ if environ is None else environ
    names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    ambient_fields = {name for name in names if name in ambient}
    if ambient_fields and ambient_fields != set(names):
        raise RuntimeError("model_configuration_environment_bundle_incomplete")
    source = ambient if ambient_fields else file_values
    values = {name: source.get(name, "") for name in names}
    for name, value in values.items():
        if not value or value != value.strip() or any(c in value for c in "\x00\r\n"):
            raise RuntimeError(f"model_configuration_missing_or_invalid:{name}")
    parsed = urlsplit(values["OPENAI_BASE_URL"])
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("model_configuration_base_url_invalid")
    return _ModelSettings(
        api_key=values["OPENAI_API_KEY"],
        base_url=values["OPENAI_BASE_URL"].rstrip("/"),
        model=values["OPENAI_MODEL"],
    )


class _TrialState:
    def __init__(self, token: str, runtime: _TrialRuntime) -> None:
        if not token:
            raise ValueError("trial token must not be blank")
        self.token = token
        self.runtime = runtime
        self.run_lock = threading.Lock()

    def run(self, instruction: str) -> dict[str, object]:
        if not self.run_lock.acquire(blocking=False):
            raise _TrialBusyError
        try:
            result = self.runtime.run(instruction)
        finally:
            self.run_lock.release()
        runtime = self.runtime.describe()
        return {
            "schemaVersion": 2,
            "mode": runtime["mode"],
            "runtime": runtime,
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

    def __init__(self, port: int, token: str, runtime: _TrialRuntime | None = None) -> None:
        if not 0 <= port <= 65_535:
            raise ValueError("port must be between 0 and 65535")
        self.trial_state = _TrialState(token, runtime or _SyntheticTrialRuntime())
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
                    "runtime": self._trial_server().trial_state.runtime.describe(),
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
        content_type = self.headers.get("Content-Type", "")
        content_type_parts = [part.strip() for part in content_type.split(";")]
        media_type = content_type_parts[0].lower()
        charset_values = [
            part.split("=", 1)[1].strip().strip('"').lower()
            for part in content_type_parts[1:]
            if part.lower().startswith("charset=")
        ]
        if media_type != "application/json" or any(
            value not in {"utf-8", "utf8"} for value in charset_values
        ):
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "content_type_invalid"})
            return
        length_text = self.headers.get("Content-Length", "")
        try:
            content_length = int(length_text)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_length_invalid"})
            return
        if content_length < 1:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_body_invalid"})
            return
        if content_length > _MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
            return
        body = self.rfile.read(content_length)
        try:
            request_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_body_invalid"})
            return
        if not isinstance(request_payload, dict) or set(request_payload) != {"instruction"}:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_body_invalid"})
            return
        instruction = request_payload["instruction"]
        if not isinstance(instruction, str) or not instruction.strip():
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "instruction_invalid"})
            return
        if len(instruction) > _MAX_INSTRUCTION_CHARS:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
            return
        try:
            payload = self._trial_server().trial_state.run(instruction.strip())
        except _TrialBusyError:
            self._send_json(HTTPStatus.CONFLICT, {"error": "trial_run_busy"})
            return
        except Exception as exc:
            if isinstance(exc, ProductTrialRunError):
                print(
                    "model-backed trial failed: "
                    + json.dumps(exc.task_errors, ensure_ascii=False, sort_keys=True),
                    file=sys.stderr,
                )
            else:
                print(f"trial runtime failed: {type(exc).__name__}", file=sys.stderr)
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


def build_server(
    *,
    port: int,
    token: str,
    runtime: _TrialRuntime | None = None,
) -> LocalTrialServer:
    """Build a loopback-only trial server; port 0 selects an ephemeral test port."""

    return LocalTrialServer(port, token, runtime)


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
    parser.add_argument(
        "--runtime",
        choices=("gpt", "synthetic"),
        default="gpt",
        help="run the configured GPT model or the deterministic offline fixture",
    )
    args = parser.parse_args(argv)

    token = secrets.token_urlsafe(32)
    runtime: _TrialRuntime
    if args.runtime == "gpt":
        runtime = _GptTrialRuntime(_load_model_settings())
    else:
        runtime = _SyntheticTrialRuntime()
    server = build_server(port=args.port, token=token, runtime=runtime)
    trial_url = f"{server.public_origin}/#token={token}"
    description = runtime.describe()
    print(
        "Quantum Entanglement 本地体验已启动"
        f"（{description['provider']} / {description['model']}，不连接任何聊天平台）"
    )
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
