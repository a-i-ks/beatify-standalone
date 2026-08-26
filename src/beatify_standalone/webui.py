"""Shared shell for the setup pages.

These are operated one-handed on a phone, often standing in someone else's
living room, so the details that matter are unglamorous: tap targets big enough
to hit without aiming, inputs at 16px because anything smaller makes iOS zoom
the viewport and leaves the page scrolled sideways, and respect for the safe
area so nothing hides under the home indicator.
"""

from __future__ import annotations

import html

from aiohttp import web

STYLE = """
  :root {
    color-scheme: light dark;
    --bg: #12121a; --card: #1e1e2a; --line: #2f2f3e;
    --text: #f2f2f7; --dim: #9a9aab; --accent: #6c5ce7;
    --ok: #16a34a; --err: #dc2626;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    font-family: -apple-system, system-ui, sans-serif; margin: 0;
    background: var(--bg); color: var(--text); line-height: 1.5;
    padding: max(1rem, env(safe-area-inset-top)) 1rem
             calc(2rem + env(safe-area-inset-bottom));
  }
  main { max-width: 30rem; margin: 0 auto; }
  /* Nothing may scroll the page sideways, whatever a network happens to be called. */
  html, body { overflow-x: hidden; }
  a.back {
    display: inline-block; color: var(--dim); text-decoration: none;
    font-size: .95rem; padding: .6rem .2rem; margin-bottom: .5rem;
  }
  h1 { font-size: 1.45rem; margin: 0 0 .3rem; letter-spacing: -.01em; }
  p.sub { color: var(--dim); font-size: .93rem; margin: 0 0 1.5rem; }
  h2 {
    font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--dim); margin: 2rem 0 .6rem; font-weight: 600;
  }
  label { display: block; font-size: .85rem; color: var(--dim); margin: 1rem 0 .4rem; }
  /* 16px is not a style choice: below it, iOS zooms the viewport on focus. */
  input, select, button, textarea {
    width: 100%; font-size: 16px; font-family: inherit;
    padding: .95rem 1rem; border-radius: .7rem;
    border: 1px solid var(--line); background: var(--card); color: inherit;
    -webkit-appearance: none; appearance: none; min-height: 3rem;
  }
  select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath fill='%239a9aab' d='M1 1l5 5 5-5'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 1rem center;
    padding-right: 2.5rem;
  }
  button {
    background: var(--accent); border: 0; color: #fff; font-weight: 600;
    margin-top: 1.25rem; cursor: pointer;
  }
  button:disabled { opacity: .5; }
  button.ghost {
    background: transparent; border: 1px solid var(--line);
    color: var(--dim); font-weight: 500; margin-top: .6rem;
  }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: .8rem; padding: 1rem; margin-bottom: .65rem;
  }
  .row { display: flex; align-items: center; gap: .8rem; }
  .row .grow { flex: 1; min-width: 0; }
  /* Long names and MAC addresses must not push the layout sideways. On a
     375 px screen there is no room to give away. */
  .row .grow strong {
    display: block; font-size: 1rem;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .row .grow small {
    color: var(--dim); font-size: .82rem;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block;
  }
  .dot { width: .65rem; height: .65rem; border-radius: 50%; flex: none; }
  .dot.on { background: #4ade80; box-shadow: 0 0 .5rem #4ade8066; }
  .dot.off { background: #555; }
  .msg { padding: .9rem 1rem; border-radius: .7rem; font-size: .92rem; margin-bottom: 1.25rem; }
  .msg.ok { background: #14532d; }
  .msg.err { background: #5b1a1a; }
  .empty { color: var(--dim); font-size: .9rem; padding: .5rem 0; }
  .spinner {
    display: inline-block; width: .9rem; height: .9rem; border-radius: 50%;
    border: 2px solid var(--line); border-top-color: var(--accent);
    animation: spin .8s linear infinite; vertical-align: -.1rem; margin-right: .5rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  pre.log {
    background: #0c0c12; border-radius: .7rem; padding: .9rem; font-size: .78rem;
    line-height: 1.65; white-space: pre-wrap; word-break: break-word;
    max-height: 12rem; overflow-y: auto; margin: 1rem 0 0; color: var(--dim);
  }
"""


def page(title: str, heading: str, subtitle: str, body: str, extra_script: str = "") -> web.Response:
    """Wrap page content in the shared shell."""
    document = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body><main>
<a class="back" href="/">&larr; Übersicht</a>
<h1>{html.escape(heading)}</h1>
<p class="sub">{subtitle}</p>
{body}
</main>{extra_script}</body></html>
"""
    return web.Response(text=document, content_type="text/html")


def message(text: str, ok: bool = True) -> str:
    return f'<div class="msg {"ok" if ok else "err"}">{text}</div>'
