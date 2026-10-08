"""Minimal HTML builder with escaping by construction.

Every ``str`` child or attribute value is escaped; only :class:`Safe` (built by this
module) is emitted as is. There is no way to pass raw markup from data.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

VOID = frozenset({"meta", "link", "input", "br"})


class Safe(str):
    """Markup produced by :func:`el`; never construct it from data."""

    __slots__ = ()


def _children(items: Iterable[object]) -> Iterable[str]:
    for item in items:
        if item is None or item is False:
            continue
        if isinstance(item, Safe):
            yield item
        elif isinstance(item, str):
            yield escape(item, quote=True)
        elif isinstance(item, Iterable):
            yield from _children(item)
        else:
            yield escape(str(item), quote=True)


def el(tag: str, *children: object, **attrs: str | bool | None) -> Safe:
    """``el("a", "text", href="/x", aria_current="page")``; ``class_`` sets ``class``."""
    parts = [tag]
    for name, value in attrs.items():
        if value is None or value is False:
            continue
        attr = name.rstrip("_").replace("_", "-")
        if value is True:
            parts.append(attr)
        else:
            parts.append(f'{attr}="{escape(value, quote=True)}"')
    opening = f"<{' '.join(parts)}>"
    if tag in VOID:
        return Safe(opening)
    return Safe(f"{opening}{''.join(_children(children))}</{tag}>")


def join(*children: object) -> Safe:
    return Safe("".join(_children(children)))
