"""Servlet/JSP request vocabulary shared by the Java and JSP front-ends.

Keeping the method and implicit-object tables in one place stops the two parsers from
drifting: a request API added for servlets is immediately available to JSP scriptlets and
EL expressions.
"""

from __future__ import annotations

from legacylens.ir.models import RequestInputKind

__all__ = [
    "EL_INPUT_OBJECTS",
    "SERVLET_REQUEST_CALLS",
    "SERVLET_REQUEST_TYPES",
    "STRUTS_FORM_TYPES",
]

# method name -> (request-input kind, whether argument 0 is the input name)
SERVLET_REQUEST_CALLS: dict[str, tuple[RequestInputKind, bool]] = {
    "getParameter": (RequestInputKind.REQUEST, True),
    "getParameterValues": (RequestInputKind.REQUEST, True),
    "getParameterMap": (RequestInputKind.REQUEST, False),
    "getParameterNames": (RequestInputKind.REQUEST, False),
    "getHeader": (RequestInputKind.HEADER, True),
    "getHeaders": (RequestInputKind.HEADER, True),
    "getHeaderNames": (RequestInputKind.HEADER, False),
    "getIntHeader": (RequestInputKind.HEADER, True),
    "getDateHeader": (RequestInputKind.HEADER, True),
    "getCookies": (RequestInputKind.COOKIE, False),
    "getInputStream": (RequestInputKind.BODY, False),
    "getReader": (RequestInputKind.BODY, False),
    "getPart": (RequestInputKind.FILE, True),
    "getParts": (RequestInputKind.FILE, False),
    "getPathInfo": (RequestInputKind.PATH, False),
    "getPathTranslated": (RequestInputKind.PATH, False),
    "getRequestURI": (RequestInputKind.PATH, False),
    "getRequestURL": (RequestInputKind.PATH, False),
    "getServletPath": (RequestInputKind.PATH, False),
    "getContextPath": (RequestInputKind.PATH, False),
    "getQueryString": (RequestInputKind.QUERY, False),
}

SERVLET_REQUEST_TYPES = frozenset(
    {
        "HttpServletRequest",
        "MultipartHttpServletRequest",
        "ServletRequest",
    }
)

STRUTS_FORM_TYPES = frozenset(
    {
        "ActionForm",
        "DynaActionForm",
        "DynaValidatorActionForm",
        "DynaValidatorForm",
        "ValidatorActionForm",
        "ValidatorForm",
    }
)

# JSP EL implicit object -> request-input kind. ``pageContext`` and session scopes are
# deliberately absent: they expose server state, not a raw request value.
EL_INPUT_OBJECTS: dict[str, RequestInputKind] = {
    "param": RequestInputKind.REQUEST,
    "paramValues": RequestInputKind.REQUEST,
    "header": RequestInputKind.HEADER,
    "headerValues": RequestInputKind.HEADER,
    "cookie": RequestInputKind.COOKIE,
}
