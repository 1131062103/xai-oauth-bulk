from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xai_oauth_bulk.account_ledger import append_account_ledger
from xai_oauth_bulk.config import Config, validate_config
from xai_oauth_bulk.registration import build_registration_profile
from xai_oauth_bulk.runner import JobResult, run_batch


class RegistrationConfigTests(unittest.TestCase):
    def test_registration_requires_explicit_enablement(self) -> None:
        cfg = Config(account_source="register", register_count=1, mailbox_provider="duckmail")
        with self.assertRaisesRegex(ValueError, "registration_enabled"):
            validate_config(cfg)

    def test_registration_rejects_parallel_workers(self) -> None:
        cfg = Config(
            account_source="register",
            registration_enabled=True,
            register_count=1,
            mailbox_provider="duckmail",
            workers=2,
        )
        with self.assertRaisesRegex(ValueError, "workers"):
            validate_config(cfg)

    def test_registration_accepts_serial_authorized_configuration(self) -> None:
        cfg = Config(
            account_source="register",
            registration_enabled=True,
            register_count=1,
            mailbox_provider="cloudflare",
        )
        validate_config(cfg)


class RegistrationProfileAndLedgerTests(unittest.TestCase):
    def test_profile_password_has_required_character_classes(self) -> None:
        profile = build_registration_profile(8)
        self.assertGreaterEqual(len(profile.password), 16)
        self.assertTrue(any(char.isupper() for char in profile.password))
        self.assertTrue(any(char.islower() for char in profile.password))
        self.assertTrue(any(char.isdigit() for char in profile.password))
        self.assertTrue(any(char in "!@#$%^&*_-" for char in profile.password))

    def test_ledger_retains_records_without_mailbox_or_oauth_secrets(self) -> None:
        profile = build_registration_profile()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "accounts.jsonl"
            append_account_ledger(
                path,
                email="owner@example.test",
                profile=profile,
                mode="standalone",
                oauth_path="/tmp/xai-owner.json",
                status="oauth_ok",
            )
            append_account_ledger(
                path,
                email="owner-two@example.test",
                profile=profile,
                mode="api",
                oauth_path="CPA:xai-owner-two.json",
                status="registered",
            )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([record["email"] for record in records], ["owner@example.test", "owner-two@example.test"])
        self.assertEqual(records[0]["status"], "oauth_ok")
        self.assertEqual(records[1]["status"], "registered")
        self.assertNotIn("mailbox_token", records[0])
        self.assertNotIn("verification_code", records[0])
        self.assertNotIn("access_token", records[0])

    def test_save_registered_account_writes_jsonl_and_email_password_txt(self) -> None:
        from xai_oauth_bulk.account_ledger import save_registered_account

        profile = build_registration_profile()
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "accounts.jsonl"
            jsonl, txt = save_registered_account(
                ledger_path=ledger,
                email="owner@example.test",
                profile=profile,
                mode="api",
                status="registered",
            )
            self.assertTrue(jsonl.is_file())
            self.assertTrue(txt.is_file())
            record = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["email"], "owner@example.test")
            self.assertEqual(record["password"], profile.password)
            self.assertEqual(record["status"], "registered")
            self.assertEqual(txt.read_text(encoding="utf-8").strip(), f"owner@example.test:{profile.password}")
            # Second save for same email does not duplicate the txt line.
            save_registered_account(
                ledger_path=ledger,
                email="owner@example.test",
                profile=profile,
                mode="api",
                status="oauth_ok",
                oauth_path="CPA:xai-owner.json",
            )
            self.assertEqual(txt.read_text(encoding="utf-8").count("owner@example.test:"), 1)
            statuses = [json.loads(line)["status"] for line in jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(statuses, ["registered", "oauth_ok"])


class RegistrationRunnerTests(unittest.TestCase):
    def test_register_source_bypasses_accounts_and_cpa_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = Config(
                account_source="register",
                registration_enabled=True,
                register_count=2,
                mailbox_provider="duckmail",
                mode="standalone",
                fail_log=str(Path(temp_dir) / "failed.jsonl"),
                sleep_between_sec=0,
            )
            with (
                patch("xai_oauth_bulk.runner.parse_accounts_file") as parse_accounts,
                patch("xai_oauth_bulk.runner.CPAClient") as cpa_client,
                patch("xai_oauth_bulk.runner.run_one_registered_standalone") as register_one,
            ):
                register_one.side_effect = [
                    JobResult(email="one@example.test", ok=True, mode="standalone"),
                    JobResult(email="two@example.test", ok=True, mode="standalone"),
                ]
                results = run_batch(cfg, log=lambda _: None)

        parse_accounts.assert_not_called()
        cpa_client.assert_not_called()
        self.assertEqual(register_one.call_count, 2)
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
