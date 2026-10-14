import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "HQ-Credentials-Manager.py"
spec = importlib.util.spec_from_file_location("credentials_manager", SCRIPT)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class UnattendedSessionTests(unittest.TestCase):
    def test_expired_session_fails_without_opening_login(self):
        with patch.object(credentials, "load_credentials", return_value={}), \
             patch.object(credentials, "validate_credentials",
                          side_effect=credentials.OdooAuthError("expired")), \
             patch.object(credentials, "login_via_browser") as login:
            with self.assertRaisesRegex(credentials.OdooAuthError,
                                        "Sign in manually"):
                credentials.ensure_session(open_browser=False, allow_login=False)
            login.assert_not_called()

    def test_valid_session_is_reused(self):
        record = {"username": "test", "uid": 1, "db": "test"}
        with patch.object(credentials, "load_credentials", return_value=record), \
             patch.object(credentials, "validate_credentials", return_value=record), \
             patch.object(credentials, "save_credentials") as save, \
             patch.object(credentials, "login_via_browser") as login:
            self.assertIs(credentials.ensure_session(allow_login=False), record)
            save.assert_called_once_with(record)
            login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
