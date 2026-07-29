"""The portable mail-search query grammar (CR-OA-031 §S1).

This module is the **sole definition** of the portable query grammar the CLI
advertises (`voa mail-search`): bare keywords, quoted `"exact phrase"`s, the
qualifiers `subject:` `from:` `to:` `category:` `newer_than:` `has:attachment`,
`OR` alternation with implicit-AND as the default, and **nestable parenthesised
groups** (advertised by `voa mail-search --help`, so dropping them would be a
capability regression against our own documented grammar).

Because groups nest, the model is a small **tree** of `QueryNode`s hanging off
`QueryModel.root`: a node is either a LEAF (a bare term/quoted phrase, or one
qualifier + its parsed value) or a GROUP (an `AND`/`OR` operator over a
non-empty list of child nodes). Every compiler walks that tree recursively.
The flat `QueryModel` fields (`terms`, `subject`, …) remain as a convenience
projection of the top level of a query that uses **no** parentheses; a grouped
query keeps each qualifier inside its group and leaves those fields unset.

`parse()` turns a query string into a provider-neutral `QueryModel`; the
provider adapters compile that model down to their native syntax (§S2 JMAP,
§S3 Gmail `X-GM-RAW`, §S4 RFC 3501 IMAP) and never see the raw string. Anything
the grammar does not recognise raises `QueryParseError` naming the offending
token — never a silent no-op, which is the failure mode this CR exists to
remove.

Relative dates normalise **once, here**: `newer_than:<N><unit>` with
`unit in {d, w, m, y}` resolves to an absolute cutoff `date`, so no provider
ever receives a unit it cannot express (Gmail, for one, has no `w`). The
calendar-free convention, applied uniformly:

    d -> 1 day    w -> 7 days    m -> 30 days    y -> 365 days
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

#: Qualifiers that take a free-form value (`<name>:<value>`).
_VALUE_QUALIFIERS = ("subject", "from", "to", "category", "newer_than")

#: Query-token qualifier name -> the `QueryNode.qualifier` name it parses to.
#: `from` is `from_` so the model never collides with the Python keyword.
_QUALIFIER_NAMES = {
    "subject": "subject",
    "from": "from_",
    "to": "to",
    "category": "category",
}

#: `newer_than:` unit -> its length in days. Weeks/months/years fold to days so
#: the model only ever carries an absolute cutoff date.
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class QueryParseError(ValueError):
    """Raised when a query contains an unknown qualifier, an unknown
    relative-date unit, or is otherwise unparseable. The message always names
    the offending token."""


class UnsupportedQualifierError(ValueError):
    """Raised when a portable query carries a qualifier the target provider
    cannot express (CR-OA-031 §S4/§S5).

    It lives here, beside the grammar, because it is a property of the
    provider-neutral model — every compiler (`jmap.compile_filter`,
    `imap.compile_imap_query`) raises the SAME type, and `vidushi_oa.mail.imap`
    re-exports it for existing importers. The message always names the
    offending qualifier so the caller knows exactly what was refused. Refusing
    beats dropping: a silently ignored qualifier returns a confidently wrong
    result set.
    """


@dataclass
class QueryNode:
    """One node of the provider-neutral query tree — a group or a leaf.

    GROUP: `operator` is `"AND"` or `"OR"` and `children` is a non-empty list of
    child nodes (leaves and/or nested groups); `term`/`qualifier`/`value` are
    all `None`.

    LEAF: `children` is empty and `operator` is `None`; EITHER `term` holds a
    bare keyword / quoted phrase, OR `qualifier` holds one of `"subject"`,
    `"from_"`, `"to"`, `"category"`, `"newer_than"`, `"has_attachment"` and
    `value` holds its parsed value — a `str` for the free-form qualifiers, the
    resolved absolute `date` for `newer_than`, `True` for `has_attachment`.
    """

    operator: str | None = None
    children: list["QueryNode"] = field(default_factory=list)
    term: str | None = None
    qualifier: str | None = None
    value: object = None

    @property
    def is_group(self) -> bool:
        """True when this node is a group (an operator over child nodes)."""
        return self.operator is not None


@dataclass
class QueryModel:
    """A provider-neutral parsed query.

    `terms` holds the bare keywords and standalone quoted phrases in query
    order, each phrase preserved as ONE value. `newer_than` is an **absolute**
    cutoff date (inclusive lower bound), already normalised from whatever
    relative unit the query used. `operator` is `"AND"` (the implicit default)
    or `"OR"` when the query used the `OR` alternation.

    `root` is the authoritative form: the whole query as a `QueryNode` tree,
    populated for **every** query, grouped or not — it is what the compilers
    walk. The flat fields above are a convenience projection of the top level,
    filled in only for a query that uses no parentheses; in a grouped query
    each qualifier stays inside its group and the flat fields stay unset.
    """

    terms: list[str] = field(default_factory=list)
    subject: str | None = None
    from_: str | None = None
    to: str | None = None
    category: str | None = None
    has_attachment: bool = False
    newer_than: date | None = None
    operator: str = "AND"
    root: QueryNode = field(default_factory=lambda: QueryNode(operator="AND"))


def parse(query: str, *, today: date | None = None) -> QueryModel:
    """Parse a portable query string into a `QueryModel`.

    `today` is the reference date `newer_than:` resolves against; it defaults
    to the real current date and is injectable so callers (and tests) can be
    deterministic. An empty or whitespace-only query parses to an empty model —
    no terms, no qualifiers, implicit-AND — which compilers treat as "no
    filter" rather than an error.

    Raises `QueryParseError` for an unknown qualifier, an unknown/ malformed
    `newer_than:` value, unbalanced quoting, or unbalanced parentheses — each
    message naming the offending token.
    """
    model = QueryModel()
    if not query or not query.strip():
        return model

    reference = today if today is not None else date.today()

    tokens = _tokenize(query)
    root, index = _parse_group(tokens, 0, query, reference, depth=0)
    if index < len(tokens):
        raise QueryParseError(
            f"unbalanced parentheses in query {query!r}: unmatched ')' token"
        )
    model.root = root
    if not any(kind == _PAREN for kind, _ in tokens):
        _project_flat(model, root)
    return model


#: Token kinds produced by `_tokenize`. `_QUOTED` is a word token that OPENED
#: with a quote (`"https://example.com/x"`) — the quotes are gone by the time
#: `_leaf` sees the value, so the kind is what carries "this was quoted, treat
#: it as a literal term". `subject:"re: invoice"` opens unquoted and so stays a
#: plain `_WORD` whose qualifier prefix is still honoured.
_WORD = "WORD"
_QUOTED = "QUOTED"
_PAREN = "PAREN"


def _tokenize(query: str) -> list[tuple[str, str]]:
    """Split a query into `(kind, value)` tokens.

    Parentheses are their own tokens, but ONLY outside a quoted phrase — a
    `"literal (paren)"` stays part of its phrase. Quoting follows the shell
    convention the grammar already used (`"…"`/`'…'`, backslash escapes), and
    an unclosed quote is a `QueryParseError` naming the query.
    """
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    started = False
    opened_quoted = False
    quote = None
    index = 0
    length = len(query)

    def flush():
        nonlocal started, opened_quoted
        if started:
            tokens.append((_QUOTED if opened_quoted else _WORD, "".join(buf)))
            buf.clear()
            started = False
            opened_quoted = False

    while index < length:
        char = query[index]
        if quote is not None:
            if char == "\\" and index + 1 < length:
                buf.append(query[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            else:
                buf.append(char)
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            if not started:
                opened_quoted = True
            started = True
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            buf.append(query[index + 1])
            started = True
            index += 2
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        if char in "()":
            flush()
            tokens.append((_PAREN, char))
            index += 1
            continue
        buf.append(char)
        started = True
        index += 1

    if quote is not None:
        raise QueryParseError(
            f"could not parse query {query!r}: No closing quotation"
        )
    flush()
    return tokens


def _parse_group(tokens, index, query, reference, *, depth):
    """Parse one group's worth of tokens, returning `(node, next_index)`.

    `depth` is the parenthesis nesting level: at depth 0 a `)` is a stray
    closing paren, and at any deeper level running out of tokens means an
    unclosed `(`. Both raise `QueryParseError` naming the offending token.
    """
    children: list[QueryNode] = []
    operator = "AND"

    while index < len(tokens):
        kind, value = tokens[index]
        if kind == _PAREN:
            if value == "(":
                child, index = _parse_group(
                    tokens, index + 1, query, reference, depth=depth + 1
                )
                children.append(child)
                continue
            if depth == 0:
                raise QueryParseError(
                    f"unbalanced parentheses in query {query!r}: unmatched "
                    "')' token"
                )
            return _group(operator, children), index + 1

        index += 1
        if value == "OR" and kind == _WORD:
            operator = "OR"
            continue
        children.append(_leaf(value, reference, quoted=kind == _QUOTED))

    if depth > 0:
        raise QueryParseError(
            f"unbalanced parentheses in query {query!r}: unclosed '(' token"
        )
    return _group(operator, children), index


def _group(operator: str, children: list[QueryNode]) -> QueryNode:
    """Build a group node, collapsing a redundant implicit-AND wrapper.

    `(a OR b)` on its own is the OR group itself, not an AND group of one — a
    single child under the default implicit-AND carries no extra meaning. An
    explicit `OR` is always kept, so `a OR` still reports its operator.
    """
    if len(children) == 1 and operator == "AND":
        return children[0]
    return QueryNode(operator=operator, children=children)


def _leaf(token: str, reference: date, *, quoted: bool = False) -> QueryNode:
    """Build one leaf node from a single query token.

    `quoted` marks a token that opened with a quote (`"https://example.com/x"`):
    the quotes are stripped by `_tokenize`, so without the flag such a phrase
    would be indistinguishable from a bare token and its colon would be
    re-parsed as a qualifier prefix. A quoted token is ALWAYS a literal term.

    Raises `QueryParseError` naming the token for an unknown qualifier or an
    unknown/malformed `newer_than:` value.
    """
    if quoted or ":" not in token:
        return QueryNode(term=token)

    name, _, value = token.partition(":")
    if name == "has":
        if value != "attachment":
            raise QueryParseError(
                f"unknown qualifier {token!r}: `has:` accepts only "
                "`has:attachment`"
            )
        return QueryNode(qualifier="has_attachment", value=True)
    if name == "newer_than":
        return QueryNode(
            qualifier="newer_than", value=reference - _relative_span(value, token)
        )
    if name in _QUALIFIER_NAMES:
        return QueryNode(qualifier=_QUALIFIER_NAMES[name], value=value)
    raise QueryParseError(
        f"unknown qualifier {token!r}: expected one of "
        + ", ".join(f"{q}:" for q in _VALUE_QUALIFIERS)
        + ", has:attachment"
        + f'; quote the token ("{token}") to search for it literally'
    )


def _project_flat(model: QueryModel, node: QueryNode) -> None:
    """Fill the flat convenience fields from a parenthesis-free `root`.

    Such a root is either a single leaf or one group of leaves, so the
    projection is exactly the top level — the shape the flat fields have always
    described. A grouped query never reaches here.
    """
    if node.is_group:
        model.operator = node.operator
        for child in node.children:
            _assign_flat(model, child)
        return
    _assign_flat(model, node)


def _assign_flat(model: QueryModel, leaf: QueryNode) -> None:
    """Copy one leaf onto its flat `QueryModel` field."""
    if leaf.term is not None:
        model.terms.append(leaf.term)
    elif leaf.qualifier == "has_attachment":
        model.has_attachment = True
    elif leaf.qualifier is not None:
        setattr(model, leaf.qualifier, leaf.value)


def _relative_span(value: str, token: str) -> timedelta:
    """Resolve a `newer_than:` value (`<N><unit>`) to a `timedelta`.

    `token` is the full original token, named verbatim in any error so the user
    sees exactly what was rejected.
    """
    unit = value[-1:] if value else ""
    count = value[:-1]
    if unit not in _UNIT_DAYS or not count.isdigit():
        raise QueryParseError(
            f"unknown relative-date value in {token!r}: expected "
            "<number><unit> with unit one of " + "/".join(_UNIT_DAYS)
        )
    return timedelta(days=int(count) * _UNIT_DAYS[unit])
