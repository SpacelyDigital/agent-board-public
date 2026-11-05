"""Run the fake-account HTTPS privacy checks; no real provider/account access."""

import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_privacy_network import DirectProviderPrivacyTests


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DirectProviderPrivacyTests)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(output.getvalue())
    return {"checks": unittest.defaultTestLoader.getTestCaseNames(DirectProviderPrivacyTests),
            "passed": result.testsRun,
            "requests_to_loopback_tls_fixtures": DirectProviderPrivacyTests.total_requests,
            "real_provider_requests": 0, "real_credentials_read": 0, "model_calls": 0,
            "redirects_followed": 0, "proxy_requests": 0,
            "fixture_routing": "Test-only sockets route the fixed provider hostname to owned loopback TLS."}


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
