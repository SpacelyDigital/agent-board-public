# Browser stress journeys

Run `python3 tests/stress_browser.py --profiles 300 --concurrency 2` after installing Playwright WebKit. Set `PLAYWRIGHT_MODULE` if Playwright is installed outside normal Node module resolution. The local development fallback is `/private/tmp/azwk/node_modules/playwright`.

Each simulated user receives an independent browser context, HOME, provider fixture files, collector process, display secret, and SQLite database. The harness copies the runtime into its output directory first and records SHA-256 hashes, so a run tests one exact source snapshot. The collector receives a minimal environment without provider credentials, explicit disposable source roots, and both account-usage and cloud-project reads disabled. The browser blocks non-loopback requests and all desktop chat-open actions.

The 300-profile default covers every combination of five layouts, 15 themes, and two modes twice, with desktop and narrow viewports. Seeded journeys exercise rename, literal HTML text, Snooze, Review and Restore, Board and Background placement, automatic titles and placement, keyboard controls, previews, rapid appearance changes, preference reloads, stale actions in two tabs, connection loss, actual collector restarts, and a damaged database recovery. A delayed save scenario checks that completing an old title change preserves a newer rename dialog. A subset attempts reciprocal reads using another running installation's token and checks that no canary title is exposed.

Reports are written under `.local/stress-browser/`. A failed profile records its seed, failing step, API response statuses, prior actions, JavaScript errors, and a screenshot. The harness stops starting new profiles after a failure. SIGINT or SIGTERM finishes active journeys and then exits; every fixture collector is terminated in cleanup. An intentionally stopped run is incomplete and must not be reported as a full pass.

These are real WebKit, HTTP, source-adapter, and database workflows using fake provider files. They do not verify native macOS permission prompts, Gatekeeper, provider accounts, hundreds of simultaneous computers, or every possible user action.
