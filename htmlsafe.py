"""Sanitization for user-authored proposal HTML.

Proposal bodies are Markdown authored by tenant users, but python-markdown
passes raw HTML straight through. Rendered with `| safe` that is a stored-XSS
sink — most dangerously on the public customer portal. This module strips
scripts, event handlers, and dangerous URLs down to a formatting-only allowlist.
"""

import nh3

# Tags a proposal legitimately uses. Everything else (script, iframe, object,
# style, form, …) is removed.
_ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "sub", "sup", "mark", "small",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "colgroup", "col",
    "a", "img",
}

_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align", "scope"},
    "col": {"span", "width"},
    "*": {"class"},
}


def sanitize(html: str) -> str:
    """Return HTML safe to render with `| safe`.

    Removes <script>/<style>/<iframe>, on* event handlers, and javascript:
    URLs; keeps formatting, tables, links, and images. Link/image URLs are
    restricted to http(s), mailto, and inline data: URIs.
    """
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto", "data"},
        link_rel="noopener noreferrer nofollow",
    )
