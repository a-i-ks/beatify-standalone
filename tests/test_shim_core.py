"""The shim's own behaviour — the parts upstream silently relies on."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.core import State
from homeassistant.exceptions import ServiceNotFound


def test_state_exposes_the_members_upstream_reads(hass):
    hass.states.async_set(
        "media_player.x", "playing", {"friendly_name": "Speaker", "media_title": "Song"}
    )
    state = hass.states.get("media_player.x")

    assert state.entity_id == "media_player.x"
    assert state.state == "playing"
    assert state.domain == "media_player"
    assert state.name == "Speaker"
    assert state.attributes["media_title"] == "Song"


def test_name_falls_back_to_object_id(hass):
    hass.states.async_set("media_player.living_room", "idle")
    assert hass.states.get("media_player.living_room").name == "living room"


def test_async_all_filters_by_domain(hass):
    hass.states.async_set("media_player.a", "idle")
    hass.states.async_set("light.b", "on")
    assert [s.entity_id for s in hass.states.async_all("media_player")] == ["media_player.a"]
    assert len(hass.states.async_all()) == 2


def test_last_changed_survives_an_attribute_only_update(hass):
    """Position maths would be wrong if every poll looked like a state change."""
    hass.states.async_set("media_player.x", "playing", {"media_position": 1})
    first = hass.states.get("media_player.x").last_changed

    hass.states.async_set("media_player.x", "playing", {"media_position": 2})
    assert hass.states.get("media_player.x").last_changed == first

    hass.states.async_set("media_player.x", "paused", {"media_position": 2})
    assert hass.states.get("media_player.x").last_changed != first


async def test_unknown_service_raises_service_not_found(hass):
    """Upstream catches this to fall through between providers."""
    with pytest.raises(ServiceNotFound):
        await hass.services.async_call("music_assistant", "play_media", {})


async def test_service_call_passes_merged_data(hass):
    seen = {}

    async def handler(data):
        seen.update(data)

    hass.services.async_register("media_player", "play_media", handler)
    await hass.services.async_call(
        "media_player", "play_media", {"media_content_id": "spotify:track:1"},
        target={"entity_id": "media_player.x"},
    )
    assert seen == {"media_content_id": "spotify:track:1", "entity_id": "media_player.x"}


async def test_state_listener_fires_and_unsubscribes(hass):
    events = []
    unsub = hass.states.async_listen("media_player.x", lambda event: events.append(event))

    hass.states.async_set("media_player.x", "playing")
    assert len(events) == 1
    assert events[0].data["new_state"].state == "playing"

    unsub()
    hass.states.async_set("media_player.x", "paused")
    assert len(events) == 1


async def test_a_raising_listener_does_not_break_publication(hass):
    def boom(event):
        raise RuntimeError("listener is broken")

    hass.states.async_listen(None, boom)
    hass.states.async_set("media_player.x", "playing")
    assert hass.states.get("media_player.x").state == "playing"
