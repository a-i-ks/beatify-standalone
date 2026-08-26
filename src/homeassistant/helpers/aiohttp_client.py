"""One shared aiohttp session, created lazily and reused."""

from __future__ import annotations

from typing import Any

import aiohttp

_KEY = "_beatify_shim_clientsession"


def async_get_clientsession(hass: Any, verify_ssl: bool = True) -> aiohttp.ClientSession:
    session = hass.data.get(_KEY)
    if session is None or session.closed:
        connector = aiohttp.TCPConnector(ssl=None if verify_ssl else False)
        session = aiohttp.ClientSession(connector=connector)
        hass.data[_KEY] = session
    return session


async def async_close_clientsession(hass: Any) -> None:
    session = hass.data.pop(_KEY, None)
    if session is not None and not session.closed:
        await session.close()
