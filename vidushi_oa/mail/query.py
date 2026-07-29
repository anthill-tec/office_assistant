"""The portable mail-search query grammar (CR-OA-031 §S1).

This module is the **sole definition** of the portable query grammar the CLI
advertises (`voa mail-search`): bare keywords, quoted `"exact phrase"`s, the
qualifiers `subject:` `from:` `to:` `category:` `newer_than:` `has:attachment`,
and `OR` alternation with implicit-AND as the default.

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
import shlex

#: Qualifiers that take a free-form value (`<name>:<value>`).
_VALUE_QUALIFIERS = ("subject", "from", "to", "category", "newer_than")

#: `newer_than:` unit -> its length in days. Weeks/months/years fold to days so
#: the model only ever carries an absolute cutoff date.
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class QueryParseError(ValueError):
    """Raised when a query contains an unknown qualifier, an unknown
    relative-date unit, or is otherwise unparseable. The message always names
    the offending token."""


@dataclass
class QueryModel:
    """A provider-neutral parsed query.

    `terms` holds the bare keywords and standalone quoted phrases in query
    order, each phrase preserved as ONE value. `newer_than` is an **absolute**
    cutoff date (inclusive lower bound), already normalised from whatever
    relative unit the query used. `operator` is `"AND"` (the implicit default)
    or `"OR"` when the query used the `OR` alternation.
    """

    terms: list[str] = field(default_factory=list)
    subject: str | None = None
    from_: str | None = None
    to: str | None = None
    category: str | None = None
    has_attachment: bool = False
    newer_than: date | None = None
    operator: str = "AND"


def parse(query: str, *, today: date | None = None) -> QueryModel:
    """Parse a portable query string into a `QueryModel`.

    `today` is the reference date `newer_than:` resolves against; it defaults
    to the real current date and is injectable so callers (and tests) can be
    deterministic. An empty or whitespace-only query parses to an empty model —
    no terms, no qualifiers, implicit-AND — which compilers treat as "no
    filter" rather than an error.

    Raises `QueryParseError` for an unknown qualifier, an unknown/ malformed
    `newer_than:` value, or unbalanced quoting.
    """
    model = QueryModel()
    if not query or not query.strip():
        return model

    reference = today if today is not None else date.today()

    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        raise QueryParseError(
            f"could not parse query {query!r}: {exc}"
        ) from exc

    for token in tokens:
        if token == "OR":
            model.operator = "OR"
            continue
        if ":" not in token:
            model.terms.append(token)
            continue

        name, _, value = token.partition(":")
        if name == "has":
            if value != "attachment":
                raise QueryParseError(
                    f"unknown qualifier {token!r}: `has:` accepts only "
                    "`has:attachment`"
                )
            model.has_attachment = True
        elif name == "newer_than":
            model.newer_than = reference - _relative_span(value, token)
        elif name == "subject":
            model.subject = value
        elif name == "from":
            model.from_ = value
        elif name == "to":
            model.to = value
        elif name == "category":
            model.category = value
        else:
            raise QueryParseError(
                f"unknown qualifier {token!r}: expected one of "
                + ", ".join(f"{q}:" for q in _VALUE_QUALIFIERS)
                + ", has:attachment"
            )

    return model


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
