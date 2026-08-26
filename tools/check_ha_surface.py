#!/usr/bin/env python3
"""Guard against upstream drift in Beatify's Home Assistant surface.

The standalone port never modifies upstream code — it satisfies upstream's
`homeassistant` imports with a shim instead. That only holds as long as we know
*exactly* which parts of HA upstream touches. Upstream ships ~12 commits a day,
so every version bump has to be checked before it is adopted.

This scans a Beatify source tree for three things:

  1. `homeassistant` imports        -> must exist in the shim
  2. `hass.<attr>` attribute access -> must exist on the shim's HomeAssistant
  3. `hass.services.async_call()`   -> must have a registered handler

...and diffs them against the baseline in `tools/ha_surface.json`. Anything new
fails the check: that is the exact list of work a version bump requires.

Usage:
    check_ha_surface.py --report [PATH]      print the surface, no checking
    check_ha_surface.py [PATH]               check against the baseline
    check_ha_surface.py --update [PATH]      accept current surface as baseline
    check_ha_surface.py --tag v4.3.1         check an upstream tag (streamed,
                                             nothing written to disk)
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TREE = REPO_ROOT / "vendor" / "custom_components" / "beatify"
BASELINE = REPO_ROOT / "tools" / "ha_surface.json"

# `hass.a.b` — two levels is enough to tell `hass.config.path` from `hass.config`
# while not drowning in call-site noise. Deliberately no leading \b: the code
# stores the object as `self._hass`, and those call sites count just as much.
HASS_ATTR = re.compile(r"hass\.([a-zA-Z_]+)(?:\.([a-zA-Z_]+))?")
SERVICE_CALL = re.compile(
    r"""async_call\(\s*["']([a-z_]+)["']\s*,\s*["']([a-z_]+)["']""", re.S
)


# --------------------------------------------------------------------------
# Seams
#
# The HA surface above is only half the contract. The port also leans on a few
# facts about upstream's *internals* — above all that claiming platform "sonos"
# reaches a generic `media_player.play_media` call. Those are not API promises,
# so they get asserted explicitly: if upstream refactors playback dispatch, this
# fails loudly instead of the box silently playing nothing at a party.
# --------------------------------------------------------------------------

SEAMS: list[tuple[str, str, str]] = [
    (
        "services/media_player.py",
        r"PLATFORM_CAPABILITIES\s*:\s*dict",
        "the platform capability table the player scan filters on",
    ),
    (
        "services/media_player.py",
        r'"sonos"\s*:\s*\{[^}]*"supported"\s*:\s*True[^}]*"spotify"\s*:\s*True',
        'the "sonos" entry is supported and spotify-capable '
        "(media_player_driver.PLATFORM depends on it)",
    ),
    (
        "services/media_player.py",
        r'if self\._platform == "sonos":\s*\n\s*return await self\._play_via_sonos',
        "_play_song still dispatches platform 'sonos' to _play_via_sonos",
    ),
    (
        "services/media_player.py",
        r'async def _play_via_sonos.*?"media_player",\s*\n\s*"play_media"',
        "_play_via_sonos still issues a generic media_player.play_media call",
    ),
    (
        "server/__init__.py",
        r"async def async_register_static_paths",
        "static path registration entry point",
    ),
    (
        "__init__.py",
        r"async def async_setup_entry",
        "the integration entry point the bootstrap calls",
    ),
    (
        "server/companion_auth.py",
        r"(?<!await )hass\.auth\.async_validate_access_token",
        "token validation is still called synchronously (AuthManager is sync)",
    ),
]

SONOS_ONLY_SERVICE = re.compile(r'"sonos"\s*,\s*\n\s*"[a-z_]+"')


def check_seams(read: Callable[[str], str | None]) -> int:
    """Assert the upstream internals the port depends on are still there."""
    failures = 0
    print("\n=== seams ===")
    for rel_path, pattern, description in SEAMS:
        source = read(rel_path)
        if source is None:
            print(f"  MISSING FILE {rel_path}")
            failures += 1
            continue
        if re.search(pattern, source, re.S):
            print(f"  ok   {rel_path}: {description}")
        else:
            print(f"  FAIL {rel_path}: {description}")
            failures += 1

    # _play_via_sonos must not have grown a sonos-specific service call, or the
    # "borrow the sonos path" trick stops being provider-agnostic.
    source = read("services/media_player.py")
    if source:
        match = re.search(r"async def _play_via_sonos.*?(?=\n    async def )", source, re.S)
        if match and SONOS_ONLY_SERVICE.search(match.group(0)):
            print("  FAIL services/media_player.py: _play_via_sonos now calls a sonos.* service")
            failures += 1
    return failures


def _iter_sources(root: Path):
    """Yield (display_path, source) for every Python file under `root`."""
    for path in sorted(root.rglob("*.py")):
        yield str(path.relative_to(root)), path.read_text(encoding="utf-8", errors="replace")


def _iter_sources_from_tag(tag: str):
    """Yield (display_path, source) from a GitHub tag, streamed in memory."""
    url = f"https://codeload.github.com/mholzi/beatify/tar.gz/refs/tags/{tag}"
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed host
        raw = io.BytesIO(response.read())
    with tarfile.open(fileobj=raw, mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            parts = member.name.split("/")
            if "custom_components" not in parts:
                continue
            index = parts.index("custom_components")
            handle = tar.extractfile(member)
            if handle is None:
                continue
            yield "/".join(parts[index + 2 :]), handle.read().decode("utf-8", "replace")


def collect(sources) -> dict[str, list[str]]:
    """Extract the HA surface used by a set of Python sources."""
    imports: set[str] = set()
    attrs: set[str] = set()
    services: set[str] = set()

    for display, source in sources:
        for domain, service in SERVICE_CALL.findall(source):
            services.add(f"{domain}.{service}")

        for match in HASS_ATTR.finditer(source):
            first, second = match.group(1), match.group(2)
            attrs.add(f"hass.{first}.{second}" if second else f"hass.{first}")

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover - upstream is valid Python
            print(f"  ! could not parse {display}: {exc}", file=sys.stderr)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "homeassistant":
                    for alias in node.names:
                        imports.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "homeassistant":
                        imports.add(alias.name)

    # `hass.data.<key>` is dict access, not API surface — collapse it.
    attrs = {a for a in attrs if not a.startswith("hass.data.")} | (
        {"hass.data"} if any(a.startswith("hass.data") for a in attrs) else set()
    )

    return {
        "imports": sorted(imports),
        "attrs": sorted(attrs),
        "services": sorted(services),
    }


def _report(surface: dict[str, list[str]]) -> None:
    for section in ("imports", "attrs", "services"):
        print(f"\n=== {section} ({len(surface[section])}) ===")
        for item in surface[section]:
            print(f"  {item}")


def _check(surface: dict[str, list[str]], baseline: dict[str, list[str]]) -> int:
    failures = 0
    for section in ("imports", "attrs", "services"):
        known = set(baseline.get(section, []))
        new = [item for item in surface[section] if item not in known]
        gone = [item for item in known if item not in surface[section]]
        if new:
            failures += len(new)
            print(f"\nNEW {section} — the shim does not cover these yet:")
            for item in new:
                print(f"  + {item}")
        if gone:
            print(f"\nno longer used {section} (shim may drop these):")
            for item in gone:
                print(f"  - {item}")
    if failures:
        print(f"\nFAIL: {failures} unsupported item(s). Extend the shim before bumping.")
    else:
        print("\nOK: upstream stays inside the shim's known surface.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", nargs="?", default=str(DEFAULT_TREE))
    parser.add_argument("--report", action="store_true", help="print surface, do not check")
    parser.add_argument("--update", action="store_true", help="write surface as new baseline")
    parser.add_argument("--tag", help="check an upstream git tag instead of a local tree")
    parser.add_argument("--seams-only", action="store_true", help="only run the seam checks")
    args = parser.parse_args()

    if args.tag:
        print(f"streaming upstream {args.tag} ...")
        sources = dict(_iter_sources_from_tag(args.tag))
    else:
        root = Path(args.tree)
        if not root.is_dir():
            parser.error(f"not a directory: {root}")
        sources = dict(_iter_sources(root))

    def read(rel_path: str) -> str | None:
        return sources.get(rel_path)

    if args.seams_only:
        return 1 if check_seams(read) else 0

    surface = collect(sources.items())

    if args.report:
        _report(surface)
        return 0

    if args.update:
        BASELINE.write_text(json.dumps(surface, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written: {BASELINE}")
        print(
            f"  {len(surface['imports'])} imports, "
            f"{len(surface['attrs'])} attrs, {len(surface['services'])} services"
        )
        return 0

    if not BASELINE.exists():
        parser.error(f"no baseline at {BASELINE} — run with --update first")
    failures = _check(surface, json.loads(BASELINE.read_text(encoding="utf-8")))
    return 1 if (failures or check_seams(read)) else 0


if __name__ == "__main__":
    sys.exit(main())
