"""Helpers shared by web-facing parsers (PHP pages, JSP, deployment descriptors).

The IR needs stable, project-relative paths even when the deployment root is unknown.
These helpers only apply deterministic conventions: a URL pattern is normalised, a file
under a conventional web root is mapped below that root, and anything else falls back to
the repository-relative path. A wrong guess is never invented where the source is
explicit.
"""

from __future__ import annotations

import posixpath

from legacylens.ir.models import DYNAMIC_INPUT

__all__ = [
    "DYNAMIC_INPUT",
    "is_under_web_inf",
    "normalise_url_path",
    "web_path",
]

# Conventional deployment roots. The first matching segment is treated as the web root;
# nested roots such as src/main/webapp therefore work because "webapp" is a segment.
_WEB_ROOTS = frozenset(
    {
        "htdocs",
        "public",
        "web",
        "webapp",
        "webapps",
        "webcontent",
        "webroot",
        "www",
        "wwwroot",
    }
)


def web_path(file_path: str) -> str:
    """Return the URL-like path of a file under a conventional web root.

    ``src/main/webapp/users/list.jsp`` becomes ``/users/list.jsp``. Files outside a known
    web root keep their repository-relative path, which stays deterministic and matches
    the file citation.
    """

    normalised = file_path.replace("\\", "/").strip("/")
    parts = normalised.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() in _WEB_ROOTS:
            return "/" + "/".join(parts[index + 1 :])
    return "/" + normalised


def is_under_web_inf(file_path: str) -> bool:
    """Whether ``file_path`` is inside a ``WEB-INF`` directory."""

    return any(part.upper() == "WEB-INF" for part in file_path.replace("\\", "/").split("/"))


def normalise_url_path(value: str) -> str:
    """Normalise a declared URL pattern without changing its meaning.

    Leading slashes are enforced, duplicate slashes collapsed and wildcard suffixes kept
    (``/users/*`` stays ``/users/*``). Empty values become ``/``.
    """

    text = " ".join(value.strip().split())
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    if text == "*":
        return "/*"
    if text.endswith("/*"):
        prefix = text[:-2].rstrip("/")
        return (prefix or "") + "/*"
    return "/" + posixpath.normpath(text).lstrip("/")
