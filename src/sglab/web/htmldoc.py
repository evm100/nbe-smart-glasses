"""HTML pages on the lab board: designed write-ups (plans, explainers with SVG figures) posted as a
whole .html file instead of Markdown.

Anybody with the operator role can post one, so the page is treated as untrusted. It is never put
into the board's own HTML. It is served on its own (GET /board/{id}/page) with a Content-Security-
Policy that
  - sandboxes it (an opaque origin, even when the page is opened directly in a tab): its scripts can't
    read the site's cookies, call the API as the viewer, or navigate the board page;
  - blocks every network request (default-src 'none'): no remote scripts, fonts, images or tracking.
    Pages must be self-contained: inline <style>, <script>, <svg>, and data: URLs for images / fonts.
The board shows it in an <iframe sandbox="allow-scripts ..."> that resizes to the page's height.

The board adds one small script to every page (PAGE_JS):
  - it tells the board how tall the page is, so the frame never needs its own scrollbar;
  - it sets <html data-theme="light|dark"> when the viewer picked a theme on the site (?theme= on the
    frame's address, or a message from the board). With no choice the OS setting applies as usual.
A page that follows the site theme styles itself like the site does:
    :root { --bg: #fff; ... }
    @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #111; ... } }
    :root[data-theme="dark"] { --bg: #111; ... }
"""
from __future__ import annotations

import html as _html
import re

# for the served page (header) and the composer's preview (<meta>, where frame-ancestors / sandbox don't apply)
_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
           "font-src data:; media-src data: blob:; form-action 'none'; base-uri 'none'")
CSP = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; " + _POLICY + "; frame-ancestors 'self'"
FRAME_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox"

PAGE_JS = """<script>/* added by the lab board: frame height + site theme */(function () {
  var d = document.documentElement, m = /[?&]theme=(light|dark)\\b/.exec(location.search);
  function theme(t) { if (t === 'light' || t === 'dark') d.setAttribute('data-theme', t); }
  if (m) theme(m[1]);
  window.addEventListener('message', function (e) { if (e.data && e.data.sglabTheme) theme(e.data.sglabTheme); });
  if (window.parent === window) return;
  var last = 0;
  function tell() {
    var h = Math.ceil(Math.max(d.scrollHeight, document.body ? document.body.scrollHeight : 0));
    if (h !== last) { last = h; window.parent.postMessage({sglabDocHeight: h}, '*'); }
  }
  if (window.ResizeObserver) new ResizeObserver(tell).observe(d);
  window.addEventListener('load', tell); document.addEventListener('DOMContentLoaded', tell); setInterval(tell, 1000);
})();</script>"""

_HEAD = re.compile(r"<head\b[^>]*>", re.I)
_BODY_END = re.compile(r"</body\s*>", re.I)
_DROP = re.compile(r"<(script|style|svg|noscript|template|title)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)
_DESC = re.compile(r"<meta\s+[^>]*name\s*=\s*[\"']description[\"'][^>]*>", re.I)
_CONTENT = re.compile(r"content\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)


def page(body: str, preview: bool = False) -> str:
    """The stored page with the board's script added (and, for the composer's srcdoc preview, the
    no-network policy as a <meta>, since a srcdoc frame has no response headers)."""
    s = body or ""
    if preview:
        meta = f'<meta http-equiv="Content-Security-Policy" content="{_POLICY}">'
        m = _HEAD.search(s)
        s = s[:m.end()] + meta + s[m.end():] if m else meta + s
    m = None
    for m in _BODY_END.finditer(s):  # the last </body>, so a "</body>" inside a script or comment can't win
        pass
    return s[:m.start()] + PAGE_JS + s[m.start():] if m else s + PAGE_JS


def text(body: str) -> str:
    """The page's readable text (no markup, scripts, styles or figures)."""
    s = _DROP.sub(" ", body or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def excerpt(body: str, n: int = 240) -> str:
    """<meta name="description"> if the page has one, else the start of its text."""
    m = _DESC.search(body or "")
    c = _CONTENT.search(m.group(0)) if m else None
    s = re.sub(r"\s+", " ", _html.unescape(c.group(1)[1:-1])).strip() if c else ""
    s = s or text(body)
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def reading_minutes(body: str) -> int:
    return max(1, round(len(text(body).split()) / 220))
