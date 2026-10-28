"""Offline attachment safety tests: fake DNS/HTTP plus an ephemeral loopback server.

Run with: .engine/Scripts/python.exe -m unittest discover -s tests -p test_download.py -v
No public DNS, external network, plugin initialization or AstrBot dependency is used.
Regression cases preserve safe error and Windows filename boundary contracts.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
# Load relative imports without executing the plugin's AstrBot-dependent __init__.
PACKAGE = "isolated_download_contract"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE, package)
_spec = importlib.util.spec_from_file_location(PACKAGE + ".download", ROOT / "download.py")
download_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(download_module)
TranslationError = download_module.TranslationError


class Content:
    def __init__(self, chunks=(), exception=None):
        self.chunks, self.exception = chunks, exception
        self.chunk_sizes = []

    async def iter_chunked(self, size):
        self.chunk_sizes.append(size)
        for chunk in self.chunks:
            yield chunk
        if self.exception is not None:
            raise self.exception


class Response:
    def __init__(self, chunks=(), length=None, status=200, exception=None):
        self.status, self.content_length = status, length
        self.content = Content(chunks, exception)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Session:
    def __init__(self, response=None, exception=None):
        self.response, self.exception = response, exception
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.exception is not None:
            raise self.exception
        return self.response


class PublicAddressTests(unittest.TestCase):
    def test_public_ipv4_and_ipv6_allowed(self):
        for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            with self.subTest(address=address):
                self.assertTrue(download_module.public(address))

    def test_nonpublic_ipv4_and_ipv6_blocked(self):
        for address in (
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "172.16.1.2",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "192.0.2.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "fe80::1%12",
            "fc00::1",
            "2001:db8::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
        ):
            with self.subTest(address=address):
                self.assertFalse(download_module.public(address))

    def test_invalid_ip_not_silently_accepted(self):
        for address in ("", "example.invalid", "127.1", "0x7f000001", "not-an-ip"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                download_module.public(address)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def make_resolver(self, answers=None, exception=None):
        inner = types.SimpleNamespace(
            resolve=AsyncMock(return_value=answers, side_effect=exception), close=AsyncMock()
        )
        with patch.object(download_module.aiohttp.resolver, "ThreadedResolver", return_value=inner):
            resolver = download_module.Resolver()
        return resolver, inner

    async def test_all_public_answers_preserved_and_arguments_forwarded(self):
        answers = [{"host": "8.8.8.8", "port": 443}, {"host": "2606:4700:4700::1111", "port": 443}]
        resolver, inner = self.make_resolver(answers)
        self.assertIs(await resolver.resolve("attachment.invalid", 443, socket.AF_UNSPEC), answers)
        inner.resolve.assert_awaited_once_with("attachment.invalid", 443, socket.AF_UNSPEC)
        await resolver.close()
        inner.close.assert_awaited_once()

    async def test_private_mixed_and_empty_answers_rejected(self):
        for answers in (
            [],
            [{"host": "127.0.0.1"}],
            [{"host": "8.8.8.8"}, {"host": "10.1.2.3"}],
            [{"host": "::ffff:192.168.1.2"}],
            [{"host": "ff02::1"}],
        ):
            with self.subTest(answers=answers):
                resolver, inner = self.make_resolver(answers)
                with self.assertRaisesRegex(OSError, "Private network attachment address blocked"):
                    await resolver.resolve("rebinding.invalid")
                inner.resolve.assert_awaited_once_with("rebinding.invalid", 0, socket.AF_INET)
                await resolver.close()

    async def test_dns_failure_propagates_without_fallback(self):
        resolver, inner = self.make_resolver(exception=OSError("DNS failed"))
        with self.assertRaisesRegex(OSError, "DNS failed"):
            await resolver.resolve("attachment.invalid")
        self.assertEqual(inner.resolve.await_count, 1)
        await resolver.close()

    async def test_every_resolution_revalidates_rebinding_answers(self):
        resolver, inner = self.make_resolver()
        inner.resolve.side_effect = [[{"host": "8.8.8.8"}], [{"host": "127.0.0.1"}]]
        self.assertEqual((await resolver.resolve("rebinding.invalid"))[0]["host"], "8.8.8.8")
        with self.assertRaises(OSError):
            await resolver.resolve("rebinding.invalid")
        await resolver.close()


class SafeNameTests(unittest.TestCase):
    def test_directory_components_removed_on_both_platforms(self):
        for name, expected in (
            ("../../secret.pdf", "secret.pdf"),
            (r"C:\private\report.pdf", "report.pdf"),
            ("../a\\b/final.pdf", "final.pdf"),
        ):
            with self.subTest(name=name):
                self.assertEqual(download_module.safe_name(name), expected)

    def test_illegal_characters_and_control_bytes_replaced(self):
        self.assertEqual(download_module.safe_name('a<>:"|?*\x00\x1f\x7fb.pdf'), "a__________b.pdf")

    def test_empty_dot_and_directory_names_use_fallback(self):
        for name in ("", ".", "..", " ... ", "folder/", "folder\\"):
            with self.subTest(name=name):
                self.assertEqual(download_module.safe_name(name), "document.pdf")

    def test_unicode_preserved_whitespace_trimmed_and_length_bounded(self):
        self.assertEqual(download_module.safe_name(" 报告.pdf. "), "报告.pdf")
        self.assertEqual(download_module.safe_name("x" * 121), "x" * 120)
        self.assertEqual(download_module.safe_name(123), "123")

    def test_windows_device_names_are_not_returned_unchanged(self):
        """Windows device basenames need disarming even with an extension."""
        for name in ("CON", "nul.pdf", "AUX.txt", "PRN", "COM1.pdf", "LPT9.txt"):
            with self.subTest(name=name):
                self.assertNotEqual(
                    download_module.safe_name(name).split(".")[0].upper(),
                    name.split(".")[0].upper(),
                )

    def test_reserved_name_prefix_remains_within_maximum_length(self):
        self.assertLessEqual(len(download_module.safe_name("CON." + "x" * 116)), 120)

    def test_truncation_does_not_reintroduce_trailing_dot_or_space(self):
        """Truncation must not introduce a Windows-unsafe trailing dot/space."""
        for name in ("a" * 119 + ".pdf", "a" * 119 + " suffix.pdf"):
            with self.subTest(name=name):
                self.assertFalse(download_module.safe_name(name).endswith((".", " ")))


class DownloadValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "attachment.pdf"

    async def test_invalid_scheme_host_and_credentials_blocked_before_connection(self):
        for url in (
            "",
            "/relative.pdf",
            "file:///etc/passwd",
            "ftp://attachment.invalid/a.pdf",
            "http:///missing-host",
            "https://user:password@attachment.invalid/a.pdf",
            "https://user@attachment.invalid/a.pdf",
            "http://attachment.invalid:bad/a.pdf",
            "http://attachment.invalid:65536/a.pdf",
            "http://attachment.invalid/a.pdf#fragment",
        ):
            with (
                self.subTest(url=url),
                patch.object(download_module.aiohttp, "TCPConnector") as connector,
            ):
                with self.assertRaises(TranslationError):
                    await download_module.download(url, self.destination, 10)
                connector.assert_not_called()
                self.assertFalse(self.destination.exists())

    async def test_private_literal_localhost_and_metadata_urls_blocked_without_network(self):
        for host in (
            "localhost",
            "LOCALHOST",
            "sub.localhost",
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "[::1]",
            "[fc00::1]",
            "[fe80::1%25eth0]",
            "[::ffff:127.0.0.1]",
        ):
            with (
                self.subTest(host=host),
                patch.object(download_module.aiohttp, "TCPConnector") as connector,
            ):
                with self.assertRaises(TranslationError):
                    await download_module.download(
                        "http://" + host + "/attachment", self.destination, 10
                    )
                connector.assert_not_called()

    async def test_private_override_does_not_allow_bad_scheme_or_credentials(self):
        for url in ("file:///private", "http://user:pass@127.0.0.1/a"):
            with (
                self.subTest(url=url),
                patch.object(download_module.aiohttp, "TCPConnector") as connector,
            ):
                with self.assertRaises(TranslationError):
                    await download_module.download(url, self.destination, 10, allow_private=True)
                connector.assert_not_called()

    async def test_malformed_ipv6_url_uses_safe_translation_error(self):
        """Malformed bracket syntax must not escape as raw ValueError."""
        with patch.object(download_module.aiohttp, "TCPConnector") as connector:
            with self.assertRaises(TranslationError):
                await download_module.download("http://[broken/attachment", self.destination, 10)
            connector.assert_not_called()


class FakeDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "attachment.pdf"

    async def invoke(
        self, response=None, exception=None, maximum=10, allow_private=False, destination=None
    ):
        session = Session(response, exception)
        connector = object()
        with (
            patch.object(
                download_module, "Resolver", return_value="validating-resolver"
            ) as resolver_factory,
            patch.object(
                download_module.aiohttp, "TCPConnector", return_value=connector
            ) as connector_factory,
            patch.object(
                download_module.aiohttp, "ClientSession", return_value=session
            ) as session_factory,
        ):
            try:
                await download_module.download(
                    "https://attachment.invalid/file.pdf",
                    destination or self.destination,
                    maximum,
                    allow_private=allow_private,
                )
            finally:
                self.session = session
                self.connector_factory = connector_factory
                self.session_factory = session_factory
                self.resolver_factory = resolver_factory

    async def test_public_download_uses_pinned_resolver_limits_no_proxy_and_no_redirects(self):
        response = Response([b"ab", b"c"], length=3)
        await self.invoke(response)
        self.assertEqual(self.destination.read_bytes(), b"abc")
        self.connector_factory.assert_called_once_with(resolver="validating-resolver", limit=2)
        self.resolver_factory.assert_called_once_with()
        self.assertFalse(self.session_factory.call_args.kwargs["trust_env"])
        self.assertEqual(self.session_factory.call_args.kwargs["timeout"].total, 120)
        self.assertEqual(
            self.session.requests,
            [("https://attachment.invalid/file.pdf", {"allow_redirects": False})],
        )
        self.assertEqual(response.content.chunk_sizes, [256 * 1024])
        self.assertTrue(response.closed)
        self.assertTrue(self.session.closed)

    async def test_private_override_disables_only_validating_resolver(self):
        await self.invoke(Response([b"ok"]), allow_private=True)
        self.connector_factory.assert_called_once_with(resolver=None, limit=2)
        self.resolver_factory.assert_not_called()
        self.assertFalse(self.session_factory.call_args.kwargs["trust_env"])

    async def test_non_200_statuses_do_not_write_and_remove_existing_destination(self):
        for status in (204, 301, 302, 307, 308, 400, 403, 404, 500):
            with self.subTest(status=status):
                self.destination.write_bytes(b"stale")
                response = Response([b"must not be written"], status=status)
                with self.assertRaisesRegex(TranslationError, "不自动跟随跳转"):
                    await self.invoke(response)
                self.assertFalse(self.destination.exists())
                self.assertEqual(response.content.chunk_sizes, [])
                self.assertEqual(len(self.session.requests), 1)
                self.assertTrue(response.closed)
                self.assertTrue(self.session.closed)

    async def test_content_length_over_limit_rejected_before_opening_file(self):
        response = Response([b"x" * 11], length=11)
        with patch.object(Path, "open", side_effect=AssertionError("must reject before open")):
            with self.assertRaisesRegex(TranslationError, "文件大小限制"):
                await self.invoke(response)
        self.assertFalse(self.destination.exists())
        self.assertEqual(response.content.chunk_sizes, [])

    async def test_content_length_equal_to_maximum_allowed(self):
        await self.invoke(Response([b"12345", b"67890"], length=10))
        self.assertEqual(self.destination.read_bytes(), b"1234567890")

    async def test_streaming_limit_applies_without_or_with_underreported_length(self):
        for length in (None, 2):
            with self.subTest(length=length):
                response = Response([b"12345", b"678901"], length=length)
                with self.assertRaisesRegex(TranslationError, "文件大小限制"):
                    await self.invoke(response)
                self.assertFalse(self.destination.exists())
                self.assertTrue(response.closed)
                self.assertTrue(self.session.closed)

    async def test_oversized_chunk_is_not_written(self):
        response = Response([b"12345", b"678901"])
        original = asyncio.to_thread
        writes = []

        async def capture(function, *args):
            writes.append(args[0])
            return await original(function, *args)

        with patch.object(download_module.asyncio, "to_thread", side_effect=capture):
            with self.assertRaises(TranslationError):
                await self.invoke(response)
        self.assertEqual(writes, [b"12345"])
        self.assertFalse(self.destination.exists())

    async def test_network_timeout_and_os_errors_are_generic_and_remove_partial_files(self):
        for exception in (
            aiohttp.ClientPayloadError("SECRET_URL"),
            aiohttp.ClientConnectionError("SECRET_URL"),
            asyncio.TimeoutError("SECRET_URL"),
            OSError("SECRET_URL"),
        ):
            with self.subTest(exception=type(exception).__name__):
                response = Response([b"partial"], exception=exception)
                with self.assertRaises(TranslationError) as caught:
                    await self.invoke(response)
                self.assertNotIn("SECRET_URL", str(caught.exception))
                self.assertIs(caught.exception.__cause__, exception)
                self.assertFalse(self.destination.exists())
                self.assertTrue(response.closed)
                self.assertTrue(self.session.closed)

    async def test_connection_failure_removes_preexisting_destination(self):
        self.destination.write_bytes(b"stale data")
        with self.assertRaises(TranslationError):
            await self.invoke(exception=aiohttp.ClientConnectionError("private URL"))
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.session.closed)

    async def test_disk_write_failure_removes_partial_file(self):
        with patch.object(download_module.asyncio, "to_thread", side_effect=OSError("disk secret")):
            with self.assertRaises(TranslationError) as caught:
                await self.invoke(Response([b"partial"]))
        self.assertNotIn("disk secret", str(caught.exception))
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.session.closed)

    async def test_missing_destination_directory_is_safe_error(self):
        destination = self.destination.parent / "missing" / "attachment.pdf"
        with self.assertRaises(TranslationError):
            await self.invoke(Response([b"a"]), destination=destination)
        self.assertFalse(destination.exists())
        self.assertTrue(self.session.closed)

    async def test_cancellation_propagates_and_removes_partial_file(self):
        response = Response([b"partial"], exception=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(response)
        self.assertFalse(self.destination.exists())
        self.assertTrue(response.closed)
        self.assertTrue(self.session.closed)

    async def test_unexpected_exception_not_swallowed_but_partial_file_removed(self):
        exception = RuntimeError("unexpected")
        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(Response([b"partial"], exception=exception))
        self.assertIs(caught.exception, exception)
        self.assertFalse(self.destination.exists())

    async def test_empty_response_is_zero_length_bounded_download(self):
        await self.invoke(Response([], length=0), maximum=0)
        self.assertEqual(self.destination.read_bytes(), b"")


class LoopbackDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "attachment.pdf"
        self.hits = []

        async def handler(request):
            self.hits.append(request.path)
            if request.path == "/redirect":
                return web.Response(status=302, headers={"Location": self.base + "/file"})
            if request.path == "/failure":
                return web.Response(status=503)
            if request.path == "/chunked":
                response = web.StreamResponse()
                response.enable_chunked_encoding()
                await response.prepare(request)
                await response.write(b"12345")
                await response.write(b"678901")
                await response.write_eof()
                return response
            return web.Response(body=b"local-pdf-data")

        app = web.Application()
        app.router.add_get("/{name}", handler)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_real_download_explicit_private_override(self):
        await download_module.download(
            self.base + "/file", self.destination, 14, allow_private=True
        )
        self.assertEqual(self.destination.read_bytes(), b"local-pdf-data")
        self.assertEqual(self.hits, ["/file"])

    async def test_real_loopback_server_blocked_by_default(self):
        with self.assertRaises(TranslationError):
            await download_module.download(self.base + "/file", self.destination, 100)
        self.assertEqual(self.hits, [])
        self.assertFalse(self.destination.exists())

    async def test_real_redirect_not_followed_even_with_private_override(self):
        with self.assertRaises(TranslationError):
            await download_module.download(
                self.base + "/redirect", self.destination, 100, allow_private=True
            )
        self.assertEqual(self.hits, ["/redirect"])
        self.assertFalse(self.destination.exists())

    async def test_real_content_length_overflow_removed(self):
        with self.assertRaises(TranslationError):
            await download_module.download(
                self.base + "/file", self.destination, 5, allow_private=True
            )
        self.assertFalse(self.destination.exists())

    async def test_real_chunked_overflow_removed(self):
        with self.assertRaises(TranslationError):
            await download_module.download(
                self.base + "/chunked", self.destination, 10, allow_private=True
            )
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.hits, ["/chunked"])

    async def test_real_http_failure_removed(self):
        with self.assertRaises(TranslationError):
            await download_module.download(
                self.base + "/failure", self.destination, 100, allow_private=True
            )
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
