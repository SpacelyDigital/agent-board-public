"""Native display secrets must never accept shared or substituted files."""

import os
from pathlib import Path
import tempfile
import unittest

from server import read_access_token


class DisplayAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "display-access"
        self.path.write_text("a" * 64)
        self.path.chmod(0o600)

    def test_private_secret(self):
        self.assertEqual(read_access_token(self.path), "a" * 64)

    def test_shared_secret_is_rejected(self):
        for mode in (0o644, 0o660, 0o604):
            self.path.chmod(mode)
            with self.assertRaises(ValueError):
                read_access_token(self.path)

    def test_substituted_symlink_is_rejected(self):
        link = self.path.with_name("substitute")
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            read_access_token(link)

    def test_shared_hardlink_is_rejected(self):
        link = self.path.with_name("hardlink")
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            read_access_token(link)

    def test_invalid_or_oversized_secret_is_rejected(self):
        for value in ("", "a" * 63, "G" * 64, "a" * 129, "a" * 64 + "\n" * 1000):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                read_access_token(self.path)


if __name__ == "__main__":
    unittest.main()
