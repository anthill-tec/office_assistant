"""CR-OA-022 §S2 — RFC 5322 composition + reply threading + From-identity (RED).

`vidushi_oa/mail/compose.py` does not exist yet. Each behavioural test below
imports `compose`/`validate_from` from it INSIDE the test method (never at
module scope) so a missing module fails only that individual test with
`ModuleNotFoundError` rather than breaking collection for the whole file.
Today every test in this file fails for one of two legitimate
"compose.py absent" reasons: a `ModuleNotFoundError` on the deferred import, or
(for the source-audit test) a plain assertion that the file does not exist yet.

Pinned shapes for GREEN (per CR-OA-022 §S2 + DN-mail-access.md §Decision 7):

  - `compose(from_addr, to, subject, body, cc=None, in_reply_to=None,
    references=None, attachments=None) -> bytes` builds a valid RFC 5322
    message via `email.message.EmailMessage` and returns `.as_bytes()`. Sets
    From/To/Subject and a plain-text body; sets Cc only when given; sets
    `In-Reply-To` + `References` only when given (`references` may be a list
    of Message-IDs or a single space-joined string).
  - `validate_from(from_addr, identities) -> None` raises `ValueError` whose
    message names the invalid `from_addr` when it is not a member of the
    `identities` set (the account address + configured aliases); returns
    `None` quietly when `from_addr` is a member.

No personal data: the module must not hardcode a real mailbox address or
Fastmail masked alias — every From/To/Cc/alias value must come from caller
args (DN Consequences invariant, §S2 AC3).
"""
import email
import os
import re
import unittest
from email import policy


def _parse(raw):
    """Parse composed bytes with the modern policy so `.get_body()` works."""
    return email.message_from_bytes(raw, policy=policy.default)


class ComposeMessageTest(unittest.TestCase):
    """§S2 AC1 — compose() builds a valid RFC 5322 message with a plain-text body."""

    def test_compose_sets_from_to_subject_and_plaintext_body(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        self.assertIsInstance(raw, bytes, "compose() must return bytes")
        parsed = _parse(raw)
        self.assertEqual(str(parsed["From"]), "me@x")
        self.assertEqual(str(parsed["To"]), "v@y")
        self.assertEqual(str(parsed["Subject"]), "S")
        body_part = parsed.get_body(preferencelist=("plain",))
        self.assertIsNotNone(body_part, "composed message must carry a plain-text body part")
        self.assertIn("B", body_part.get_content())

    def test_compose_includes_cc_header_when_given(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B", cc="c@z")
        parsed = _parse(raw)
        self.assertEqual(str(parsed["Cc"]), "c@z")

    def test_compose_omits_cc_header_when_not_given(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        parsed = _parse(raw)
        self.assertIsNone(parsed["Cc"], "Cc header must be absent when no cc arg is given")


class ComposeOriginatorHeadersTest(unittest.TestCase):
    """RFC 5322 §3.6.4 originator headers — a composed message must carry its own
    `Date` and `Message-ID`.

    Beyond RFC completeness this is what keeps two identical `voa mail-draft`
    runs from serialising to byte-identical RFC822: Fastmail/Cyrus blobIds are
    content-addressed, so identical bytes collide on the same blob and the
    second `Email/import` answers an `alreadyExists` SetError."""

    def test_compose_sets_a_date_header(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        parsed = _parse(raw)
        self.assertIsNotNone(parsed["Date"], "composed message must carry a Date header")

    def test_compose_sets_a_message_id_header(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        parsed = _parse(raw)
        message_id = str(parsed["Message-ID"] or "")
        self.assertTrue(
            message_id.startswith("<") and message_id.endswith(">"),
            f"composed message must carry an angle-addr Message-ID; got {message_id!r}",
        )

    def test_message_id_domain_comes_from_the_from_address_not_the_local_host(self):
        """RFC 5322 §3.6.4 wants a domain the sender owns — and the stdlib default
        (`socket.getfqdn()`) would leak the user's machine name into every draft."""
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="Me <me@fastmail.com>", to="v@y", subject="S", body="B")
        parsed = _parse(raw)
        self.assertTrue(
            str(parsed["Message-ID"]).endswith("@fastmail.com>"),
            f"Message-ID must be scoped to the From domain; got {parsed['Message-ID']!r}",
        )

    def test_two_identical_composes_are_not_byte_identical(self):
        from vidushi_oa.mail.compose import compose

        first = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        second = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        self.assertNotEqual(
            first, second,
            "identical compose() calls must not serialise to identical bytes — a "
            "content-addressed blob store would collide them onto one draft",
        )


class ComposeReplyThreadingTest(unittest.TestCase):
    """§S2 AC1 (reply half) — In-Reply-To / References threading."""

    def test_compose_sets_in_reply_to_from_source_message_id(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(
            from_addr="me@x", to="v@y", subject="Re: S", body="B",
            in_reply_to="<m1@y>", references=["<m0@y>", "<m1@y>"],
        )
        parsed = _parse(raw)
        self.assertEqual(str(parsed["In-Reply-To"]), "<m1@y>")

    def test_compose_references_header_contains_full_chain_from_a_list(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(
            from_addr="me@x", to="v@y", subject="Re: S", body="B",
            in_reply_to="<m1@y>", references=["<m0@y>", "<m1@y>"],
        )
        parsed = _parse(raw)
        refs = str(parsed["References"])
        self.assertIn("<m0@y>", refs)
        self.assertIn("<m1@y>", refs)

    def test_compose_accepts_a_space_joined_references_string(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(
            from_addr="me@x", to="v@y", subject="Re: S", body="B",
            in_reply_to="<m1@y>", references="<m0@y> <m1@y>",
        )
        parsed = _parse(raw)
        refs = str(parsed["References"])
        self.assertIn("<m0@y>", refs)
        self.assertIn("<m1@y>", refs)

    def test_compose_omits_reply_headers_when_not_a_reply(self):
        from vidushi_oa.mail.compose import compose

        raw = compose(from_addr="me@x", to="v@y", subject="S", body="B")
        parsed = _parse(raw)
        self.assertIsNone(parsed["In-Reply-To"], "a non-reply message must carry no In-Reply-To header")
        self.assertIsNone(parsed["References"], "a non-reply message must carry no References header")

    def test_compose_reply_threads_from_a_fetched_message(self):
        """A reply built FROM a `mail-get`-fetched `Message` (base.py shape) threads
        correctly: the source's own id becomes In-Reply-To, appended onto its
        existing References chain — the shape the §S3 `mail-reply` verb will build."""
        from vidushi_oa.mail.base import Message
        from vidushi_oa.mail.compose import compose

        source = Message(
            id="<m1@y>", account="fastmail_main", source_tag="[FM]",
            subject="Original", sender="v@y", references="<m0@y>", in_reply_to="",
        )
        chain = (source.references.split() if source.references else []) + [source.id]
        raw = compose(
            from_addr="me@x", to=source.sender, subject="Re: " + source.subject,
            body="B", in_reply_to=source.id, references=chain,
        )
        parsed = _parse(raw)
        self.assertEqual(str(parsed["In-Reply-To"]), "<m1@y>")
        refs = str(parsed["References"])
        self.assertIn("<m0@y>", refs)
        self.assertIn("<m1@y>", refs)


class ValidateFromTest(unittest.TestCase):
    """§S2 AC2 — From-identity validation against the account's identities/aliases."""

    def test_validate_from_raises_value_error_naming_the_invalid_address(self):
        from vidushi_oa.mail.compose import validate_from

        with self.assertRaises(ValueError) as ctx:
            validate_from("stranger@evil", {"me@x", "alias@x"})
        self.assertIn("stranger@evil", str(ctx.exception))

    def test_validate_from_accepts_the_account_address(self):
        from vidushi_oa.mail.compose import validate_from

        self.assertIsNone(validate_from("me@x", {"me@x", "alias@x"}))

    def test_validate_from_accepts_a_configured_alias(self):
        from vidushi_oa.mail.compose import validate_from

        self.assertIsNone(validate_from("alias@x", {"me@x", "alias@x"}))


class NoPersonalDataInComposeModuleTest(unittest.TestCase):
    """§S2 AC3 — no hardcoded real mailbox address / masked alias in compose.py
    (DN Consequences invariant): every From/To/Cc/alias value must come from
    caller args, never a literal baked into the module."""

    _ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _ALLOWED_PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net")

    def test_compose_module_hardcodes_no_real_mailbox_address(self):
        import vidushi_oa

        compose_path = os.path.join(os.path.dirname(vidushi_oa.__file__), "mail", "compose.py")
        self.assertTrue(
            os.path.isfile(compose_path),
            "vidushi_oa/mail/compose.py does not exist yet (§S2 not implemented)",
        )
        with open(compose_path, encoding="utf-8") as f:
            source = f.read()
        offenders = [
            m for m in self._ADDRESS_RE.findall(source)
            if m.split("@", 1)[1].lower() not in self._ALLOWED_PLACEHOLDER_HOSTS
        ]
        self.assertEqual(
            offenders, [],
            f"compose.py hardcodes non-placeholder address literal(s): {offenders!r}",
        )


if __name__ == "__main__":
    unittest.main()
