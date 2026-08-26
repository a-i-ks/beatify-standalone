"""`Store` — upstream's only persistence primitive."""

from __future__ import annotations

import json

from homeassistant.helpers.storage import Store


async def test_roundtrip(hass):
    store = Store(hass, 1, "beatify.stats")
    assert await store.async_load() is None

    await store.async_save({"games": 3})
    assert await store.async_load() == {"games": 3}


async def test_written_file_uses_home_assistant_envelope(hass):
    """So a `.storage` directory copied from a real HA install still loads."""
    store = Store(hass, 7, "beatify.settings")
    await store.async_save({"a": 1})

    raw = json.loads(store.path.read_text())
    assert raw == {"version": 7, "key": "beatify.settings", "data": {"a": 1}}


async def test_reads_a_bare_document_too(hass):
    store = Store(hass, 1, "legacy")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"not": "wrapped"}))
    assert await store.async_load() == {"not": "wrapped"}


async def test_corrupt_store_returns_none_instead_of_raising(hass):
    store = Store(hass, 1, "broken")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not json")
    assert await store.async_load() is None
