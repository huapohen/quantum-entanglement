from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from examples.product_trial_server import _GptTrialRuntime, _load_model_settings, _ModelSettings
from quantum_entanglement.adapters.openai_responses import OpenAIResponsesRuntime


class ProductTrialModelConfigTests(unittest.TestCase):
    def write_env(self, directory: str, content: str) -> Path:
        path = Path(directory) / ".env"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_complete_model_bundle_without_exposing_key_in_repr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_env(
                directory,
                "OPENAI_API_KEY=sk-local-secret\n"
                "OPENAI_BASE_URL=https://gateway.example/v1\n"
                "OPENAI_MODEL=gpt-test\n",
            )

            settings = _load_model_settings(environ={}, dotenv_path=path)

        self.assertEqual(settings.base_url, "https://gateway.example/v1")
        self.assertEqual(settings.model, "gpt-test")
        self.assertNotIn("sk-local-secret", repr(settings))

    def test_explicit_environment_overrides_dotenv_as_one_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_env(
                directory,
                "OPENAI_API_KEY=file-key\n"
                "OPENAI_BASE_URL=https://file.example/v1\n"
                "OPENAI_MODEL=file-model\n",
            )
            settings = _load_model_settings(
                environ={
                    "OPENAI_API_KEY": "environment-key",
                    "OPENAI_BASE_URL": "https://environment.example/v1",
                    "OPENAI_MODEL": "environment-model",
                },
                dotenv_path=path,
            )

        self.assertEqual(settings.api_key, "environment-key")
        self.assertEqual(settings.base_url, "https://environment.example/v1")
        self.assertEqual(settings.model, "environment-model")

    def test_missing_field_fails_closed_without_printing_any_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_env(
                directory,
                "OPENAI_API_KEY=secret-that-must-stay-hidden\n"
                "OPENAI_BASE_URL=https://gateway.example/v1\n",
            )

            with self.assertRaises(RuntimeError) as raised:
                _load_model_settings(environ={}, dotenv_path=path)

        message = str(raised.exception)
        self.assertIn("OPENAI_MODEL", message)
        self.assertNotIn("secret-that-must-stay-hidden", message)
        self.assertNotIn("gateway.example", message)

    def test_partial_environment_bundle_never_mixes_with_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_env(
                directory,
                "OPENAI_API_KEY=file-key\n"
                "OPENAI_BASE_URL=https://file.example/v1\n"
                "OPENAI_MODEL=file-model\n",
            )

            with self.assertRaisesRegex(RuntimeError, "environment_bundle_incomplete"):
                _load_model_settings(
                    environ={"OPENAI_API_KEY": "unmatched-environment-key"},
                    dotenv_path=path,
                )

    def test_insecure_or_ambiguous_base_urls_are_rejected(self) -> None:
        for base_url in (
            "http://gateway.example/v1",
            "https://user:password@gateway.example/v1",
            "https://gateway.example/v1?route=other",
            "https://gateway.example/v1#fragment",
        ):
            with self.subTest(base_url=base_url), tempfile.TemporaryDirectory() as directory:
                path = self.write_env(
                    directory,
                    "OPENAI_API_KEY=test-key\n"
                    f"OPENAI_BASE_URL={base_url}\n"
                    "OPENAI_MODEL=test-model\n",
                )
                with self.assertRaisesRegex(RuntimeError, "base_url_invalid"):
                    _load_model_settings(environ={}, dotenv_path=path)

    def test_duplicate_secret_field_is_rejected_without_value_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_env(
                directory,
                "OPENAI_API_KEY=first-secret\n"
                "OPENAI_API_KEY=second-secret\n"
                "OPENAI_BASE_URL=https://gateway.example/v1\n"
                "OPENAI_MODEL=test-model\n",
            )
            with self.assertRaises(RuntimeError) as raised:
                _load_model_settings(environ={}, dotenv_path=path)

        self.assertEqual(str(raised.exception), "model_configuration_duplicate_field")

    def test_gpt_runtime_constructs_async_primitives_inside_http_worker_loop(self) -> None:
        observed: dict[str, object] = {}

        async def fake_workflow(
            instruction: str,
            runtime: OpenAIResponsesRuntime,
        ) -> dict[str, object]:
            observed["instruction"] = instruction
            observed["runtime"] = runtime
            await runtime.close()
            return {"completed": True}

        runner = _GptTrialRuntime(
            _ModelSettings("test-key", "https://gateway.example/v1", "test-model")
        )
        with (
            patch(
                "examples.product_trial_server.run_custom_instruction",
                side_effect=fake_workflow,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            result = pool.submit(runner.run, "worker-thread instruction").result(timeout=5)

        self.assertEqual(result, {"completed": True})
        self.assertEqual(observed["instruction"], "worker-thread instruction")
        self.assertIsInstance(observed["runtime"], OpenAIResponsesRuntime)


if __name__ == "__main__":
    unittest.main()
