"""Usage parsing, refresh policy, credential scope and stdio protocol regressions."""
import base64
import ctypes
import json
import math
import os
import shlex
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usage_source import (UsageSource, UsageUnavailable, parse_codex_usage, parse_claude_usage,
                          _claude_token, _copy_keychain_item, fetch_codex_usage, fetch_claude_usage, _fetch_claude_in_process)
from privacy_network import ProviderRequestError


def write_python_fixture(executable, code):
    script = executable.with_suffix(".py")
    script.write_text(code)
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n')
    executable.chmod(0o700)


class UsageTests(unittest.TestCase):
    now = 1_800_000_000

    def codex(self, *, primary=None, secondary=None):
        return {"rateLimits": {"limitId": "codex", "primary": primary, "secondary": secondary}}

    def test_codex_weekly_can_be_primary_or_secondary(self):
        weekly = {"usedPercent": 95, "windowDurationMins": 10080, "resetsAt": self.now + 86400}
        short = {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": self.now + 3600}
        for response in (self.codex(primary=weekly), self.codex(primary=short, secondary=weekly)):
            self.assertEqual(parse_codex_usage(response), [{"id": "weekly", "label": "Weekly", "remaining_percent": 5.0, "reset_at": self.now + 86400}])

    def test_codex_explicit_bucket_wins_over_legacy_view(self):
        response = self.codex(primary={"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": self.now + 60})
        response["rateLimitsByLimitId"] = {"codex": {"limitId": "codex", "primary": {"usedPercent": 99, "windowDurationMins": 10080, "resetsAt": self.now + 70}}, "other": {"accountId": "must-not-escape", "credits": {"balance": "secret"}}}
        windows = parse_codex_usage(response)
        self.assertEqual(windows[0]["remaining_percent"], 1)
        self.assertNotIn("secret", json.dumps(windows))
        self.assertNotIn("must-not-escape", json.dumps(windows))

    def test_codex_wrong_bucket_or_missing_weekly_is_unavailable(self):
        for response in ({"rateLimitsByLimitId": {"other": {"primary": {"windowDurationMins": 10080}}}}, self.codex(primary={"usedPercent": 0, "windowDurationMins": 300, "resetsAt": self.now})):
            with self.assertRaises(UsageUnavailable):
                parse_codex_usage(response)

    def test_codex_credits_are_independent_of_subscription_windows_and_earned_resets(self):
        response = self.codex()
        response["rateLimitResetCredits"] = {"availableCount": 4}
        for balance, expected in (("0.000", 0), ("123.4567", 123.4567), ("-2.75", -2.75)):
            response["rateLimits"]["credits"] = {"balance": balance, "hasCredits": expected > 0, "unlimited": False,
                                                 "private": "do-not-export"}
            with self.subTest(balance=balance):
                self.assertEqual(parse_codex_usage(response, include_credits=True),
                                 {"windows": [], "credits": {"amount": expected, "unit": "credits"}})
        with self.assertRaises(UsageUnavailable):
            parse_codex_usage(response)
        response["rateLimitsByLimitId"] = {"codex": {"limitId": "codex"},
                                            "other": response["rateLimits"]}
        with self.assertRaises(UsageUnavailable):
            parse_codex_usage(response, include_credits=True)

    def test_codex_never_substitutes_zero_for_unreported_or_invalid_balance(self):
        response = self.codex(primary={"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": self.now + 100})
        base = {"balance": "123", "hasCredits": True, "unlimited": False}
        invalid = [None, {}, {**base, "unlimited": True}, {**base, "hasCredits": 1}]
        invalid.extend({**base, "balance": value} for value in (
            None, 0, "", "NaN", "inf", "12 dollars", "123.45678901234567890", "9007199254740992"))
        for snapshot in invalid:
            response["rateLimits"]["credits"] = snapshot
            with self.subTest(snapshot=snapshot):
                reading = parse_codex_usage(response, include_credits=True)
                self.assertNotIn("credits", reading)
                self.assertEqual(reading["windows"][0]["remaining_percent"], 0)

    def test_claude_real_zero_and_hundred_and_iso_resets(self):
        windows = parse_claude_usage({"five_hour": {"utilization": 0, "resets_at": "2027-01-15T12:00:00Z"}, "seven_day": {"utilization": 100, "resets_at": self.now}})
        self.assertEqual([row["remaining_percent"] for row in windows], [100, 0])
        self.assertEqual([row["id"] for row in windows], ["five_hour", "weekly"])
        self.assertIsInstance(windows[0]["reset_at"], float)

    def test_missing_or_invalid_data_never_becomes_fake_remaining(self):
        for percent in (None, True, "95", -1, 101, math.nan, math.inf):
            with self.assertRaises(UsageUnavailable):
                parse_claude_usage({"five_hour": {"utilization": percent, "resets_at": self.now}})
        for reset in (None, "2027-01-15T12:00:00", "bad", 0):
            with self.assertRaises(UsageUnavailable):
                parse_claude_usage({"five_hour": {"utilization": 20, "resets_at": reset}})
        self.assertEqual(len(parse_claude_usage({"five_hour": None, "seven_day": {"utilization": 7, "resets_at": self.now}})), 1)

    def test_refresh_is_at_most_once_per_minute_per_provider(self):
        calls = {"claude": 0, "codex": 0}
        def fetch(provider):
            def run():
                calls[provider] += 1
                return [{"id": "weekly", "label": "Weekly", "remaining_percent": 33, "reset_at": self.now + 1000}]
            return run
        source = UsageSource({provider: fetch(provider) for provider in calls})
        initial = source.scan(self.now)
        source.scan(self.now + 59)
        self.assertEqual(calls, {"claude": 1, "codex": 1})
        source.scan(self.now + 60)
        self.assertEqual(calls, {"claude": 2, "codex": 2})
        initial[0]["windows"][0]["remaining_percent"] = 999
        self.assertEqual(source.scan(self.now + 61)[0]["windows"][0]["remaining_percent"], 33)

    def test_claude_reports_explicitly_disabled_credits_without_inventing_balance(self):
        response = {"seven_day": {"utilization": 70, "resets_at": self.now + 3600},
                    "extra_usage": {"is_enabled": False, "user_disabled": True, "credits_ever_enabled": True,
                                    "monthly_limit": None, "used_credits": None},
                    "spend": {"enabled": False, "balance": None, "used": {"amount_minor": 0, "currency": "USD", "exponent": 2}}}
        reading = parse_claude_usage(response, include_credits=True)
        self.assertEqual(reading["credits"], {"status": "disabled", "unit": "USD"})
        self.assertEqual(reading["windows"][0]["remaining_percent"], 30)
        with patch("usage_source.read_process", return_value=SimpleNamespace(stdout=json.dumps(reading))):
            self.assertEqual(fetch_claude_usage(), reading)
        source = UsageSource({"claude": lambda: reading, "codex": lambda: []})
        self.assertEqual(source.scan(self.now)[0]["credits"],
                         {"status": "disabled", "unit": "USD", "observed_at": self.now, "stale": False})

    def test_claude_spending_cap_and_exhaustion_are_not_a_credit_balance_or_off_switch(self):
        for extra in ({"is_enabled": True, "user_disabled": False, "monthly_limit": 10000, "used_credits": 2500},
                      {"is_enabled": False, "user_disabled": False, "spend_limit_reached": True},
                      {"is_enabled": 0, "user_disabled": 1}):
            reading = parse_claude_usage({"seven_day": {"utilization": 70, "resets_at": self.now + 3600},
                                         "extra_usage": extra}, include_credits=True)
            self.assertNotIn("credits", reading)

    def test_failed_refresh_retains_last_success_explicitly_stale(self):
        attempts = 0
        def fetch():
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                raise UsageUnavailable("Provider is unavailable.")
            return [{"id": "weekly", "label": "Weekly", "remaining_percent": 7, "reset_at": self.now + 1000}]
        source = UsageSource({"claude": fetch, "codex": lambda: []})
        initial = source.scan(self.now)
        self.assertFalse(initial[0]["stale"])
        self.assertFalse(initial[1]["available"])
        self.assertEqual(initial[1]["windows"], [])
        failed = source.scan(self.now + 61)[0]
        self.assertTrue(failed["available"])
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["updated_at"], self.now)
        self.assertEqual(failed["windows"][0]["remaining_percent"], 7)
        self.assertEqual(failed["error"], "Provider is unavailable.")

    def test_unexpected_exception_is_sanitized(self):
        def fetch():
            raise RuntimeError("Bearer secret-token-from-provider")
        rows = UsageSource({"claude": fetch, "codex": fetch}).scan(self.now)
        self.assertNotIn("secret-token", json.dumps(rows))
        self.assertEqual(rows[0]["windows"], [])
        self.assertFalse(rows[0]["available"])

    def test_rate_limit_retry_after_survives_helper_and_backoff_recovers(self):
        with patch('usage_source._claude_token', return_value='fixture'), patch('usage_source.read_provider_json',
                side_effect=ProviderRequestError('http', 429, retry_after=900)):
            with self.assertRaises(UsageUnavailable) as raised:
                _fetch_claude_in_process(self.now)
        error = raised.exception
        helper_error = {'error': str(error), 'error_code': error.code, 'retry_after': error.retry_after}
        with patch('usage_source.read_process', return_value=SimpleNamespace(stdout=json.dumps(helper_error))):
            with self.assertRaises(UsageUnavailable) as raised:
                fetch_claude_usage()
        self.assertEqual(raised.exception.code, 'rate_limited')
        self.assertEqual(raised.exception.retry_after, 900)
        reading = {'windows': [{'id': 'weekly', 'remaining_percent': 30, 'reset_at': self.now + 86400}],
                   'credits': {'status': 'disabled', 'unit': 'USD'}}
        outcomes = iter([reading, raised.exception, UsageUnavailable('Limited.', code='rate_limited'), reading, reading])
        calls = []
        def fetch():
            calls.append(True)
            result = next(outcomes)
            if isinstance(result, Exception): raise result
            return result
        source = UsageSource({'claude': fetch, 'codex': lambda: []})
        source.scan(self.now)
        limited = source.scan(self.now + 60)[0]
        self.assertEqual(limited['retry_at'], self.now + 960)
        self.assertTrue(limited['stale'])
        self.assertTrue(limited['credits']['stale'])
        self.assertEqual(limited['updated_at'], self.now)
        for seconds in [120, 300, 600, 959]: source.scan(self.now + seconds)
        self.assertEqual(len(calls), 2)
        repeated = source.scan(self.now + 960)[0]
        self.assertEqual(repeated['retry_at'], self.now + 2760)
        recovered = source.scan(self.now + 2760)[0]
        self.assertFalse(recovered['stale'])
        self.assertFalse(recovered['credits']['stale'])
        self.assertIsNone(recovered['error_code'])
        self.assertIsNone(recovered['retry_at'])
        source.scan(self.now + 4559)
        self.assertEqual(len(calls), 4)
        source.scan(self.now + 4560)
        self.assertEqual(len(calls), 5)

    def test_rate_limited_success_keeps_learned_pace_and_other_provider_stays_independent(self):
        reading = [{"id": "weekly", "remaining_percent": 30, "reset_at": self.now + 86400}]
        outcomes = iter([reading, UsageUnavailable("Limited.", code="rate_limited"),
                         reading, UsageUnavailable("Limited again.", code="rate_limited"), reading, reading])
        calls = {"claude": 0, "codex": 0}
        def claude():
            calls["claude"] += 1
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result
        def codex():
            calls["codex"] += 1
            return reading
        source = UsageSource({"claude": claude, "codex": codex})
        source.scan(self.now)
        self.assertEqual(source.scan(self.now + 60)[0]["retry_at"], self.now + 360)
        recovered = source.scan(self.now + 360)[0]
        self.assertFalse(recovered["stale"])
        source.scan(self.now + 420)
        self.assertEqual(calls, {"claude": 3, "codex": 4})
        source.scan(self.now + 659)
        self.assertEqual(calls["claude"], 3)
        repeated = source.scan(self.now + 660)[0]
        self.assertEqual(repeated["retry_at"], self.now + 1260)
        source.scan(self.now + 1260)
        source.scan(self.now + 1320)
        self.assertEqual(calls["claude"], 5)
        source.scan(self.now + 1859)
        self.assertEqual(calls["claude"], 5)
        source.scan(self.now + 1860)
        self.assertEqual(calls["claude"], 6)

    def test_server_retry_floor_survives_success_and_is_runtime_only(self):
        reading = [{"id": "weekly", "remaining_percent": 30, "reset_at": self.now + 86400}]
        outcomes = iter([UsageUnavailable("Limited.", code="rate_limited", retry_after=3600), reading])
        calls = []
        def fetch():
            calls.append(True)
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result
        source = UsageSource({"claude": fetch, "codex": lambda: reading})
        source.scan(self.now)
        source.scan(self.now + 3600)
        source.scan(self.now + 3660)
        source.scan(self.now + 7199)
        self.assertEqual(len(calls), 2)
        self.assertEqual(source._next_attempts["claude"], self.now + 7200)
        # No setting or credential is changed; a new runtime starts normally.
        restarted = UsageSource({"claude": lambda: reading, "codex": lambda: reading})
        restarted.scan(self.now)
        self.assertEqual(restarted._next_attempts["claude"], self.now + 60)

    def test_repeated_transient_failures_back_off_and_cap_retry_interval(self):
        calls = []
        def fetch():
            calls.append(True)
            raise UsageUnavailable('Unavailable.')
        source = UsageSource({'claude': fetch, 'codex': lambda: []})
        at = self.now
        for wait in [60, 120, 240, 480, 960, 1800, 1800]:
            row = source.scan(at)[0]
            self.assertEqual(row['retry_at'], at + wait)
            count = len(calls)
            source.scan(at + wait - 1)
            self.assertEqual(len(calls), count)
            at += wait

    def test_claude_account_access_reason_survives_helper_and_clears_after_recovery(self):
        helper_error = {"error": "Keychain access was not allowed.", "error_code": "claude_account_access_required"}
        with patch("usage_source.read_process", return_value=SimpleNamespace(stdout=json.dumps(helper_error))):
            with self.assertRaises(UsageUnavailable) as result:
                fetch_claude_usage()
        self.assertEqual(result.exception.code, "claude_account_access_required")
        outcomes = iter([result.exception, [{"id": "weekly", "remaining_percent": 42, "reset_at": self.now + 3600}]])

        def claude():
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value

        source = UsageSource({"claude": claude, "codex": lambda: []})
        failed = source.scan(self.now)[0]
        self.assertEqual(failed["error_code"], "claude_account_access_required")
        self.assertFalse(failed["available"])
        recovered = source.scan(self.now + 60)[0]
        self.assertIsNone(recovered["error_code"])
        self.assertIsNone(recovered["error"])
        self.assertTrue(recovered["available"])

    def test_unknown_helper_error_codes_are_not_exported(self):
        helper_error = {"error": "Account usage is unavailable.", "error_code": "arbitrary-sensitive-value"}
        with patch("usage_source.read_process", return_value=SimpleNamespace(stdout=json.dumps(helper_error))):
            with self.assertRaises(UsageUnavailable) as result:
                fetch_claude_usage()
        self.assertIsNone(result.exception.code)

    def test_past_reset_does_not_invent_a_refill(self):
        def fetch():
            return [{"id": "weekly", "label": "Weekly", "remaining_percent": 0, "reset_at": self.now - 1}]
        row = UsageSource({"claude": fetch, "codex": fetch}).scan(self.now)[0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["windows"][0]["remaining_percent"], 0)

    def test_credit_only_observation_remains_separate_from_weekly_usage(self):
        first_reading = iter([{"windows": [], "credits": {"amount": 0, "unit": "credits"}}])

        def codex():
            reading = next(first_reading, None)
            if reading is None:
                raise UsageUnavailable("Provider is unavailable.")
            return reading

        source = UsageSource({"codex": codex, "claude": lambda: [
            {"id": "five_hour", "remaining_percent": 0, "reset_at": self.now - 1},
            {"id": "weekly", "remaining_percent": 7, "reset_at": self.now + 1000},
        ]})
        claude, codex_first = source.scan(self.now)
        self.assertEqual([window["id"] for window in claude["windows"]], ["weekly"])
        self.assertFalse(claude["stale"])
        self.assertEqual(codex_first["windows"], [])
        self.assertEqual(codex_first["credits"], {"amount": 0, "unit": "credits", "observed_at": self.now, "stale": False})
        _, failed = source.scan(self.now + 61)
        self.assertEqual(failed["credits"], {**codex_first["credits"], "stale": True})

    def test_redirects_are_rejected_before_forwarding_bearer(self):
        with patch("usage_source._claude_token", return_value="fixture"), \
                patch("usage_source.read_provider_json", side_effect=ProviderRequestError("redirect")):
            with self.assertRaisesRegex(UsageUnavailable, "redirected"):
                _fetch_claude_in_process(self.now)

    def test_background_keychain_query_suppresses_legacy_prompts_and_restores_prior_state(self):
        for initial in (0, 1):
            for query_fails in (False, True):
                with self.subTest(initial=initial, query_fails=query_fails):
                    current = initial

                    def get_interaction(pointer):
                        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte))[0] = current
                        return 0

                    def set_interaction(value):
                        nonlocal current
                        current = value
                        return 0

                    def copy_item(query, result):
                        self.assertEqual(current, 0)
                        if query_fails:
                            raise RuntimeError("Fixture failure")
                        return -25308

                    security = SimpleNamespace(SecKeychainGetUserInteractionAllowed=Mock(side_effect=get_interaction),
                                               SecKeychainSetUserInteractionAllowed=Mock(side_effect=set_interaction),
                                               SecItemCopyMatching=Mock(side_effect=copy_item))
                    if query_fails:
                        with self.assertRaisesRegex(RuntimeError, "Fixture failure"):
                            _copy_keychain_item(security, None, None, allow_interaction=False)
                    else:
                        self.assertEqual(_copy_keychain_item(security, None, None, allow_interaction=False), -25308)
                    self.assertEqual(current, initial)

    def test_background_keychain_query_fails_closed_without_legacy_prompt_guard(self):
        copy_item = Mock()
        fixtures = (
            SimpleNamespace(SecItemCopyMatching=copy_item),
            SimpleNamespace(SecKeychainGetUserInteractionAllowed=Mock(return_value=-1),
                            SecKeychainSetUserInteractionAllowed=Mock(), SecItemCopyMatching=copy_item),
            SimpleNamespace(SecKeychainGetUserInteractionAllowed=Mock(return_value=0),
                            SecKeychainSetUserInteractionAllowed=Mock(return_value=-1), SecItemCopyMatching=copy_item),
        )
        for security in fixtures:
            with self.assertRaises(UsageUnavailable):
                _copy_keychain_item(security, None, None, allow_interaction=False)
        copy_item.assert_not_called()

    def test_explicit_keychain_authorization_can_query_without_background_guard(self):
        security = SimpleNamespace(SecItemCopyMatching=Mock(return_value=-128))
        self.assertEqual(_copy_keychain_item(security, "query", "result", allow_interaction=True), -128)
        security.SecItemCopyMatching.assert_called_once_with("query", "result")

    def test_active_account_and_organization_are_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cache = {
                "acct:wrong-account|client:active-org:https://api.anthropic.com:user:profile": {"token": "wrong-account-token", "expiresAt": (self.now + 1000) * 1000},
                "acct:active-account|client:wrong-org:https://api.anthropic.com:user:profile": {"token": "wrong-org-token", "expiresAt": (self.now + 2000) * 1000},
                "acct:active-account|client:active-org:https://not-anthropic.example:user:profile": {"token": "wrong-host-token", "expiresAt": (self.now + 3000) * 1000},
                "acct:active-account|client:active-org:https://api.anthropic.com:user:profile": {"token": "right-token", "expiresAt": (self.now + 100) * 1000},
                "acct:active-account|client:active-org:https://api.anthropic.com:user:inference": {"token": "lower-priority-token", "expiresAt": (self.now + 3000) * 1000},
            }
            (folder / "config.json").write_text(json.dumps({"lastKnownAccountUuid": "active-account", "oauth:tokenCacheV2": base64.b64encode(b"encrypted-fixture").decode()}))
            with patch("usage_source._keychain_password", return_value=b"fixture"), patch("usage_source._decrypt_chromium", side_effect=lambda raw, password: json.dumps(cache).encode()), patch("usage_source._active_claude_org", return_value="active-org"):
                self.assertEqual(_claude_token(folder, self.now), "right-token")
                with self.assertRaises(UsageUnavailable):
                    _claude_token(folder, self.now + 4000)

    def test_codex_cold_start_budget_is_independent_of_claude_http_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "slow-codex-fixture"
            write_python_fixture(executable, f'''
import sys,json,time
for line in sys.stdin:
 value=json.loads(line)
 if value.get('id')==1:
  time.sleep(0.08)
  print(json.dumps({{'id':1,'result':{{}}}}),flush=True)
 if value.get('id')==2:
  print(json.dumps({{'id':2,'result':{{'rateLimits':{{'limitId':'codex','primary':{{'usedPercent':95,'windowDurationMins':10080,'resetsAt':{self.now + 1000}}}}}}}}}),flush=True)
''')
            executable.chmod(0o700)
            with patch("usage_source._codex_binary", return_value=str(executable)), patch("usage_source.REQUEST_TIMEOUT", 0.001), patch("usage_source.CODEX_TIMEOUT", 1.0):
                self.assertEqual(fetch_codex_usage()[0]["remaining_percent"], 5)

    def test_stdio_only_initializes_and_reads_account(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            executable, record = folder / "codex-fixture", folder / "methods.jsonl"
            write_python_fixture(executable, f'''
import sys,json
record={str(record)!r}
for line in sys.stdin:
 value=json.loads(line)
 with open(record,'a') as output: output.write(json.dumps(value['method'])+'\\n')
 if value.get('id')==1: print(json.dumps({{'id':1,'result':{{}}}}),flush=True)
 if value.get('id')==2:
  rate_limits = {{'limitId':'codex','primary':{{'usedPercent':95,'windowDurationMins':10080,'resetsAt':{self.now + 1000}}},'credits':{{'balance':'0','hasCredits':False,'unlimited':False}}}}
  print(json.dumps({{'id':2,'result':{{'rateLimits':rate_limits}}}}),flush=True)
''')
            executable.chmod(0o700)
            with patch("usage_source._codex_binary", return_value=str(executable)):
                reading = fetch_codex_usage(include_credits=True)
                self.assertEqual(reading["windows"][0]["remaining_percent"], 5)
                self.assertEqual(reading["credits"], {"amount": 0, "unit": "credits"})
            self.assertEqual([json.loads(line) for line in record.read_text().splitlines()], ["initialize", "initialized", "account/rateLimits/read"])

    def test_account_child_does_not_inherit_telemetry_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "environment-codex-fixture"
            write_python_fixture(executable, f'''
import sys,json,os
assert os.environ.get('OTEL_SDK_DISABLED')=='true'
assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in os.environ
assert 'OTEL_RESOURCE_ATTRIBUTES' not in os.environ
assert 'SENTRY_DSN' not in os.environ
assert sys.argv[1:]==['app-server','--stdio','-c','analytics.enabled=false']
for line in sys.stdin:
 value=json.loads(line)
 if value.get('id')==1: print(json.dumps({{'id':1,'result':{{}}}}),flush=True)
 if value.get('id')==2:
  print(json.dumps({{'id':2,'result':{{'rateLimits':{{'limitId':'codex','primary':{{'usedPercent':95,'windowDurationMins':10080,'resetsAt':{self.now + 1000}}}}}}}}}),flush=True)
''')
            with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://developer.example/collect",
                                         "OTEL_RESOURCE_ATTRIBUTES": "private.fixture=do-not-forward",
                                         "SENTRY_DSN": "https://fixture@developer.example/1"}), \
                    patch("usage_source._codex_binary", return_value=str(executable)):
                self.assertEqual(fetch_codex_usage()[0]["remaining_percent"], 5)


if __name__ == "__main__":
    unittest.main()
