"""Offline bridge contract/security tests; only an ephemeral loopback server is used.

Run with: .engine/Scripts/python.exe -m unittest discover -s tests -p test_bridge.py -v
Regression cases assert safe errors for hostile headers and malformed JSON roles.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("isolated_bridge_contract", ROOT / "bridge.py")
bridge_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge_module)
ModelBridge = bridge_module.ModelBridge


class Context:
    def __init__(self, behavior=None):
        self.requests = []
        self.behavior = behavior

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        if self.behavior:
            return await self.behavior(**kwargs)
        return SimpleNamespace(completion_text="Translated text")


class Request:
    """Minimal in-process request for deterministic cancellation/concurrency tests."""

    def __init__(self, bridge, data):
        self.headers = {"Authorization": "Bearer " + bridge.token}
        self.data = data

    async def json(self):
        return self.data


PAYLOAD = {"messages": [{"role": "user", "content": "Source document"}]}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = Context()
        self.bridge = await ModelBridge(self.context, "configured-provider").__aenter__()
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False)

    async def asyncTearDown(self):
        await self.client.close()
        await self.bridge.__aexit__(None, None, None)

    async def post(self, payload=PAYLOAD, authorization=None, **kwargs):
        header = "Bearer " + self.bridge.token if authorization is None else authorization
        async with self.client.post(
            self.bridge.url + "/chat/completions",
            headers={"Authorization": header},
            json=payload,
            **kwargs,
        ) as response:
            return response.status, await response.json()

    def assert_error(self, result, status, message=None):
        actual, body = result
        self.assertEqual(actual, status)
        self.assertEqual(set(body), {"error"})
        self.assertEqual(body["error"]["type"], "bridge_error")
        if message is not None:
            self.assertEqual(body["error"]["message"], message)
        self.assertNotIn(self.bridge.token, json.dumps(body))
        self.assertNotIn("Source document", json.dumps(body))

    async def test_server_binds_only_ephemeral_ipv4_loopback(self):
        parts = urlsplit(self.bridge.url)
        self.assertEqual((parts.scheme, parts.hostname, parts.path), ("http", "127.0.0.1", "/v1"))
        self.assertGreater(parts.port, 0)
        self.assertTrue(self.bridge.runner.addresses)
        self.assertTrue(all(address[0] == "127.0.0.1" for address in self.bridge.runner.addresses))

    async def test_tokens_are_unique_per_job(self):
        other = ModelBridge(self.context, "other-provider")
        self.assertNotEqual(other.token, self.bridge.token)
        self.assertGreaterEqual(len(other.token), 32)

    async def test_auth_missing_rejected_before_json_parse(self):
        async with self.client.post(
            self.bridge.url + "/chat/completions", data="not JSON"
        ) as response:
            self.assert_error((response.status, await response.json()), 401, "Unauthorized")
        self.assertEqual(self.context.requests, [])
        self.assertEqual(self.bridge.calls, 0)

    async def test_bad_auth_rejected(self):
        for header in (
            "",
            "Bearer wrong",
            self.bridge.token,
            "bearer " + self.bridge.token,
            "Bearer " + self.bridge.token + "x",
        ):
            with self.subTest(header=header[:12]):
                self.assert_error(await self.post(authorization=header), 401)
        self.assertEqual(self.context.requests, [])
        self.assertEqual(self.bridge.calls, 0)

    async def test_non_ascii_authorization_returns_401_not_server_error(self):
        """Non-ASCII headers must never reach compare_digest(str, str)."""
        with patch("aiohttp.web_protocol.RequestHandler.log_exception"):
            async with self.client.post(
                self.bridge.url + "/chat/completions",
                json=PAYLOAD,
                headers={"Authorization": "Bearer 秘密"},
            ) as response:
                self.assertEqual(response.status, 401)
                self.assertEqual((await response.json())["error"]["type"], "bridge_error")
        self.assertEqual(self.context.requests, [])

    async def test_success_uses_only_configured_provider_and_openai_shape(self):
        status, body = await self.post(
            {
                **PAYLOAD,
                "model": "attacker-model",
                "api_key": "do-not-forward",
                "temperature": 2,
                "max_tokens": 1,
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["id"].startswith("chatcmpl-"))
        self.assertEqual(body["object"], "chat.completion")
        self.assertIsInstance(body["created"], int)
        self.assertEqual(body["model"], "astrbot")
        self.assertNotIn("usage", body)
        self.assertEqual(
            body["choices"],
            [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Translated text"},
                    "finish_reason": "stop",
                }
            ],
        )
        self.assertEqual(
            self.context.requests,
            [
                {
                    "chat_provider_id": "configured-provider",
                    "system_prompt": "",
                    "prompt": "Source document",
                }
            ],
        )
        self.assertEqual((self.bridge.calls, self.bridge.failures), (1, 0))
        self.assertFalse(self.bridge.active)

    async def test_system_developer_and_user_messages_join_separately(self):
        messages = [
            {"role": role, "content": text}
            for role, text in (
                ("system", "Translate only"),
                ("user", "First 中文"),
                ("developer", "Keep layout"),
                ("user", "Second"),
            )
        ]
        self.assertEqual((await self.post({"messages": messages}))[0], 200)
        self.assertEqual(
            self.context.requests[0],
            {
                "chat_provider_id": "configured-provider",
                "system_prompt": "Translate only\n\nKeep layout",
                "prompt": "First 中文\n\nSecond",
            },
        )

    async def test_invalid_json_and_oversized_body_are_rejected(self):
        for body in ("{broken", "{" + "x" * (256 * 1024)):
            with self.subTest(size=len(body)):
                async with self.client.post(
                    self.bridge.url + "/chat/completions",
                    data=body,
                    headers={"Authorization": "Bearer " + self.bridge.token},
                ) as response:
                    self.assert_error((response.status, await response.json()), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_unsupported_top_level_payloads(self):
        for payload in (None, [], "text", 1, False):
            with self.subTest(payload=payload):
                # aiohttp json=None sends no body: still exercises malformed/non-object rejection.
                self.assert_error(await self.post(payload), 400)
        self.assertEqual(self.context.requests, [])

    async def test_stream_tools_and_functions_rejected(self):
        for key, value in (
            ("stream", True),
            ("stream", "true"),
            ("tools", [{"type": "function"}]),
            ("functions", [{"name": "execute"}]),
        ):
            with self.subTest(key=key, value=value):
                self.assert_error(await self.post({**PAYLOAD, key: value}), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_empty_optional_features_remain_non_streaming(self):
        self.assertEqual(
            (await self.post({**PAYLOAD, "stream": False, "tools": [], "functions": []}))[0], 200
        )

    async def test_messages_container_and_count_rejected(self):
        for messages in (None, {}, "text", [], [PAYLOAD["messages"][0]] * 33):
            with self.subTest(messages=messages):
                self.assert_error(await self.post({"messages": messages}), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_message_content_must_be_string(self):
        for message in (
            None,
            "text",
            [],
            {},
            {"role": "user", "content": None},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": "https://unused.invalid"}],
            },
            {"role": "user", "content": 1},
        ):
            with self.subTest(message=message):
                self.assert_error(await self.post({"messages": [message]}), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_unsupported_or_missing_roles_rejected(self):
        for role in ("assistant", "tool", "function", "unknown", None, 3):
            with self.subTest(role=role):
                self.assert_error(
                    await self.post({"messages": [{"role": role, "content": "text"}]}), 400
                )
        self.assert_error(await self.post({"messages": [{"content": "text"}]}), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_unhashable_message_role_returns_400_not_server_error(self):
        """JSON role arrays/objects must be rejected before set membership."""
        for role in ([], {}):
            with (
                self.subTest(role=role),
                patch("aiohttp.web_protocol.RequestHandler.log_exception"),
            ):
                async with self.client.post(
                    self.bridge.url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.bridge.token},
                    json={"messages": [{"role": role, "content": "text"}]},
                ) as response:
                    self.assertEqual(response.status, 400)

    async def test_system_only_and_combined_oversize_rejected(self):
        for messages in (
            [{"role": "system", "content": "instructions"}],
            [{"role": "system", "content": "s" * 50001}, {"role": "user", "content": "u" * 50000}],
        ):
            with self.subTest(size=sum(len(m["content"]) for m in messages)):
                self.assert_error(await self.post({"messages": messages}), 400)
        self.assertEqual(self.bridge.calls, 0)

    async def test_message_count_and_text_size_boundaries_accepted(self):
        self.assertEqual(
            (await self.post({"messages": [{"role": "user", "content": "u" * 100000}]}))[0], 200
        )
        self.assertEqual(
            (await self.post({"messages": [{"role": "user", "content": "x"}] * 32}))[0], 200
        )

    async def test_budget_counts_attempts_and_never_calls_provider_after_exhaustion(self):
        self.bridge.max_calls = 1
        self.assertEqual((await self.post())[0], 200)
        self.assert_error(await self.post(), 402, "Model call budget exhausted")
        self.assert_error(await self.post(), 402)
        self.assertEqual(len(self.context.requests), 1)
        self.assertEqual((self.bridge.calls, self.bridge.failures), (1, 2))

    async def test_zero_budget_rejects_first_call(self):
        self.bridge.max_calls = 0
        self.assert_error(await self.post(), 402)
        self.assertEqual(self.context.requests, [])

    async def test_provider_exception_is_generic_and_prevents_retries(self):
        async def fail(**kwargs):
            raise RuntimeError("Source document SECRET_PROVIDER_KEY")

        self.context.behavior = fail
        result = await self.post()
        self.assert_error(result, 502, "Configured AstrBot model call failed")
        self.assertNotIn("SECRET_PROVIDER_KEY", json.dumps(result))
        self.assert_error(
            await self.post(), 502, "A previous model call failed; this job will not retry it"
        )
        self.assertEqual(len(self.context.requests), 1)
        self.assertEqual((self.bridge.calls, self.bridge.failures), (2, 1))
        self.assertFalse(self.bridge.active)

    async def test_invalid_outputs_fail_closed(self):
        for response in (
            None,
            SimpleNamespace(),
            SimpleNamespace(completion_text=None),
            SimpleNamespace(completion_text=17),
            SimpleNamespace(completion_text=""),
            SimpleNamespace(completion_text=" \n\t"),
            SimpleNamespace(completion_text="x" * 100001),
        ):
            with self.subTest(response_type=type(response).__name__):

                async def generate(**kwargs):
                    return response

                self.context.behavior = generate
                self.bridge.failures = 0
                self.assert_error(await self.post(), 502)
                self.assertEqual(self.bridge.failures, 1)
                self.assertFalse(self.bridge.active)

    async def test_output_boundary_and_whitespace_preserved(self):
        async def generate(**kwargs):
            return SimpleNamespace(completion_text=" " + "x" * 99998 + "\n")

        self.context.behavior = generate
        status, body = await self.post()
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], " " + "x" * 99998 + "\n")

    async def test_timeout_cancels_provider_and_disallows_retry(self):
        cancelled = asyncio.Event()

        async def slow(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.context.behavior = slow
        self.bridge.timeout = 0.02
        self.assert_error(await self.post(), 502)
        self.assertTrue(cancelled.is_set())
        self.assert_error(await self.post(), 502)
        self.assertEqual(len(self.context.requests), 1)
        self.assertFalse(self.bridge.active)

    async def test_provider_calls_are_serialized(self):
        entered, release = asyncio.Event(), asyncio.Event()
        running = peak = 0

        async def slow(**kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            entered.set()
            await release.wait()
            running -= 1
            return SimpleNamespace(completion_text="ok")

        self.context.behavior = slow
        first = asyncio.create_task(self.bridge._complete(Request(self.bridge, PAYLOAD)))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(self.bridge._complete(Request(self.bridge, PAYLOAD)))
            await asyncio.sleep(0)
            self.assertEqual(self.bridge.calls, 2)
            self.assertEqual(len(self.context.requests), 1)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 1)
            self.assertEqual([r.status for r in responses], [200, 200])
            self.assertEqual(peak, 1)
            self.assertFalse(self.bridge.active)
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (first, second) if t is not None), return_exceptions=True
            )

    async def test_shutdown_cancels_inflight_and_queued_requests(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.context.behavior = slow
        first = asyncio.create_task(self.bridge._complete(Request(self.bridge, PAYLOAD)))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(self.bridge._complete(Request(self.bridge, PAYLOAD)))
            await asyncio.sleep(0)
            self.assertEqual(len(self.bridge.active), 2)
            await asyncio.wait_for(self.bridge.__aexit__(None, None, None), 2)
            self.assertTrue(first.cancelled())
            self.assertTrue(second.cancelled())
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.bridge.active)
            self.assertFalse(self.bridge.runner.addresses)
            self.assertEqual(len(self.context.requests), 1)
        finally:
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (first, second) if t is not None), return_exceptions=True
            )

    async def test_only_completion_post_route_is_exposed(self):
        for method, path, expected in (("GET", "/chat/completions", 405), ("POST", "/models", 404)):
            async with self.client.request(method, self.bridge.url + path) as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.context.requests, [])


class BridgeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_cleans_runner(self):
        fake_runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        fake_site = SimpleNamespace(start=AsyncMock(side_effect=OSError("bind failed")))
        with (
            patch.object(bridge_module.web, "AppRunner", return_value=fake_runner),
            patch.object(bridge_module.web, "TCPSite", return_value=fake_site) as site,
        ):
            bridge = ModelBridge(Context(), "provider")
            with self.assertRaisesRegex(OSError, "bind failed"):
                await bridge.__aenter__()
        fake_runner.cleanup.assert_awaited_once()
        site.assert_called_once_with(fake_runner, "127.0.0.1", 0)


if __name__ == "__main__":
    unittest.main()
