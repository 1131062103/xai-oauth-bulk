from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from xai_oauth_bulk.mailbox import (
    Mailbox,
    MailboxConfig,
    MailboxError,
    MailboxService,
    VerificationCodeTimeout,
    domain_is_blocked,
    email_domain,
    extract_verification_code,
    load_blocked_domains_file,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def session_with(*payloads):
    session = Mock()
    session.request.side_effect = [Response(payload) for payload in payloads]
    return session


class MailboxTests(unittest.TestCase):
    def test_extracts_xai_and_numeric_codes(self):
        self.assertEqual(extract_verification_code("", "ABC-123 xAI verification"), "ABC-123")
        self.assertEqual(extract_verification_code("Your verification code: 123456"), "123456")
        self.assertIsNone(extract_verification_code("", "ABC-123 unrelated notification"))

    def test_config_accepts_registration_mailbox_key(self):
        config = MailboxConfig.from_mapping({"mailbox_provider": "cloudflare"})
        self.assertEqual(config.provider, "cloudflare")

    def test_duckmail_provisions_then_polls_code(self):
        session = session_with(
            {"hydra:member": [{"domain": "mail.test", "isVerified": True}]},
            {},
            {"token": "mail-token"},
            {"hydra:member": [{"id": "message-1", "to": [{"address": "ignored@mail.test"}]}]},
            {"subject": "Your code", "text": "Your verification code: 123456"},
        )
        service = MailboxService({"email_provider": "duckmail", "duckmail_api_base": "https://duck.test"}, session=session)

        mailbox = service.provision()
        # The randomized mailbox address is the recipient returned by a real provider.
        session.request.side_effect = [
            Response({"hydra:member": [{"id": "message-1", "to": [{"address": mailbox.address}]}]}),
            Response({"subject": "Your code", "text": "Your verification code: 123456"}),
        ]

        self.assertTrue(mailbox.address.endswith("@mail.test"))
        self.assertEqual(service.wait_for_code(mailbox, timeout_sec=1), "123456")
        create_call = session.request.call_args_list[1]
        self.assertEqual(create_call.args, ("POST", "https://duck.test/accounts"))
        self.assertEqual(create_call.kwargs["json"]["expiresIn"], 0)

    def test_cloudflare_admin_provision_uses_configured_auth_and_domain(self):
        session = session_with({"address": "testuser@domain.test", "jwt": "address-jwt"})
        with patch("xai_oauth_bulk.mailbox._username", return_value="testuser"):
            service = MailboxService(
                {
                    "email_provider": "cloudflare",
                    "cloudflare_api_base": "https://cf.test",
                    "cloudflare_api_key": "secret",
                    "cloudflare_auth_mode": "x-admin-auth",
                    "cloudflare_path_accounts": "/admin/new_address",
                    "defaultDomains": "domain.test",
                },
                session=session,
            )
            mailbox = service.provision()

        self.assertEqual(mailbox, Mailbox("cloudflare", "testuser@domain.test", "address-jwt"))
        call = session.request.call_args
        self.assertEqual(call.args, ("POST", "https://cf.test/admin/new_address"))
        self.assertEqual(call.kwargs["json"], {"name": "testuser", "enablePrefix": True, "domain": "domain.test"})
        self.assertEqual(call.kwargs["headers"]["x-admin-auth"], "secret")

    def test_yyds_provisions_with_preferred_domain(self):
        session = session_with(
            {"success": True, "data": [{"domain": "other.test", "isVerified": True, "isPublic": True}, {"domain": "wanted.test", "isVerified": True, "isPublic": True}]},
            {"success": True, "data": {"address": "testuser@wanted.test", "token": "yyds-token"}},
        )
        with patch("xai_oauth_bulk.mailbox._username", return_value="testuser"):
            service = MailboxService({"email_provider": "yyds", "yyds_api_base": "https://yyds.test", "yyds_api_key": "key", "yyds_preferred_domains": "wanted.test"}, session=session)
            mailbox = service.provision()

        self.assertEqual(mailbox, Mailbox("yyds", "testuser@wanted.test", "yyds-token"))
        self.assertEqual(session.request.call_args_list[1].kwargs["json"], {"address": "testuser", "domain": "wanted.test"})

    def test_polling_is_bounded_when_no_message_arrives(self):
        session = session_with({"hydra:member": []}, {"hydra:member": []})
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        service = MailboxService(MailboxConfig(poll_timeout_sec=2, poll_interval_sec=1), session=session, clock=clock, sleep=sleep)
        with self.assertRaises(VerificationCodeTimeout):
            service.wait_for_code(Mailbox("duckmail", "a@test", "token"))

    def test_domain_is_blocked_matches_exact_and_subdomain_suffix(self):
        blocked = ("dpdns.org", "spam.test")
        self.assertTrue(domain_is_blocked("dpdns.org", blocked))
        self.assertTrue(domain_is_blocked("xx.lucky04.dpdns.org", blocked))
        self.assertTrue(domain_is_blocked("SPAM.TEST", blocked))
        self.assertFalse(domain_is_blocked("example.com", blocked))
        self.assertFalse(domain_is_blocked("notdpdns.org", blocked))
        self.assertEqual(email_domain("v720f8y9y5@xx.lucky04.dpdns.org"), "xx.lucky04.dpdns.org")

    def test_load_blocked_domains_file_ignores_comments_and_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blocked.txt"
            path.write_text(
                "# comment\n\ndpdns.org  # note\n@Bad.Example\n.dpdns.org\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_blocked_domains_file(path),
                ("dpdns.org", "bad.example"),
            )
            self.assertEqual(load_blocked_domains_file(Path(temp_dir) / "missing.txt"), ())

    def test_config_merges_blocked_domains_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blocked.txt"
            path.write_text("dpdns.org\n", encoding="utf-8")
            config = MailboxConfig.from_mapping(
                {
                    "mailbox_provider": "duckmail",
                    "mailbox_blocked_domains": "extra.test",
                    "mailbox_blocked_domains_file": str(path),
                }
            )
        self.assertEqual(config.blocked_domains, ("extra.test", "dpdns.org"))

    def test_provision_refetches_when_address_domain_is_blocked(self):
        session = Mock()
        service = MailboxService(
            MailboxConfig(
                provider="duckmail",
                blocked_domains=("dpdns.org",),
                domain_filter_max_attempts=5,
            ),
            session=session,
        )
        service.provider = Mock()
        service.provider.provision.side_effect = [
            Mailbox("duckmail", "v720f8y9y5@xx.lucky04.dpdns.org", "t1"),
            Mailbox("duckmail", "ok@mail.test", "t2"),
        ]
        logs: list[str] = []

        mailbox = service.provision(log=logs.append)

        self.assertEqual(mailbox, Mailbox("duckmail", "ok@mail.test", "t2"))
        self.assertEqual(service.provider.provision.call_count, 2)
        self.assertTrue(any("domain filtered" in line for line in logs))

    def test_provision_raises_when_all_addresses_filtered(self):
        session = Mock()
        service = MailboxService(
            MailboxConfig(
                provider="duckmail",
                blocked_domains=("dpdns.org",),
                domain_filter_max_attempts=2,
            ),
            session=session,
        )
        service.provider = Mock()
        service.provider.provision.return_value = Mailbox(
            "duckmail", "a@xx.lucky04.dpdns.org", "t"
        )
        with self.assertRaisesRegex(MailboxError, "domain filter rejected"):
            service.provision()

    def test_duckmail_skips_blocked_domains_before_create(self):
        session = session_with(
            {
                "hydra:member": [
                    {"domain": "xx.lucky04.dpdns.org", "isVerified": True},
                    {"domain": "mail.test", "isVerified": True},
                ]
            },
            {},
            {"token": "mail-token"},
        )
        service = MailboxService(
            {
                "email_provider": "duckmail",
                "duckmail_api_base": "https://duck.test",
                "mailbox_blocked_domains": "dpdns.org",
            },
            session=session,
        )
        mailbox = service.provision()
        self.assertTrue(mailbox.address.endswith("@mail.test"))
        create_call = session.request.call_args_list[1]
        self.assertTrue(create_call.kwargs["json"]["address"].endswith("@mail.test"))


if __name__ == "__main__":
    unittest.main()
