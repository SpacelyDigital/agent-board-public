"""Quota-only local exports. All files and observations below are disposable."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_quota as quota

NOW = 1793872800


def envelope(*, at=NOW * 1000, windows=None, value=None):
    if value is None:
        value = {"provider": "claude", "observed_at_ms": at, "windows": windows if windows is not None else [
            {"id": "five_hour", "used_basis_points": 2350, "reset_at": NOW + 3600},
            {"id": "weekly", "used_basis_points": 4100, "reset_at": NOW + 6 * 86400}]}
    payload = json.dumps(value, separators=(",", ":"))
    return json.dumps({"version": 1, "payload": payload, "sha256": hashlib.sha256(payload.encode()).hexdigest()}).encode()


class LocalQuotaTests(unittest.TestCase):
    def test_codex_uses_duration_not_position_and_local_record_time(self):
        raw = {"primary": {"used_percent": 42.25, "window_minutes": 10080, "resets_at": NOW + 86400},
               "secondary": {"used_percent": 99, "window_minutes": 300, "resets_at": NOW + 3600},
               "credits": {"balance": "DO NOT COPY"}, "plan_type": "DO NOT COPY"}
        timestamp = datetime.fromtimestamp(NOW - 300, timezone.utc).isoformat()
        result = quota.parse_codex_observation(raw, timestamp, now=NOW)
        self.assertEqual(result["updated_at"], NOW - 300)
        self.assertTrue(result["stale"])
        self.assertEqual(result["windows"][0]["remaining_percent"], 57.75)
        self.assertNotIn("DO NOT COPY", json.dumps(result))

    def test_codex_rejects_malformed_or_model_windows(self):
        base = {"secondary": {"used_percent": 20, "window_minutes": 10080, "resets_at": NOW + 86400}}
        self.assertIsNotNone(quota.parse_codex_observation(base, NOW, now=NOW))
        variants = []
        for field, values in (("used_percent", [True, float("nan"), float("inf"), -1, 101, "20"]),
                              ("window_minutes", [True, 300, "10080", 10080.0]),
                              ("resets_at", [True, float("nan"), NOW + 0.5, NOW + 20 * 86400])):
            for value in values:
                item = json.loads(json.dumps(base))
                item["secondary"][field] = value
                variants.append(item)
        variants.extend([{**base, "limit_id": item} for item in ("model-bucket", "", True, [], {})])
        for item in variants:
            with self.subTest(raw=item):
                self.assertIsNone(quota.parse_codex_observation(item, NOW, now=NOW))
        self.assertIsNone(quota.parse_codex_observation(base, NOW + 61, now=NOW))
        self.assertIsNone(quota.parse_codex_observation(base, "2026-11-05T10:00:00", now=NOW))

    def test_codex_conflicting_weekly_slots_are_not_guessed(self):
        raw = {"primary": {"used_percent": 20, "window_minutes": 10080, "resets_at": NOW + 86400},
               "secondary": {"used_percent": 30, "window_minutes": 10080, "resets_at": NOW + 86400}}
        self.assertIsNone(quota.parse_codex_observation(raw, NOW, now=NOW))

    def test_expired_codex_window_is_never_inferred_reset(self):
        raw = {"secondary": {"used_percent": 100, "window_minutes": 10080, "resets_at": NOW - 1}}
        result = quota.parse_codex_observation(raw, NOW - 60, now=NOW)
        self.assertTrue(result["stale"])
        self.assertEqual(result["windows"][0]["remaining_percent"], 0)

    def test_claude_canonical_payload_and_precision(self):
        result = quota.parse_claude_export(envelope(at=NOW * 1000 + 123), now=NOW + 1)
        self.assertEqual(result["updated_at"], NOW + 0.123)
        self.assertEqual([item["remaining_percent"] for item in result["windows"]], [76.5, 59])
        self.assertFalse(result["stale"])
        self.assertTrue(quota.parse_claude_export(envelope(), now=NOW + 121)["stale"])

    def test_claude_optional_reset_retains_percentage_and_normal_staleness(self):
        raw = envelope(windows=[
            {"id": "five_hour", "used_basis_points": 6800, "reset_at": None},
            {"id": "weekly", "used_basis_points": 2500, "reset_at": NOW + 3600}])
        result = quota.parse_claude_export(raw, now=NOW)
        self.assertTrue(result["available"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["windows"][0]["remaining_percent"], 32)
        self.assertIsNone(result["windows"][0]["reset_at"])
        self.assertEqual(result["windows"][1]["reset_at"], NOW + 3600)
        self.assertTrue(quota.parse_claude_export(raw, now=NOW + 121)["stale"])
        self.assertTrue(quota.parse_claude_export(raw, now=NOW + 3601)["stale"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = root / quota.CLAUDE_EXPORT_RELATIVE
            export.mkdir(parents=True)
            (export / quota.CLAUDE_EXPORT_SLOTS[0]).write_bytes(raw)
            reader = quota.ClaudeLocalQuota(root)
            self.assertEqual(reader.snapshot(NOW)["windows"], result["windows"])
            self.assertTrue(reader.snapshot(NOW + 121)["stale"])

    def test_valid_empty_export_clears_subscription_availability(self):
        result = quota.parse_claude_export(envelope(windows=[]), now=NOW)
        self.assertFalse(result["available"])
        self.assertEqual(result["windows"], [])

    def test_every_truncated_byte_is_rejected(self):
        raw = envelope()
        for position in range(len(raw)):
            with self.subTest(position=position):
                self.assertIsNone(quota.parse_claude_export(raw[:position], now=NOW))

    def test_modified_content_and_noncanonical_payloads_are_rejected(self):
        raw = envelope()
        for index in range(len(raw)):
            altered = raw[:index] + bytes([raw[index] ^ 1]) + raw[index + 1:]
            if altered != raw and altered.strip() != raw.strip():
                self.assertIsNone(quota.parse_claude_export(altered, now=NOW))
        original = json.loads(envelope())
        original["payload"] = original["payload"].replace('","', '", "')
        original["sha256"] = hashlib.sha256(original["payload"].encode()).hexdigest()
        self.assertIsNone(quota.parse_claude_export(json.dumps(original).encode(), now=NOW))

    def test_untrusted_field_shapes_types_and_bounds_are_rejected(self):
        base = {"provider": "claude", "observed_at_ms": NOW * 1000,
                "windows": [{"id": "weekly", "used_basis_points": 5000, "reset_at": NOW + 86400}]}
        values = []
        for field, choices in (("provider", ["codex", True, {"credential": "fake"}]),
                               ("observed_at_ms", [True, NOW * 1000.0, (NOW + 61) * 1000, -1, 10**300]),
                               ("windows", [None, {}, "fake", [base["windows"][0]] * 3])):
            values.extend([{**base, field: choice} for choice in choices])
        values.append({**base, "chat": "fixture-private-message"})
        for field, choices in (("id", ["seven_day", "spend_limit", True]),
                               ("used_basis_points", [True, 10.5, -1, 10001, float("inf")]),
                               ("reset_at", [True, NOW + 0.5, NOW + 20 * 86400, str(NOW), "", [], {}])):
            for choice in choices:
                item = {**base["windows"][0], field: choice}
                values.append({**base, "windows": [item]})
        values.append({**base, "windows": [base["windows"][0], base["windows"][0]]})
        values.append({**base, "windows": [{**base["windows"][0], "token": "fake-secret"}]})
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(quota.parse_claude_export(envelope(value=value), now=NOW))

    def test_duplicate_json_nonfinite_depth_and_oversize_are_rejected(self):
        raw = envelope()
        self.assertIsNone(quota.parse_claude_export(raw.replace(b'"version": 1', b'"version": 1, "version": 1'), now=NOW))
        self.assertIsNone(quota.parse_claude_export(b'{"version":NaN}', now=NOW))
        self.assertIsNone(quota.parse_claude_export(b'[' * 4000 + b']' * 4000, now=NOW))
        self.assertIsNone(quota.parse_claude_export(b'x' * (quota.MAX_EXPORT_BYTES + 1), now=NOW))
        self.assertIsNone(quota.parse_claude_export(b'\xff', now=NOW))

    def test_reader_chooses_same_second_newer_slot_and_retains_failed_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = root / quota.CLAUDE_EXPORT_RELATIVE
            export.mkdir(parents=True)
            a, b = (export / name for name in quota.CLAUDE_EXPORT_SLOTS)
            a.write_bytes(envelope(at=NOW * 1000))
            b.write_bytes(envelope(at=NOW * 1000 + 1, windows=[
                {"id": "weekly", "used_basis_points": 7500, "reset_at": NOW + 86400}]))
            reader = quota.ClaudeLocalQuota(root)
            result = reader.snapshot(NOW)
            self.assertEqual(result["updated_at"], NOW + 0.001)
            self.assertEqual(result["windows"][0]["remaining_percent"], 25)
            b.write_bytes(b'{"version":1,"payload":')
            one_failed = reader.snapshot(NOW + 1)
            self.assertTrue(one_failed["stale"])
            self.assertEqual(one_failed["updated_at"], result["updated_at"])
            self.assertEqual(one_failed["windows"][0]["remaining_percent"], 25)
            a.write_bytes(b'')
            failed = reader.snapshot(NOW + 30)
            self.assertTrue(failed["stale"])
            self.assertEqual(failed["updated_at"], result["updated_at"])
            b.write_bytes(envelope(at=(NOW + 31) * 1000, windows=[]))
            self.assertFalse(reader.snapshot(NOW + 31)["available"])

    def test_missing_previously_seen_slot_and_rollback_do_not_refresh_old_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = root / quota.CLAUDE_EXPORT_RELATIVE
            export.mkdir(parents=True)
            a, b = (export / name for name in quota.CLAUDE_EXPORT_SLOTS)
            a.write_bytes(envelope())
            reader = quota.ClaudeLocalQuota(root)
            self.assertFalse(reader.snapshot(NOW)["stale"])
            b.write_bytes(envelope(at=(NOW + 1) * 1000))
            self.assertFalse(reader.snapshot(NOW + 1)["stale"])
            b.unlink()
            result = reader.snapshot(NOW + 2)
            self.assertTrue(result["stale"])
            self.assertEqual(result["updated_at"], NOW + 1)

    def test_reader_never_follows_directory_file_links_fifos_or_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, outside = Path(temporary) / "root", Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            secret = outside / "secret"
            secret.write_bytes(envelope())
            (root / "agent-board").symlink_to(outside, target_is_directory=True)
            with patch("local_quota.os.read", side_effect=AssertionError("No linked source content may be read")):
                self.assertFalse(quota.ClaudeLocalQuota(root).snapshot(NOW)["available"])
            (root / "agent-board").unlink()
            export = root / quota.CLAUDE_EXPORT_RELATIVE
            export.mkdir(parents=True)
            a = export / quota.CLAUDE_EXPORT_SLOTS[0]
            a.symlink_to(secret)
            with patch("local_quota.os.read", side_effect=AssertionError("No linked source content may be read")):
                self.assertFalse(quota.ClaudeLocalQuota(root).snapshot(NOW)["available"])
            a.unlink()
            os.link(secret, a)
            with patch("local_quota.os.read", side_effect=AssertionError("No hardlink content may be read")):
                self.assertFalse(quota.ClaudeLocalQuota(root).snapshot(NOW)["available"])
            a.unlink()
            os.mkfifo(a)
            self.assertFalse(quota.ClaudeLocalQuota(root).snapshot(NOW)["available"])

    def test_reader_has_no_account_or_network_dependency(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("socket.create_connection", side_effect=AssertionError("No connections")), \
                patch("subprocess.Popen", side_effect=AssertionError("No provider programs")):
            root = Path(temporary)
            export = root / quota.CLAUDE_EXPORT_RELATIVE
            export.mkdir(parents=True)
            (export / quota.CLAUDE_EXPORT_SLOTS[0]).write_bytes(envelope())
            (root / "credentials.json").write_text("not a quota export")
            self.assertTrue(quota.ClaudeLocalQuota(root).snapshot(NOW)["available"])

    def test_actual_mod_output_round_trips_through_the_file_reader(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is needed only for the isolated fake-host mod check")
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([node, str(Path(__file__).with_name("local_quota_mod_smoke.mjs")), temporary],
                                    check=True, text=True, capture_output=True, timeout=20)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "passed")
            self.assertGreaterEqual(report["writes"], 35)
            reader = quota.ClaudeLocalQuota(Path(temporary) / ".claude")
            reading = reader.snapshot(NOW + 10)
            self.assertTrue(reading["available"])
            self.assertEqual(reading["windows"][0]["remaining_percent"], 75)


if __name__ == "__main__":
    unittest.main()
