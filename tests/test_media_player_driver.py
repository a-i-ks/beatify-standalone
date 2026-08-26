"""The driver: upstream's service calls in, Spotify calls out."""

from __future__ import annotations

import pytest
from fake_spotify import FakeSpotifyClient

from beatify_standalone.media_player_driver import ENTITY_ID, PLATFORM, MediaPlayerDriver
from homeassistant.helpers import entity_registry as er


@pytest.fixture
async def driver(hass):
    client = FakeSpotifyClient()
    instance = MediaPlayerDriver(hass, client, "Beatify")
    await instance.async_setup()
    yield instance, client, hass
    await instance.async_stop()


async def test_entity_is_registered_as_a_platform_upstream_can_play_through(driver):
    """The whole port hinges on this: see MediaPlayerDriver's docstring."""
    _, _, hass = driver
    entry = er.async_get(hass).async_get(ENTITY_ID)

    assert entry is not None
    assert entry.platform == PLATFORM == "sonos"
    assert entry.domain == "media_player"


async def test_upstreams_player_scan_finds_it(driver):
    """`async_get_media_players` is what decides whether Beatify has a speaker."""
    from custom_components.beatify.services.media_player import async_get_media_players

    _, _, hass = driver
    players = await async_get_media_players(hass)

    assert [p["entity_id"] for p in players] == [ENTITY_ID]
    assert players[0]["supports_spotify"] is True


async def test_play_media_starts_the_requested_track(driver):
    _, client, hass = driver
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": ENTITY_ID,
            "media_content_id": "spotify:track:6COzABVCHQzyvc3rTMtrXn",
            "media_content_type": "music",
        },
    )

    play = next(kwargs for name, kwargs in client.calls if name == "play")
    # Started as an album context with an offset, not as a bare uri list — see
    # MediaPlayerDriver._resolve_context for why that distinction matters.
    assert play["context_uri"] == "spotify:album:fake"
    assert play["offset"] == {"uri": "spotify:track:6COzABVCHQzyvc3rTMtrXn"}
    assert play["device_id"] == FakeSpotifyClient.DEVICE_ID


async def test_play_media_publishes_state_before_returning(driver):
    """Upstream declares a track failed if `media_title` has not moved."""
    _, _, hass = driver
    await hass.services.async_call(
        "media_player",
        "play_media",
        {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:x"},
    )

    state = hass.states.get(ENTITY_ID)
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Er gehört zu mir"
    assert state.attributes["media_artist"] == "Marianne Rosenberg"
    assert state.attributes["media_content_id"] == "spotify:track:x"


async def test_seek_converts_seconds_to_milliseconds(driver):
    _, client, hass = driver
    await hass.services.async_call(
        "media_player", "media_seek", {"entity_id": ENTITY_ID, "seek_position": 42.5}
    )
    assert ("seek", {"position_ms": 42500, "device_id": FakeSpotifyClient.DEVICE_ID}) in client.calls


async def test_volume_converts_fraction_to_percent(driver):
    _, client, hass = driver
    await hass.services.async_call(
        "media_player", "volume_set", {"entity_id": ENTITY_ID, "volume_level": 0.35}
    )
    assert ("set_volume", {"percent": 35, "device_id": FakeSpotifyClient.DEVICE_ID}) in client.calls


async def test_repeat_modes_map_onto_spotify_vocabulary(driver):
    _, client, hass = driver
    for beatify_mode, spotify_state in (("off", "off"), ("all", "context"), ("one", "track")):
        await hass.services.async_call(
            "media_player", "repeat_set", {"entity_id": ENTITY_ID, "repeat": beatify_mode}
        )
        assert client.calls[-1][1]["state"] == spotify_state


async def test_stop_and_pause_both_pause(driver):
    _, client, hass = driver
    await hass.services.async_call("media_player", "media_stop", {"entity_id": ENTITY_ID})
    await hass.services.async_call("media_player", "media_pause", {"entity_id": ENTITY_ID})
    assert client.call_names().count("pause") == 2


async def test_position_is_reported_in_seconds(driver):
    _, client, hass = driver
    client.current_uri = "spotify:track:x"
    client.progress_ms = 30_000
    await hass.services.async_call("homeassistant", "update_entity", {})

    state = hass.states.get(ENTITY_ID)
    assert state.attributes["media_position"] == 30.0
    assert state.attributes["media_duration"] == 210.0
    assert state.attributes["media_position_updated_at"] is not None


async def test_idle_when_nothing_is_playing(driver):
    _, _, hass = driver
    await hass.services.async_call("homeassistant", "update_entity", {})
    assert hass.states.get(ENTITY_ID).state == "idle"


async def test_an_idle_device_is_activated_before_playing(driver):
    """Spotify answers `play` on an idle device with 403 Restriction violated.

    A party box is idle between rounds, so this is the normal path — found on
    real hardware, where the very first real playback attempt failed with it.
    """
    _, client, hass = driver
    client._devices = [{"id": FakeSpotifyClient.DEVICE_ID, "name": "Beatify", "is_active": False}]

    await hass.services.async_call(
        "media_player", "play_media",
        {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:x"},
    )

    names = client.call_names()
    assert "transfer" in names, "must transfer to the idle device"
    assert names.index("transfer") < names.index("play"), "transfer has to come first"


async def test_an_active_device_is_not_needlessly_transferred(driver):
    _, client, hass = driver

    await hass.services.async_call(
        "media_player", "play_media",
        {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:x"},
    )

    assert "transfer" not in client.call_names()


async def test_a_restriction_violation_is_retried_after_a_transfer(driver):
    """The device can also go idle between the check and the command."""
    from beatify_standalone.spotify import SpotifyError

    _, client, hass = driver
    calls = {"n": 0}
    original = client.play

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            client._record("play", **kwargs)
            raise SpotifyError("PUT /me/player/play -> 403: Player command failed: Restriction violated")
        return await original(*args, **kwargs)

    client.play = flaky
    await hass.services.async_call(
        "media_player", "play_media",
        {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:x"},
    )

    assert calls["n"] == 2, "retried once"
    assert "transfer" in client.call_names()
    assert hass.states.get(ENTITY_ID).attributes["media_content_id"] == "spotify:track:x"


async def test_the_album_lookup_is_cached_across_plays(driver):
    """A playlist replays the same songs; one lookup per track is enough."""
    _, client, hass = driver
    for _ in range(3):
        await hass.services.async_call(
            "media_player", "play_media",
            {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:x"},
        )

    assert client.call_names().count("track") == 1
    assert client.call_names().count("play") == 3


async def test_a_track_without_an_album_still_plays(driver):
    """Falls back to the bare uri list rather than dropping the round."""
    _, client, hass = driver

    async def no_album(track_uri):
        client._record("track", track_uri=track_uri)
        return {"uri": track_uri}

    client.track = no_album
    await hass.services.async_call(
        "media_player", "play_media",
        {"entity_id": ENTITY_ID, "media_content_id": "spotify:track:y"},
    )

    play = next(kwargs for name, kwargs in client.calls if name == "play")
    assert play["uris"] == ["spotify:track:y"]
    assert play["context_uri"] is None
