from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xai_oauth_bulk.accounts import Account
from xai_oauth_bulk.config import Config
from xai_oauth_bulk.cpa_client import CPAClient, CPAClientError
from xai_oauth_bulk.oauth_device import OAuthDeviceError, poll_device_token
from xai_oauth_bulk.runner import JobResult, run_batch
from xai_oauth_bulk.schema import credential_file_name


class CPAClientInventoryTests(unittest.TestCase):
    def test_list_xai_auth_file_names_filters_by_provider(self) -> None:
        client = CPAClient("http://cpa.example", "key")
        client._get = MagicMock(
            return_value={
                "files": [
                    {"name": "xai-a@example.com.json", "type": "xai"},
                    {"name": "xai-b@example.com.json", "provider": "XAI"},
                    {"name": "openai-a@example.com.json", "type": "openai"},
                    {"type": "xai"},
                    "invalid",
                ]
            }
        )

        self.assertEqual(
            client.list_xai_auth_file_names(),
            {"xai-a@example.com.json", "xai-b@example.com.json"},
        )

    def test_list_xai_auth_file_names_rejects_malformed_response(self) -> None:
        client = CPAClient("http://cpa.example", "key")
        client._get = MagicMock(return_value={"files": {}})

        with self.assertRaises(CPAClientError):
            client.list_xai_auth_file_names()


class BatchPrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accounts = [
            Account(email="known@example.com", password="password"),
            Account(email="new@example.com", password="password"),
        ]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cfg = Config(
            mode="api",
            cpa_base_url="http://cpa.example",
            cpa_management_key="key",
            accounts_file="accounts.txt",
            fail_log=str(Path(self.temp_dir.name) / "failed.jsonl"),
            skip_existing=True,
            sleep_between_sec=0,
        )

    @patch("xai_oauth_bulk.runner.run_one_api")
    @patch("xai_oauth_bulk.runner.CPAClient")
    @patch("xai_oauth_bulk.runner.parse_accounts_file")
    def test_precheck_once_skips_exact_cpa_filename(
        self,
        parse_accounts: MagicMock,
        client_class: MagicMock,
        run_one_api: MagicMock,
    ) -> None:
        parse_accounts.return_value = self.accounts
        client_class.return_value.list_xai_auth_file_names.return_value = {
            credential_file_name("known@example.com")
        }
        run_one_api.return_value = JobResult(email="new@example.com", ok=True, mode="api")

        results = run_batch(self.cfg, log=lambda _: None)

        client_class.return_value.list_xai_auth_file_names.assert_called_once_with()
        run_one_api.assert_called_once()
        self.assertEqual(run_one_api.call_args.args[0].email, "new@example.com")
        self.assertEqual(len(results), 2)
        skipped = next(result for result in results if result.skipped)
        self.assertEqual(skipped.email, "known@example.com")
        self.assertEqual(skipped.path, "CPA:xai-known@example.com.json")

    @patch("xai_oauth_bulk.runner.run_one_api")
    @patch("xai_oauth_bulk.runner.CPAClient")
    @patch("xai_oauth_bulk.runner.parse_accounts_file")
    def test_precheck_failure_prevents_all_oauth_work(
        self,
        parse_accounts: MagicMock,
        client_class: MagicMock,
        run_one_api: MagicMock,
    ) -> None:
        parse_accounts.return_value = self.accounts
        client_class.return_value.list_xai_auth_file_names.side_effect = CPAClientError("forbidden")

        results = run_batch(self.cfg, log=lambda _: None)

        run_one_api.assert_not_called()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result.ok for result in results))
        self.assertTrue(Path(self.cfg.fail_log).is_file())
        self.assertEqual(len(Path(self.cfg.fail_log).read_text(encoding="utf-8").splitlines()), 2)

    @patch("xai_oauth_bulk.runner.run_one_api")
    @patch("xai_oauth_bulk.runner.CPAClient")
    @patch("xai_oauth_bulk.runner.parse_accounts_file")
    def test_local_api_marker_does_not_skip_when_cpa_file_is_absent(
        self,
        parse_accounts: MagicMock,
        client_class: MagicMock,
        run_one_api: MagicMock,
    ) -> None:
        parse_accounts.return_value = self.accounts[:1]
        self.cfg.out_dir = self.temp_dir.name
        Path(self.cfg.out_dir, credential_file_name("known@example.com")).write_text(
            "local marker", encoding="utf-8"
        )
        client_class.return_value.list_xai_auth_file_names.return_value = set()
        run_one_api.return_value = JobResult(email="known@example.com", ok=True, mode="api")

        run_batch(self.cfg, log=lambda _: None)

        run_one_api.assert_called_once()

    @patch("xai_oauth_bulk.runner.run_one_api")
    @patch("xai_oauth_bulk.runner.CPAClient")
    @patch("xai_oauth_bulk.runner.parse_accounts_file")
    def test_no_skip_existing_bypasses_cpa_precheck(
        self,
        parse_accounts: MagicMock,
        client_class: MagicMock,
        run_one_api: MagicMock,
    ) -> None:
        parse_accounts.return_value = self.accounts
        self.cfg.skip_existing = False
        run_one_api.side_effect = [
            JobResult(email=account.email, ok=True, mode="api") for account in self.accounts
        ]

        results = run_batch(self.cfg, log=lambda _: None)

        client_class.assert_not_called()
        self.assertEqual(run_one_api.call_count, 2)
        self.assertEqual(len(results), 2)

    @patch("xai_oauth_bulk.runner.run_one_standalone")
    @patch("xai_oauth_bulk.runner.CPAClient")
    @patch("xai_oauth_bulk.runner.parse_accounts_file")
    def test_standalone_never_uses_cpa_precheck(
        self,
        parse_accounts: MagicMock,
        client_class: MagicMock,
        run_one_standalone: MagicMock,
    ) -> None:
        parse_accounts.return_value = self.accounts[:1]
        self.cfg.mode = "standalone"
        run_one_standalone.return_value = JobResult(
            email="known@example.com", ok=True, mode="standalone"
        )

        run_batch(self.cfg, log=lambda _: None)

        client_class.assert_not_called()
        run_one_standalone.assert_called_once()


class OAuthPollLoggingTests(unittest.TestCase):
    @patch("xai_oauth_bulk.oauth_device.time.sleep")
    @patch("xai_oauth_bulk.oauth_device._session")
    def test_authorization_pending_logs_wait_once(
        self, session_factory: MagicMock, sleep: MagicMock
    ) -> None:
        response = MagicMock(status_code=400)
        response.json.return_value = {"error": "authorization_pending"}
        session_factory.return_value.post.return_value = response
        messages: list[str] = []
        checks = 0

        def cancel() -> bool:
            nonlocal checks
            checks += 1
            return checks > 1

        with self.assertRaisesRegex(OAuthDeviceError, "cancelled"):
            poll_device_token("device-code", log=messages.append, cancel=cancel)

        self.assertEqual(messages, ["waiting for browser authorization"])
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
