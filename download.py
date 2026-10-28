"""Bounded, public-network-only downloads for platform-provided attachments."""

import asyncio
import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .errors import TranslationError


def public(address):
    value = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped:
        value = value.ipv4_mapped
    return value.is_global and not value.is_multicast


class Resolver(AbstractResolver):
    def __init__(self):
        self.inner = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.inner.resolve(host, port, family)
        if not results or any(not public(item["host"]) for item in results):
            raise OSError("Private network attachment address blocked")
        return results

    async def close(self):
        await self.inner.close()


def safe_name(name):
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name).strip(" .")[:120].rstrip(" .")
    if name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        name = "_" + name
    return name[:120].rstrip(" .") or "document.pdf"


async def download(url: str, destination: Path, maximum: int, allow_private=False):
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port
    except (ValueError, TypeError) as exc:
        raise TranslationError("附件地址无效。") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise TranslationError("附件地址无效。")
    if not allow_private:
        if parts.hostname == "localhost" or parts.hostname.endswith(".localhost"):
            raise TranslationError("默认禁止下载本机/内网地址。")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            if not public(str(address)):
                raise TranslationError("默认禁止下载本机/内网地址。")
    connector = aiohttp.TCPConnector(resolver=None if allow_private else Resolver(), limit=2)
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=120), trust_env=False
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise TranslationError("附件下载失败（不自动跟随跳转），请重新发送原文件。")
                if response.content_length is not None and response.content_length > maximum:
                    raise TranslationError("附件超过插件文件大小限制。")
                size = 0
                with destination.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > maximum:
                            raise TranslationError("附件超过插件文件大小限制。")
                        await asyncio.to_thread(stream.write, chunk)
    except BaseException as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError)):
            raise TranslationError("附件下载失败或超时，请检查平台适配器和网络。") from exc
        raise
