from __future__ import annotations

import asyncio
import json
import threading
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from quantum_entanglement.adapters.openai_responses import (
    OpenAIResponsesAPIError,
    OpenAIResponsesConfig,
    OpenAIResponsesHTTPError,
    OpenAIResponsesLimitError,
    OpenAIResponsesProtocolError,
    OpenAIResponsesRuntime,
    OpenAIResponsesTransportError,
)
from quantum_entanglement.agent_runtime import (
    AgentInvocation,
    AgentRuntimeClosedError,
    AgentRuntimePort,
)
from quantum_entanglement.context import ContextBundle, ContextItem
from quantum_entanglement.protocol import (
    ActorKind,
    ActorRef,
    CoordinationEnvelope,
    EnvelopeKind,
    HandoffContract,
)
from quantum_entanglement.scheduler import TaskSpec

Responder = Callable[[BaseHTTPRequestHandler, dict[str, Any]], None]


class _FakeResponsesServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeResponsesHandler)
        self.responder: Responder | None = None
        self.requests: list[dict[str, Any]] = []


class _FakeResponsesHandler(BaseHTTPRequestHandler):
    server: _FakeResponsesServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        observation = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
            "body": body,
        }
        self.server.requests.append(observation)
        responder = self.server.responder
        if responder is None:
            self.send_error(500)
            return
        responder(self, body)

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _send_sse(handler: BaseHTTPRequestHandler, events: list[dict[str, Any] | str]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    try:
        for event in events:
            data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            handler.wfile.write(f"data: {data}\n\n".encode())
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _make_invocation() -> AgentInvocation:
    handoff = HandoffContract(
        goal="执行用户发布的自定义指令",
        acceptance_criteria=("返回模型生成内容",),
        deliverables=("answer.md",),
        constraints=("不得泄露凭据",),
    )
    task = TaskSpec(
        "分析量子纠缠协同产品",
        "researcher",
        handoff,
        task_id="task-custom-1",
    )
    sender = ActorRef("orchestrator", "Orchestrator", ActorKind.SYSTEM)
    recipient = ActorRef("researcher", "Researcher", ActorKind.AGENT)
    envelope = CoordinationEnvelope.create(
        session_id="session-custom",
        thread_id="thread-custom",
        sender=sender,
        recipients=(recipient,),
        kind=EnvelopeKind.TASK_ASSIGN,
        payload={"task": task.to_dict()},
        correlation_id="correlation-custom",
        causation_id="cause-custom",
        idempotency_key="invoke:custom-1",
        authority=handoff.authority,
    )
    item = ContextItem(
        "context-custom",
        "instruction",
        "请比较人与多智能体协作的关键差异。",
        required=True,
        provenance="user-input",
    )
    context = ContextBundle((item,), (), 1_000, item.estimated_tokens, "sha256:custom")
    return AgentInvocation(task, envelope, context)


class OpenAIResponsesRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = _FakeResponsesServer()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.api_key = "sk-test-secret-that-must-never-leak"
        self.release = threading.Event()

    async def asyncTearDown(self) -> None:
        self.release.set()
        await asyncio.to_thread(self.server.shutdown)
        await asyncio.to_thread(self.server.server_close)
        self.server_thread.join(timeout=2)

    def config(self, **overrides: Any) -> OpenAIResponsesConfig:
        values: dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "model": "gpt-5.6-sol",
            "timeout_seconds": 2,
            "max_response_bytes": 64 * 1024,
        }
        values.update(overrides)
        return OpenAIResponsesConfig(**values)

    async def test_streams_custom_invocation_and_maps_usage_metadata(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            _send_sse(
                handler,
                [
                    {"type": "response.created", "response": {"id": "resp_custom"}},
                    {"type": "response.output_text.delta", "delta": "模型生成的"},
                    {"type": "response.output_text.delta", "delta": "自定义结果"},
                    {"type": "response.output_text.done", "text": "模型生成的自定义结果"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_custom",
                            "usage": {
                                "input_tokens": 41,
                                "output_tokens": 9,
                                "total_tokens": 50,
                                "input_tokens_details": {"cached_tokens": 3},
                                "output_tokens_details": {"reasoning_tokens": 2},
                            },
                        },
                    },
                    "[DONE]",
                ],
            )

        self.server.responder = respond
        config = self.config()
        runtime = OpenAIResponsesRuntime(config)
        self.assertIsInstance(runtime, AgentRuntimePort)

        result = await runtime.invoke(_make_invocation())

        self.assertEqual(result.narration, "模型生成的自定义结果")
        self.assertEqual(result.metadata["runtime"], "openai-responses")
        self.assertEqual(result.metadata["provider"], "openai")
        self.assertEqual(result.metadata["model"], "gpt-5.6-sol")
        self.assertEqual(result.metadata["responseId"], "resp_custom")
        self.assertEqual(
            result.metadata["usage"],
            {
                "input_tokens": 41,
                "output_tokens": 9,
                "total_tokens": 50,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        )
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/responses")
        self.assertEqual(request["authorization"], f"Bearer {self.api_key}")
        self.assertEqual(request["accept"], "text/event-stream")
        self.assertEqual(request["body"]["model"], "gpt-5.6-sol")
        self.assertIs(request["body"]["stream"], True)
        self.assertEqual(
            request["body"]["input"],
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": OpenAIResponsesRuntime.render_input(_make_invocation()),
                        }
                    ],
                }
            ],
        )
        prompt = request["body"]["input"][0]["content"][0]["text"]
        self.assertIn("执行用户发布的自定义指令", prompt)
        self.assertIn("请比较人与多智能体协作", prompt)
        self.assertNotIn(self.api_key, repr(config))
        self.assertNotIn(self.api_key, repr(runtime))
        await runtime.close()

    async def test_http_error_does_not_expose_provider_body_or_key(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            diagnostic = f'{{"error":"echoed {self.api_key}"}}'.encode()
            handler.send_response(401)
            handler.send_header("Content-Length", str(len(diagnostic)))
            handler.end_headers()
            handler.wfile.write(diagnostic)

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config())

        with self.assertRaises(OpenAIResponsesHTTPError) as raised:
            await runtime.invoke(_make_invocation())

        self.assertEqual(raised.exception.status, 401)
        self.assertNotIn(self.api_key, str(raised.exception))
        self.assertNotIn(self.api_key, repr(raised.exception))
        await runtime.close()

    async def test_sse_error_does_not_expose_provider_message_or_key(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            _send_sse(
                handler,
                [
                    {
                        "type": "error",
                        "code": self.api_key,
                        "message": f"gateway echoed Authorization: Bearer {self.api_key}",
                    }
                ],
            )

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config())

        with self.assertRaises(OpenAIResponsesAPIError) as raised:
            await runtime.invoke(_make_invocation())

        self.assertNotIn(self.api_key, str(raised.exception))
        self.assertNotIn(self.api_key, repr(raised.exception))
        await runtime.close()

    async def test_stream_above_maximum_response_bytes_fails_closed(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            _send_sse(
                handler,
                [{"type": "response.output_text.delta", "delta": "x" * 1_024}],
            )

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config(max_response_bytes=128))

        with self.assertRaises(OpenAIResponsesLimitError):
            await runtime.invoke(_make_invocation())

        await runtime.close()

    async def test_truncated_stream_with_partial_text_never_becomes_success(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            _send_sse(
                handler,
                [{"type": "response.output_text.delta", "delta": "partial result"}],
            )

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config())

        with self.assertRaisesRegex(OpenAIResponsesProtocolError, "response.completed"):
            await runtime.invoke(_make_invocation())

        await runtime.close()

    async def test_timeout_is_redacted_transport_error(self) -> None:
        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            self.release.wait(timeout=1)
            _send_sse(
                handler,
                [{"type": "response.output_text.delta", "delta": self.api_key}],
            )

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config(timeout_seconds=0.05))

        with self.assertRaises(OpenAIResponsesTransportError) as raised:
            await runtime.invoke(_make_invocation())

        self.assertNotIn(self.api_key, str(raised.exception))
        self.release.set()
        await runtime.close()

    async def test_close_drains_accepted_request_and_rejects_new_work(self) -> None:
        started = threading.Event()

        def respond(handler: BaseHTTPRequestHandler, _body: dict[str, Any]) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            started.set()
            self.release.wait(timeout=2)
            try:
                handler.wfile.write(
                    b'data: {"type":"response.output_text.delta","delta":"done"}\n\n'
                    b'data: {"type":"response.completed","response":{}}\n\n'
                )
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        self.server.responder = respond
        runtime = OpenAIResponsesRuntime(self.config())
        invocation_task = asyncio.create_task(runtime.invoke(_make_invocation()))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))

        close_task = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)
        self.assertFalse(close_task.done())
        self.release.set()

        self.assertEqual((await invocation_task).narration, "done")
        await close_task
        await runtime.close()
        with self.assertRaises(AgentRuntimeClosedError):
            await runtime.invoke(_make_invocation())
        self.assertEqual(len(self.server.requests), 1)


if __name__ == "__main__":
    unittest.main()
