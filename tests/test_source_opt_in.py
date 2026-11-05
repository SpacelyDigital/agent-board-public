"""No account-reader construction without an explicit native/CLI choice."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import Monitor, parse_arguments


class SourceOptInTests(unittest.TestCase):
    def test_cli_is_local_by_default_and_explicit_choices_are_mutually_exclusive(self):
        _, args = parse_arguments([])
        self.assertFalse(args.usage_enabled)
        self.assertFalse(args.projects_enabled)
        _, args = parse_arguments(["--usage", "--projects"])
        self.assertTrue(args.usage_enabled)
        self.assertTrue(args.projects_enabled)
        _, args = parse_arguments(["--no-usage", "--no-projects"])
        self.assertFalse(args.usage_enabled)
        self.assertFalse(args.projects_enabled)
        for flags in (["--usage", "--no-usage"], ["--projects", "--no-projects"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_arguments(flags)

    def test_no_root_monitor_does_not_construct_any_account_reader_by_default(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch("server.CodexSource"))
            stack.enter_context(patch("server.ClaudeSource"))
            usage = stack.enter_context(patch("server.UsageSource", side_effect=AssertionError("Implicit credential access")))
            projects = stack.enter_context(patch("server.ClaudeProjectSource", side_effect=AssertionError("Implicit account request")))
            monitor = Monitor(Path(folder))
            self.assertIsNone(monitor.usage_source)
            self.assertIsNone(monitor.project_source)
            usage.assert_not_called()
            projects.assert_not_called()

    def test_explicit_monitor_choices_construct_only_selected_account_readers(self):
        for allow_usage, allow_projects in ((True, False), (False, True), (True, True)):
            with self.subTest(usage=allow_usage, projects=allow_projects), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                stack.enter_context(patch("server.CodexSource"))
                stack.enter_context(patch("server.ClaudeSource"))
                usage = stack.enter_context(patch("server.UsageSource"))
                projects = stack.enter_context(patch("server.ClaudeProjectSource"))
                Monitor(Path(folder), usage_enabled=allow_usage, projects_enabled=allow_projects)
                self.assertEqual(usage.call_count, int(allow_usage))
                self.assertEqual(projects.call_count, int(allow_projects))


if __name__ == "__main__":
    unittest.main()
