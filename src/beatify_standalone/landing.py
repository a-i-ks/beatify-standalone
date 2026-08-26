"""A front door, so the box can be reached by typing its name and nothing else.

Without this, the bare address answers 404 and every page has to be typed from
memory with its full path — awkward on a phone, which is the only computer this
box is meant to need. Type the host, get tappable links.
"""

from __future__ import annotations

from aiohttp import web

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beatify</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #12121a; color: #f2f2f7; padding: 2rem 1rem 3rem; }}
  main {{ max-width: 26rem; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .3rem; }}
  p.sub {{ color: #a0a0b0; font-size: .9rem; margin: 0 0 2rem; }}
  a {{ display: block; background: #1e1e2a; border: 1px solid #2a2a38;
       border-radius: .7rem; padding: 1rem 1.1rem; margin-bottom: .7rem;
       text-decoration: none; color: inherit; }}
  a strong {{ display: block; font-size: 1.05rem; margin-bottom: .15rem; }}
  a span {{ color: #8a8a9a; font-size: .85rem; line-height: 1.4; }}
  a.primary {{ background: #6c5ce7; border-color: #6c5ce7; }}
  a.primary span {{ color: #ddd8ff; }}
  footer {{ color: #6a6a7a; font-size: .75rem; margin-top: 2rem; line-height: 1.6; }}
</style></head><body><main>
<h1>Beatify</h1>
<p class="sub">{host}</p>

<a class="primary" href="/beatify/admin">
  <strong>Spiel hosten</strong>
  <span>Runde starten, QR-Code für die Gäste anzeigen</span></a>

<a href="/beatify/play">
  <strong>Mitspielen</strong>
  <span>Als Spieler einer laufenden Runde beitreten</span></a>

<a href="/beatify/static/dashboard.html">
  <strong>Punktetafel</strong>
  <span>Große Ansicht für den Fernseher</span></a>

<a href="/beatify/audio">
  <strong>Audioausgang</strong>
  <span>Zwischen Klinke und Fernseher umschalten, mit Testton</span></a>

<a href="/beatify/wifi">
  <strong>WLAN einrichten</strong>
  <span>Die Box mit einem neuen Netz verbinden</span></a>

<a href="/beatify/bluetooth">
  <strong>Controller koppeln</strong>
  <span>Gamepad per Bluetooth verbinden</span></a>

<footer>
  Beatify Standalone · das Spiel selbst stammt von
  <a href="https://github.com/mholzi/beatify" style="display:inline;background:none;border:0;padding:0;color:#8a8a9a;text-decoration:underline">mholzi/beatify</a>
</footer>
</main></body></html>
"""


def register_landing_routes(app: web.Application) -> None:
    async def landing(request: web.Request) -> web.StreamResponse:
        host = request.headers.get("X-Forwarded-Host") or request.host
        return web.Response(
            text=_PAGE.format(host=host), content_type="text/html"
        )

    for path in ("/", "/beatify"):
        app.router.add_route("GET", path, landing)
