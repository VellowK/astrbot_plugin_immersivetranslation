"""Per-job loopback OpenAI adapter; real model keys never leave AstrBot."""

import asyncio
import hmac
import secrets
import time
import uuid

from aiohttp import web


class ModelBridge:
    def __init__(self, context, provider_id: str, max_calls=200, timeout=120):
        self.context, self.provider_id = context, provider_id
        self.max_calls, self.timeout = max_calls, timeout
        self.token = secrets.token_urlsafe(32)
        self.calls = 0
        self.failures = 0
        self.runner = None
        self.url = ""
        self.slot = asyncio.Semaphore(1)
        self.active: set[asyncio.Task] = set()

    async def __aenter__(self):
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post("/v1/chat/completions", self._complete)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            port = self.runner.addresses[0][1]
            self.url = f"http://127.0.0.1:{port}/v1"
            return self
        except BaseException:
            await self.runner.cleanup()
            raise

    async def __aexit__(self, *args):
        for task in list(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*list(self.active), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()

    @staticmethod
    def _error(message, status):
        return web.json_response(
            {"error": {"message": message, "type": "bridge_error"}}, status=status
        )

    async def _complete(self, request):
        authorization = request.headers.get("Authorization", "")
        if not authorization.isascii() or not hmac.compare_digest(
            authorization, "Bearer " + self.token
        ):
            return self._error("Unauthorized", 401)
        try:
            data = await request.json()
        except (ValueError, TypeError, web.HTTPException):
            return self._error("Invalid JSON or oversized request", 400)
        if (
            not isinstance(data, dict)
            or data.get("stream")
            or data.get("tools")
            or data.get("functions")
        ):
            return self._error(
                "Only non-streaming text completions without tools are supported", 400
            )
        messages = data.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
            return self._error("Invalid messages", 400)
        system, user = [], []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                return self._error("Only string message content is supported", 400)
            role = message.get("role")
            if not isinstance(role, str):
                return self._error("Invalid message role", 400)
            if role in {"system", "developer"}:
                system.append(message["content"])
            elif role == "user":
                user.append(message["content"])
            else:
                return self._error("Only system and user translation messages are supported", 400)
        if not user or sum(map(len, system + user)) > 100000:
            return self._error("Missing or oversized source text", 400)
        # Count before awaiting, including failed attempts: limits bound actual
        # provider calls even if upstream retries its OpenAI request.
        if self.calls >= self.max_calls:
            self.failures += 1
            return self._error("Model call budget exhausted", 402)
        self.calls += 1
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with self.slot:
                if self.failures:
                    return self._error(
                        "A previous model call failed; this job will not retry it", 502
                    )
                try:
                    response = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=self.provider_id,
                            system_prompt="\n\n".join(system),
                            prompt="\n\n".join(user),
                        ),
                        self.timeout,
                    )
                    text = getattr(response, "completion_text", None)
                    if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                        raise ValueError("Empty or oversized model output")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failures += 1
                    return self._error("Configured AstrBot model call failed", 502)
            # Usage is omitted deliberately: the bridge must not invent token counts.
            return web.json_response(
                {
                    "id": "chatcmpl-" + uuid.uuid4().hex,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "astrbot",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        finally:
            self.active.discard(task)
