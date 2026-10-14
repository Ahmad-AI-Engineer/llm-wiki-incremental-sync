import unittest
from datetime import datetime, timezone

from hq_state import parse_time


class ParseTimeTests(unittest.TestCase):
    def test_outline_utc_timestamp(self):
        self.assertEqual(
            parse_time("2026-09-30T19:42:26.891Z"),
            datetime(2026, 9, 30, 19, 42, 26, 891000, tzinfo=timezone.utc),
        )

    def test_existing_iso_timestamp_and_missing_value(self):
        self.assertEqual(parse_time("2026-09-30T19:42:26+00:00"),
                         datetime(2026, 9, 30, 19, 42, 26, tzinfo=timezone.utc))
        self.assertIsNone(parse_time(None))


if __name__ == "__main__":
    unittest.main()
