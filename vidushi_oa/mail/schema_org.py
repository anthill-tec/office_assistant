"""schema.org extraction from mail HTML (CR-OA-028 §S2).

Pulls structured ``Order`` / ``Invoice`` / ``ParcelDelivery`` entities out of an
email body's HTML — the machine-readable layer many order-confirmation and
shipping mails embed — so the higher tiers (§S3 candidate-building, §S4
heuristic fallback) can work from parsed data instead of scraping prose.

STDLIB ONLY: this reads markup with :mod:`html.parser` and decodes JSON-LD with
:mod:`json` — no ``bs4``/``lxml`` (which are not installed, and never belong in
this pure library path). :func:`extract_schema_org` is a pure function: no I/O,
no network, no adapter — HTML string in, list of plain dicts out.

Two markup dialects are recognised:

* **JSON-LD** — every ``<script type="application/ld+json">`` block is decoded;
  a top-level object, a top-level array, and a JSON-LD ``@graph`` array are all
  unwrapped. Only objects whose ``@type`` is ``Order``/``Invoice``/
  ``ParcelDelivery`` are returned as top-level results; their inner objects
  (``OrderItem``/``Product``/``Organization``/``PostalAddress``/
  ``MonetaryAmount`` …) stay nested dicts. Malformed JSON is skipped, never
  raised.
* **Microdata** — an ``itemscope``/``itemtype`` element whose type path ends in
  a recognised type becomes an equivalent dict; each descendant ``itemprop``
  becomes a key (its text, or a nested dict when the ``itemprop`` element is
  itself an ``itemscope``). A nested ``itemscope`` is attached to its parent,
  never returned as a separate top-level result.

**Injection-safe by construction:** every value is inert data read verbatim out
of the markup. The parser never evaluates, imports, dispatches on, or otherwise
acts on any field content — imperative text in, say, a ``description`` comes
back as an unchanged string.
"""
import json
from html.parser import HTMLParser

_RECOGNISED_TYPES = frozenset({"Order", "Invoice", "ParcelDelivery"})


def _type_recognised(value) -> bool:
    """True if a JSON-LD ``@type`` (string or list) names a recognised entity."""
    if isinstance(value, list):
        return any(v in _RECOGNISED_TYPES for v in value)
    return value in _RECOGNISED_TYPES


class _JsonLdScriptParser(HTMLParser):
    """Capture the raw text of every ``application/ld+json`` script block.

    ``html.parser`` treats ``<script>`` content as CDATA, so the JSON body is
    handed back untouched (tags/entities inside are not parsed away)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "script":
            attr_map = dict(attrs)
            script_type = (attr_map.get("type") or "").strip().lower()
            if script_type == "application/ld+json":
                self._capturing = True
                self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self.blocks.append("".join(self._buffer))
            self._capturing = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)


def _collect_jsonld_node(node, results: list[dict]) -> None:
    """Walk a decoded JSON-LD node, collecting recognised top-level entities.

    Unwraps top-level arrays and a JSON-LD ``@graph`` array; a recognised object
    is appended as-is (its nested objects stay nested). Never descends into an
    entity's own fields — nesting is preserved, not flattened."""
    if isinstance(node, list):
        for item in node:
            _collect_jsonld_node(item, results)
        return
    if not isinstance(node, dict):
        return
    graph = node.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            _collect_jsonld_node(item, results)
        return
    if _type_recognised(node.get("@type")):
        results.append(node)


def _extract_jsonld(html: str) -> list[dict]:
    parser = _JsonLdScriptParser()
    parser.feed(html)
    parser.close()
    results: list[dict] = []
    for block in parser.blocks:
        try:
            decoded = json.loads(block)
        except (ValueError, TypeError):
            # Malformed JSON in a script block is skipped silently, never raised.
            continue
        _collect_jsonld_node(decoded, results)
    return results


class _MicrodataParser(HTMLParser):
    """Build recognised entities from ``itemscope``/``itemprop`` microdata.

    Keeps a stack of open elements. An ``itemscope`` element opens an item dict
    (``@type`` = the last path segment of its ``itemtype``); an ``itemprop``
    child contributes a key — a nested dict when it is itself an ``itemscope``,
    otherwise its trimmed text. Only recognised top-level items (an ``itemscope``
    with no enclosing item and no ``itemprop`` of its own) reach the results;
    nested items attach to their parent regardless of type."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_map = dict(attrs)
        item: dict | None = None
        if "itemscope" in attr_map:
            item = {}
            itemtype = attr_map.get("itemtype")
            if itemtype:
                item["@type"] = itemtype.rstrip("/").split("/")[-1]
        self._stack.append({
            "tag": tag,
            "itemprop": attr_map.get("itemprop"),
            "item": item,
            "text": [],
        })

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        match_idx = None
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx]["tag"] == tag:
                match_idx = idx
                break
        if match_idx is None:
            return
        while len(self._stack) > match_idx:
            self._finalise(self._stack.pop())

    def _finalise(self, frame: dict) -> None:
        parent = None
        for candidate in reversed(self._stack):
            if candidate["item"] is not None:
                parent = candidate["item"]
                break
        item = frame["item"]
        itemprop = frame["itemprop"]
        if item is not None:
            if itemprop is not None and parent is not None:
                parent[itemprop] = item
            elif itemprop is None and parent is None:
                if item.get("@type") in _RECOGNISED_TYPES:
                    self.results.append(item)
            # else: an itemscope that is neither a top-level entity nor a
            # property of a parent item — nothing to record.
        elif itemprop is not None and parent is not None:
            parent[itemprop] = "".join(frame["text"]).strip()


def _extract_microdata(html: str) -> list[dict]:
    parser = _MicrodataParser()
    parser.feed(html)
    parser.close()
    return parser.results


def extract_schema_org(html: str) -> list[dict]:
    """Return every schema.org ``Order``/``Invoice``/``ParcelDelivery`` entity.

    Scans ``html`` for both JSON-LD (``<script type="application/ld+json">``)
    and microdata (``itemscope``/``itemprop``) markup and returns the recognised
    top-level entities as plain dicts, nested objects preserved. HTML with no
    schema.org markup yields ``[]``. Malformed JSON-LD is skipped, never raised.

    Every returned value is inert data read verbatim from the markup — the
    parser never acts on field content."""
    if not html:
        return []
    return _extract_jsonld(html) + _extract_microdata(html)
