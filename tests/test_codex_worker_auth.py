import unittest
from unittest.mock import patch

import api


class CodexWorkerAuthTests(unittest.TestCase):
    def test_demo_mode_does_not_block_enabled_worker_token(self):
        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "CODEX_WORKER_ENABLED", True),
            patch.object(api, "CODEX_WORKER_TOKEN", "worker-secret"),
            patch.object(api, "REGISTRATION_CODE", ""),
            patch.object(api.os, "getenv", return_value=""),
        ):
            self.assertTrue(api.codex_worker_token_valid("worker-secret"))

    def test_worker_can_be_disabled_even_with_valid_token(self):
        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "CODEX_WORKER_ENABLED", False),
            patch.object(api, "CODEX_WORKER_TOKEN", "worker-secret"),
            patch.object(api, "REGISTRATION_CODE", ""),
            patch.object(api.os, "getenv", return_value=""),
        ):
            self.assertFalse(api.codex_worker_token_valid("worker-secret"))

    def test_wrong_worker_token_is_rejected(self):
        with (
            patch.object(api, "CODEX_WORKER_ENABLED", True),
            patch.object(api, "CODEX_WORKER_TOKEN", "worker-secret"),
            patch.object(api, "REGISTRATION_CODE", ""),
            patch.object(api.os, "getenv", return_value=""),
        ):
            self.assertFalse(api.codex_worker_token_valid("wrong-secret"))


if __name__ == "__main__":
    unittest.main()
