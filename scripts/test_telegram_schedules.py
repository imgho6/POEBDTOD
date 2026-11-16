"""Regression tests for half-hour Telegram forecast slot selection."""

import importlib.util
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name("telegram-schedules.py")
SPEC = importlib.util.spec_from_file_location("telegram_schedules", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

KYIV = ZoneInfo("Europe/Kyiv")


class StatusTargetTests(unittest.TestCase):
    def at(self, hour, minute, second=0, day=15):
        return datetime(2026, 11, day, hour, minute, second, tzinfo=KYIV)

    def test_catches_up_next_slot_after_publication_boundary(self):
        target = MODULE.select_status_target(self.at(11, 2), "2026-11-15:21")
        self.assertEqual(target, ("2026-11-15", 23, "2026-11-15:23", self.at(11, 30)))

    def test_late_run_targets_interval_after_publication_boundary(self):
        target = MODULE.select_status_target(self.at(11, 14), "2026-11-15:21")
        self.assertEqual(target, ("2026-11-15", 23, "2026-11-15:23", self.at(11, 30)))

    def test_2100_publication_targets_2130_to_2200(self):
        target = MODULE.select_status_target(self.at(21, 0), "2026-11-15:41")
        self.assertEqual(target, ("2026-11-15", 43, "2026-11-15:43", self.at(21, 30)))

    def test_does_not_duplicate_current_slot(self):
        self.assertIsNone(MODULE.select_status_target(self.at(11, 2), "2026-11-15:23"))

    def test_does_not_duplicate_on_quarter_hour_run(self):
        self.assertIsNone(MODULE.select_status_target(self.at(11, 15), "2026-11-15:23"))

    def test_does_not_publish_older_slot_after_newer_one(self):
        self.assertIsNone(MODULE.select_status_target(self.at(11, 2), "2026-11-15:24"))

    def test_boundary_run_selects_new_slot(self):
        target = MODULE.select_status_target(self.at(11, 30), "2026-11-15:22")
        self.assertEqual(target, ("2026-11-15", 24, "2026-11-15:24", self.at(12, 0)))

    def test_midnight_rollover_uses_next_date(self):
        target = MODULE.select_status_target(self.at(0, 0, day=16), "2026-11-15:47")
        self.assertEqual(target, ("2026-11-16", 1, "2026-11-16:1", self.at(0, 30, day=16)))

    def test_send_status_message_logs_without_undefined_boundary(self):
        now = self.at(11, 0)
        schedule = {"date": "2026-11-15", "queues": {"1.1": ["on"] * 48}}
        state = {"statusMessages": {"lastKey": "2026-11-15:22"}}
        with (
            patch.object(MODULE, "TOKEN", "test-token"),
            patch.object(MODULE, "CHANNEL_ID", "-100"),
            patch.object(MODULE, "telegram_request", return_value={"result": {"message_id": 123}}),
            patch.object(MODULE, "save_state"),
        ):
            MODULE.send_status_message(schedule, state, now)
        self.assertEqual(state["statusMessages"]["lastKey"], "2026-11-15:23")
        self.assertEqual(state["statusMessageIds"][-1]["message_id"], 123)

    def test_vercel_delay_is_compensated_and_waits_for_boundary(self):
        now = self.at(11, 27)
        schedule = {"date": "2026-11-15", "queues": {"1.1": ["on"] * 48}}
        state = {"statusMessages": {"lastKey": "2026-11-15:22"}}
        with (
            patch.dict(MODULE.os.environ, {"TELEGRAM_BOUNDARY_ADVANCE_SECONDS": "900"}),
            patch.object(MODULE, "TOKEN", "test-token"),
            patch.object(MODULE, "CHANNEL_ID", "-100"),
            patch.object(MODULE, "telegram_request", return_value={"result": {"message_id": 124}}),
            patch.object(MODULE, "save_state"),
            patch.object(MODULE.time, "sleep") as sleep,
        ):
            MODULE.send_status_message(schedule, state, now)
        sleep.assert_called_once_with(180.0)
        self.assertEqual(state["statusMessages"]["lastKey"], "2026-11-15:24")
        self.assertEqual(state["statusMessageIds"][-1]["message_id"], 124)

    def test_cleanup_initializes_two_hour_cycle_without_deleting(self):
        now = self.at(12, 0)
        state = {
            "statusMessageIds": [
                {"message_id": 1, "sentAt": self.at(8, 0).isoformat()}
            ]
        }
        with (
            patch.object(MODULE, "save_state"),
            patch.object(MODULE, "telegram_request") as telegram,
        ):
            MODULE.cleanup_status_messages(state, now)
        telegram.assert_not_called()
        self.assertEqual(state["statusMessageIds"][0]["message_id"], 1)
        self.assertEqual(state["statusCleanup"]["lastRunAt"], now.isoformat())

    def test_cleanup_waits_until_two_hours_after_last_cleanup(self):
        now = self.at(12, 0)
        state = {
            "statusCleanup": {"lastRunAt": self.at(10, 1).isoformat()},
            "statusMessageIds": [
                {"message_id": 1, "sentAt": self.at(8, 0).isoformat()}
            ],
        }
        with (
            patch.object(MODULE, "save_state"),
            patch.object(MODULE, "telegram_request") as telegram,
        ):
            MODULE.cleanup_status_messages(state, now)
        telegram.assert_not_called()
        self.assertEqual(len(state["statusMessageIds"]), 1)

    def test_cleanup_deletes_only_messages_at_least_two_hours_old(self):
        now = self.at(12, 0)
        state = {
            "statusCleanup": {"lastRunAt": self.at(10, 0).isoformat()},
            "statusMessageIds": [
                {"message_id": 1, "sentAt": self.at(9, 30).isoformat()},
                {"message_id": 2, "sentAt": self.at(10, 30).isoformat()},
            ],
        }
        with (
            patch.object(MODULE, "CHANNEL_ID", "-100"),
            patch.object(MODULE, "TOKEN", "test-token"),
            patch.object(MODULE, "save_state"),
            patch.object(MODULE, "telegram_request", return_value={"ok": True}) as telegram,
        ):
            MODULE.cleanup_status_messages(state, now)
        self.assertEqual(telegram.call_count, 1)
        self.assertIn('"message_id": 1', telegram.call_args.args[1].decode("utf-8"))
        self.assertEqual([item["message_id"] for item in state["statusMessageIds"]], [2])
        self.assertEqual(state["statusCleanup"]["lastRunAt"], now.isoformat())

    def test_cleanup_keeps_messages_with_unknown_timestamp(self):
        now = self.at(12, 0)
        state = {
            "statusCleanup": {"lastRunAt": self.at(10, 0).isoformat()},
            "statusMessageIds": [{"message_id": 1}],
        }
        with (
            patch.object(MODULE, "save_state"),
            patch.object(MODULE, "telegram_request") as telegram,
        ):
            MODULE.cleanup_status_messages(state, now)
        telegram.assert_not_called()
        self.assertEqual(len(state["statusMessageIds"]), 1)

    def test_vercel_dispatch_advance_selects_next_half_hour_boundary(self):
        self.assertEqual(
            MODULE.forecast_boundary_for_dispatch(self.at(11, 27), 900),
            self.at(11, 30),
        )
        self.assertEqual(
            MODULE.forecast_boundary_for_dispatch(self.at(11, 57), 900),
            self.at(12, 0),
        )

if __name__ == "__main__":
    unittest.main()
