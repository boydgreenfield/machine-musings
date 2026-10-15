"""Server-side renderers that produce trusted markup for the UI.

This package is the only place in ``coverset.web`` that creates :class:`markupsafe.Markup` (the
templates use ``|safe`` on nothing else). Every module here escapes each interpolated value itself:
text content with :func:`~coverset.web.render.html.esc`, attribute values with
:func:`~coverset.web.render.html.attr`, URLs with :func:`~coverset.web.render.html.safe_href`. Each
module has an XSS test in ``tests/web/test_render_xss.py``.

Modules: :mod:`.html` (escaping and SVG helpers), :mod:`.source` (Pygments source view with coverage
line states), :mod:`.treemap` (squarified treemap), :mod:`.shapegrid` (shape grid and mini strip),
:mod:`.ribbon` (flow ribbon), :mod:`.timeline` (run timeline strip, shard Gantt, "why this order"
bars) and :mod:`.markdown` (GitHub-flavoured markdown without raw HTML).
"""
