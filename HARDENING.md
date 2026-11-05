# Hardening record

This record starts with the fixture-only hardening round completed on October 30,
2026 and includes later signed-artifact, clean-VM fixture and actual native/provider
checks. It records reproduced failures, fixes, test methods and remaining release
checks. Passing
these checks does not establish that every possible defect has been eliminated.

## Isolation and privacy

The October 30 simulations made **zero real provider requests, zero model calls, and zero reads of real Claude/Codex credentials or Keychain password items**. They used disposable source files, fake credentials where authentication behavior was under test, fake executables, and loopback HTTP fixtures. They did not start or resume real chats.

Each browser profile received an independent HOME, provider fixture directories, private display secret, collector process, browser context, and SQLite database. Collectors received a minimal environment without provider credentials, explicit fixture source roots, and both usage and cloud-project reads disabled. The browser blocked requests outside its owned loopback origin and blocked desktop chat-open actions. All completed browser-run collectors were checked for cleanup.

The application has no developer telemetry or collection endpoint. Optional authenticated provider reads are separate from developer collection and are described in [PRIVACY.md](PRIVACY.md). The simulated tests did not exercise those reads against real providers. Opening the optional Ko-fi link is a user-initiated external browser action, not an automatic dashboard request.

Evidence paths below are relative to the repository. The `.local/` reports and fixtures are local QA artifacts and are intentionally excluded from source and release exports.

## Browser and API workflows

The browser runner used actual headless Playwright WebKit, the production UI scripts, real collector HTTP handlers, production source adapters reading fake files, and real SQLite stores. It ran two journeys at a time. It did not represent 300 simultaneous computers or 300 native macOS installations.

| Run | Independent profiles | Failed profiles | Recorded workflow steps | API action attempts | HTTP outcomes | Appearance combinations | Cross-installation checks | Time |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| Pinned matrix | 300 | 0 | 5,636 | 2,443 | 2,413 successful 200s; 30 expected stale-action 409s | 150 | 36 | 535.156 seconds |
| Later-source smoke | 3 | 0 | 62 | 28 | 26 successful 200s; 2 expected stale-action 409s | 3 | 1 | 7.635 seconds |

The 150 combinations comprise five layouts, 15 themes, and two modes. Every combination ran twice in the pinned matrix with different seeded control sequences. Journeys used narrow and desktop viewports. A recorded workflow step is a trace entry, not a count of every individual mouse or keyboard event.

The journeys exercised rename and automatic titles, literal HTML text, Snooze, Review and Restore, Board and Background placement, automatic placement, keyboard navigation, previews, appearance changes, reload persistence, two-tab stale actions, connection loss, actual collector restarts, review persistence, and damaged-database recovery. Cross-installation checks attempted reciprocal reads with another running installation's token and checked that no canary title was exposed. Stale-response tests deliberately froze one browser tab's state response while lifecycle changes continued through real fixture logs and the real store.

Reproduced UI failures were:

- **Snooze always returned 409.** The UI omitted the displayed event ID required by the backend revision guard. The UI now sends that ID, and stale Snooze requests remain rejected. Seed `20365759` reproduced the original failure.
- **An old title save closed a newer rename dialog.** A delayed save for one chat could close another chat's newly opened rename dialog and lose its unsaved draft. A dialog revision and key guard now restrict each completion to the dialog that submitted it. Seed `20470488` reproduced the original failure.
- **Recovery information needed persistent display.** The existing monitor notice now keeps the local recovery warning through healthy polling and restarts, and shows it alongside a disconnect warning. Source health remains visible. This behavior was checked with an actually malformed fixture database and real collector restarts.

Before-fix failures, screenshots, and after-fix results remain in:

- `.local/stress-browser/snooze-before-fix-2/profile-0000/failure.json`
- `.local/stress-browser/rename-race-before-fix/profile-0001/failure.json`
- `.local/stress-browser/rename-race-after-fix/results.json`
- `.local/stress-browser/final-pinned-users-300-seed-20261030/results.json`
- `.local/stress-browser/final-pinned-users-300-seed-20261030/evidence-report.md`
- `.local/stress-browser/final-pinned-users-300-seed-20261030/cleanup-verification.json`
- `.local/stress-browser/release-current-source-smoke/results.json`

### Exact source scope

The 300-profile run used an immutable snapshot of 29 HTTP/UI runtime files. Its manifest is `.local/stress-browser/final-pinned-users-300-seed-20261030/source-manifest.json`, with SHA-256 `098da69f431311c50fc6f5f10c44bfc92e8edc6475227e644cd44fecd144e78d`. The browser harness files also matched their recorded hashes at completion.

Four runtime files changed after that snapshot: `board_store.py` gained stricter event-ID handling, `claude_source.py` gained linear context stripping, `process_reader.py` received formatting cleanup, and `server.py` gained native parent-lifetime and startup/shutdown handling. The later three-profile smoke used a new snapshot that matched all 29 current runtime files when it completed. Its manifest is `.local/stress-browser/release-current-source-smoke/source-manifest.json`, with SHA-256 `ee4bcb059227c7090b87ff8088ac43c3512ba5286582ff8a2e46e911de86fe7f`.

The three-profile smoke covered the changed runtime in integration alongside focused backend regressions. It did not repeat the entire appearance matrix. Browser fixtures did not enable the native parent-lifetime flag, which has separate process-lifetime tests. Neither snapshot includes the native Swift app or establishes the identity of a later packaged artifact.

Earlier discovery and deliberately stopped runs are retained but are not counted as full 300-profile passes. The initial discovery completed 110 profiles before repinning; two in-flight profiles were discarded when that old runner was stopped.

## Adapter and helper hardening

The adapter review used 500 synthetic Codex chats, 500 synthetic Claude chats, 1,000 mutated Codex events, and 77 project-schema mutations, supported by 12 adversarial unit tests. The final merged fixture regression suite passed all 171 tests in 8.648 seconds. These regression tests support the integration checks; their count does not include the browser journeys or native smoke workflows.

The fixes cover nonfinite and overflowing timestamps, corrupt or deeply malformed JSON, unsupported record shapes, mismatched source identities, FIFO and malformed rollout paths, bounded helper output, nonblocking input deadlines, active-account and organization selection using fake caches, finite credential expiry, and bounded printable tokens. Fixed-origin request paths reject cross-origin controls and malformed identifiers. Redirects are not followed, and provider failure bodies are not exposed as error text.

Repeated incomplete context tags and code fences previously caused excessive caption-parsing work. The parser now scans them without repeated whole-input rescans. Four complexity cases passed; one recorded fixture with 35,000 repeated opening tags completed in 0.0086 seconds on the test machine. This is a local measurement, not a performance guarantee for every machine or transcript.

Four real helper-process lifetime workflows passed against both source and frozen watchdog dispatch: normal completion, explicit shutdown, forced collector death, and forced death before reader registration. Gated execution prevents the target executable from starting until supervision is registered. Same-group descendants that ignore SIGTERM are reaped, and watchdog descriptor updates are serialized.

Seven authenticated network checks passed using fake tokens and loopback provider fixtures, with six local GET requests, zero followed redirects, and zero real provider requests. The frozen dispatch check used the packaged gated-exec boundary to launch fixture Python; it did not authenticate a real provider account.

Evidence:

- `.local/privacy-redteam/results.json`
- `.local/privacy-redteam/source-lifetime.json`
- `.local/privacy-redteam/frozen-lifetime.json`
- `.local/privacy-redteam/frozen-gated-network.json`
- `.local/privacy-redteam/bundle-dependencies.json`

## HTTP and local-state hardening

The real loopback HTTP suite exercised 200 deterministic actors making 1,000 mixed actions, with at most 16 actors concurrent. These actors shared one board; they were not 200 independent installations. A separate two-installation fixture checked distinct tokens, data canaries, and persistence. Ten focused HTTP workflows and ten store workflows were recorded in the API review.

Reproduced problems and changes include:

- Request sockets could accumulate threads indefinitely. The server now limits concurrent handlers to 32, applies five-second absolute header and body deadlines, and bounds request bodies to 16,384 bytes.
- Duplicate security headers and ambiguous framing were handled inconsistently. Duplicate Host, Origin, authentication and Content-Length headers, and Transfer-Encoding requests are rejected. Packaged private reads require the per-launch secret; mutations also require the CSRF guard. Source CLI development without an access-token file retains its documented browser-compatible mode.
- Rejected requests could flood logs or include private identifiers and terminal controls. Rejection logging is generic and rate-limited to one line per five seconds.
- A FIFO substituted for the display secret could block startup. Nonblocking open now permits immediate validation. Symlinks, hardlinks, nonregular files, foreign ownership and shared permissions are rejected.
- Malformed source fields, unpaired surrogates and malformed stored extra JSON could break scans or the state feed. Normalization and persisted-extra reads are typed and bounded. Overlong event IDs are rejected, while distinct surrogate event values retain distinct revision hashes.
- Invalid activation metadata such as NaN could produce illegal JSON. It is backed up and repaired without discarding chats, preferences or review state. Invalid numeric API state and transient SQLite errors return valid, recoverable 503 JSON rather than NaN or Infinity.

Evidence:

- `.local/redteam-api/baseline-reproductions.json`
- `.local/redteam-api/findings.md`
- `.local/redteam-api/summary.json`

### Recovery preserves evidence

Only confirmed SQLite corruption codes permit automatic replacement of the board database. The recovery path preserves the original database family and private copies before opening a fresh board. Partial copy or move failures retain originals or attempt rollback; an incomplete recovery does not proceed to a new board. Busy, locked, permission, disk-full and I/O errors do not trigger a fresh database.

Invalid activation metadata follows a narrower repair path: a complete local database backup is made, the invalid discovery timestamp is repaired, and existing chats, preferences and acknowledgements remain. Recovery and repair notices persist locally across restarts and identify the local preservation location.

Preserving damaged files does not prove their contents can be salvaged. Before manual restoration, quit Agent Board and preserve both the current data and recovery folders. Restore the database together with matching WAL/SHM files, if present, from one known-good backup. Do not mix files from different backups or assume the preserved damaged file is a usable backup.

## Native review and test boundaries

The native review reproduced a collector surviving forced native-owner death. A baseline fixture still returned HTTP 200 after its native parent was killed. Native launches now use a private stdin lifetime pipe, and collectors stop when that pipe closes. Source CLI launches can opt out. Six source lifetime workflows passed, including already-closed ownership, unexpected pipe input, forced owner death, and managed-reader cleanup.

Two native callback scenarios passed using the production request, polling and action methods extracted from `MacApp.swift`, compiled as Swift and exercised against real delayed loopback HTTP responses. They checked that late 200/503 responses cannot cross a collector generation and that no requests are sent after the owned collector dies. UI effects were represented by counters; this was not an end-to-end launch of the installed app.

Four notification-intent workflows passed using extracted production Swift methods and a fake permission center. They checked late settings/grants, explicit disable, newer denial, and a current successful grant. No OS notification permission dialog was displayed, and no notification was delivered.

The native navigation smoke passed using an actual hidden, nonpersistent AppKit `WKWebView` with the production navigation delegate. It rendered the starting and stopped local documents and verified that the stopped document replaced the old DOM. It is distinct from the headless Playwright browser and from extracted-method fixtures.

The final parent-lifetime run passed seven workflows. It included the frozen collector, a compiled Swift owner killed with SIGKILL, and managed-reader cleanup. The compiled-owner case tracked one owned process and found zero remaining after owner death. The managed-reader case used a source fixture; the report identifies that distinction. The native runtime smoke also passed against the actual `CollectorRuntime.swift` and frozen collector, covering launch, authentication, fixture rename, persistence, restart, unexpected-exit handling, and cleanup of a same-group helper that ignores SIGTERM.

Native evidence and maintained checks include `.local/native-review-qa/parent-death.json` for the baseline failure, `.local/native-review-qa/parent-lifetime-final.json` for the final seven workflows, `.local/native-state-qa/`, `.local/native-notification-qa/`, `.local/native-navigation-qa/`, `.local/native-distribution-qa/`, and the `tests/native_*_smoke.py` files. None of these methods establishes clean-Mac Setup, TCC permission prompts, launch-at-login registration, real Keychain approval, Gatekeeper acceptance, notarization, or App Store review.

## Repeating the checks

Run commands from the repository root with development dependencies installed. Playwright WebKit and Swift are test/build dependencies; they are not recipient installation requirements.

```sh
python3 -m unittest discover -s tests
python3 tests/stress_browser.py --profiles 300 --concurrency 2 --seed 20261030
python3 tests/privacy_network_smoke.py
python3 tests/reader_lifetime_smoke.py
python3 tests/native_parent_lifetime_smoke.py
python3 tests/native_state_smoke.py
python3 tests/native_notification_smoke.py
python3 tests/native_navigation_smoke.py
```

After building the packaged application, the frozen process checks accept its collector executable, and the native runtime check uses the built app with disposable fixtures:

```sh
python3 tests/reader_lifetime_smoke.py "build/Agent Board.app/Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
python3 tests/native_parent_lifetime_smoke.py "build/Agent Board.app/Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
python3 tests/native_runtime_smoke.py
```

The browser runner records seeds, traces, responses, failures and screenshots under `.local/stress-browser/`, and hashes an immutable runtime snapshot before testing it. A source change after that snapshot requires a new scope decision and relevant verification. An intentionally stopped run must be reported as incomplete.

## Final 0.2.1 artifact verification

The final merged regression suite passed all 171 tests using the packaged build's
portable Python. The rebuilt collector passed 40 HTTP/SQLite/WebKit checks, and
the ZIP-extracted collector passed the same 40 checks independently. These
included authenticated production UI reads/actions, Review and Restore,
About/privacy, no external browser requests, unchanged provider fixtures and
restart persistence. The maintained runner is `tests/packaged_workflow_smoke.py`
with `tests/packaged_workflow_smoke.cjs`; set `PLAYWRIGHT_MODULE` if needed.
It never dispatches the real account or authorization helper modes.

The native runtime fixture exercised the actual `CollectorRuntime.swift` and
frozen helper through launch, authentication, rename, saved-state restart,
per-launch secret rotation, unexpected exit and stubborn same-group shutdown.
Seven final parent-lifetime workflows passed, including force-killing an actual
compiled Swift owner and confirming its frozen collector and captured owned
descendants exited. Four notification-intent workflows and the hidden native
WKWebView starting-to-stopped document replacement also passed. These remain
fixture checks rather than actual OS permission grants.

The exact ZIP-extracted helper passed all four watchdog/reader lifetime checks
and seven fake-provider HTTP checks through gated launch. Its SHA-256 is
`3f3e0660b74cd02c0648be6ce93fd1f07bdd612dd58df7fc2b78557a0e8cdd2e`.
The HTTP fixture launches harmless Python from the frozen gate and patches
source adapters to loopback; it does not exercise frozen real-account dispatch.

The final ZIP was extracted into a separate path containing spaces. Deep strict
ad-hoc signature validation passed, and both native executable hashes matched
the tested build. A read-only final DMG mount verified its app, Applications
shortcut, preview label, signature and matching executable hashes, then unmounted.
Native dependency inspection found no build-host library dependency: the arm64
display targets macOS 13, and the collector/Python targets macOS 11. The actual
host was macOS 26.6; macOS 13 was not run.

| Local-preview artifact | SHA-256 |
| --- | --- |
| `Agent-Board-0.2.1-macOS-arm64-local-preview.zip` | `c8983df9f9b89f18937d3096638e77bab449ba042c3023587fb0d59ae84795a3` |
| `Agent-Board-0.2.1-macOS-arm64-local-preview.dmg` | `bbd58a15038cf2e9547ee8fda8edc5b255bf333fb8da94fcf7edee84d998b542` |

Final local evidence includes `.local/final-validation/VALIDATION-SUMMARY.json`,
`.local/final-artifact-qa/`, `.local/packaged-workflow-qa/run-0gvx0zsf/`,
`.local/native-review-qa/parent-lifetime-final.json`, and
`.local/privacy-redteam/final-extracted-*.json`.

At the October 30 fixture pass, this machine had no usable signing identity,
so those artifacts were ad-hoc local previews. Developer ID signing and
notarization were completed later, as recorded below. The owner-installed app
and board were not changed. Sanitized source and local-preview downloads were
backed up locally and privately. No public or App Store release was performed.
The approved live phase and its limits are recorded in
[REAL_ENVIRONMENT_TEST_PLAN.md](REAL_ENVIRONMENT_TEST_PLAN.md).

## Developer ID artifact validation on 2026-11-03

Version 0.2.1 build 3 was signed with Developer ID and the hardened runtime.
Apple accepted both the ZIP and DMG notarization submissions. Independent local
checks passed for app and DMG signatures, the app/collector/Python hardened
runtime, stapled-ticket validation, Gatekeeper acceptance and DMG integrity.
All 70 file/symlink entries in the extracted ZIP and mounted DMG app matched
the stapled staging app. The read-only DMG was detached after inspection.

Later clean-VM testing reproduced a startup hang in this 0.2.1 collector. Its
private draft backup is marked with the known issue and is not a release
candidate. The artifact checks below remain historical signature and payload
evidence; the 0.2.2 startup fix has separate results below.

| Developer ID artifact | SHA-256 |
| --- | --- |
| `Agent-Board-0.2.1-macOS-arm64.zip` | `97e0c7826c2e5d2134d2c8448775e9cd0c0d92a72f75eb23fac3e06e0e27c18c` |
| `Agent-Board-0.2.1-macOS-arm64.dmg` | `6b30b5f557063147e2c67e91a2153f39d5962a95bab4d7e13f7695aeb5084425` |

The signed build passed 40 packaged HTTP/SQLite/WebKit fixture checks, four
frozen reader-lifetime checks, seven parent-lifetime checks and native runtime
launch, private authentication, persistence and restart checks. No owned fixture
processes remained. These checks made zero real provider requests, zero model
calls and zero real credential or Keychain password-item reads. Their fixtures
do not establish actual macOS permission grants or live provider compatibility.

The disposable recipient VM runs macOS 26.6.2 (25G83). At baseline it had no
active Xcode/command-line developer tools and no Agent Board data. Read-only
checks verified installed OpenAI desktop app `com.openai.codex` version
26.1027.61615 build 24141, and Claude desktop version 4.4212.0. Both passed
deep strict signature and Gatekeeper assessment as Notarized Developer ID.
These metadata checks do not establish an Agent Board integration pass.
The owner reported signing in to both providers; no test prompts had been sent
at this earlier artifact/metadata checkpoint. Later native tests are recorded below.

The OpenAI app had no quarantine or download-origin metadata at inspection.
Claude retained Safari quarantine metadata but no download-origin metadata.
These checks verify the installed provider bundles; they do not reconstruct
their original download paths.

Safari's first Agent Board DMG download attempt stopped at 358 KB; a retry
completed. Read-only guest metadata showed the exact expected 12,349,613-byte
size, and guest Gatekeeper accepted it as Notarized Developer ID. macOS Downloads
privacy controls denied the SSH session full-file and extended-attribute access,
so the downloaded copy's hash, quarantine, origin metadata and direct signature
checks remain unverified. No privacy permissions were broadened. At this earlier
0.2.1 transfer checkpoint, Finder installation and first-open workflows had not
been completed. Later versions have separate actual installation results below.
The interruption is a recorded test-transfer failure; its cause was not established.

Local evidence is under
`.local/live-validation/20261103-025735/notarized-artifact-check/`,
`.local/live-validation/20261103-025735/signed-build-qa/`,
`.local/live-validation/20261103-025735/guest-baseline.json`,
`.local/live-validation/20261103-025735/guest-download-verification.json`, and
`.local/live-validation/20261103-025735/provider-installers/guest-provider-verification.json`.
These local QA records are excluded from source exports. No public release,
App Store submission or change to the existing owner installation was performed.

## Clean-VM startup failure and 0.2.2 build 4

The signed 0.2.1 frozen collector stalled before its local health endpoint became
available on the disposable macOS 26.6.2 VM. A process sample showed startup
blocked in `socket.gethostbyaddr`, called by Python's HTTP server hostname
resolution. `BoardHTTPServer.server_bind` now binds through the TCP server
directly and assigns its local server name without reverse DNS.

Two added regression tests exercise an actual authenticated loopback health
request with both resolver functions configured to fail, and an occupied-port
failure that must close only its failed server socket. The full source suite
passed all 173 tests after the fix. That fixed checkpoint was version 0.2.2 build 4.

The exact signed candidate passed 34 frozen-executable checks on the clean guest.
The staged app signatures and these two executable hashes matched the candidate:

| Candidate executable | SHA-256 |
| --- | --- |
| Native display | `a78ae01c6d275112422279a51def637e041a6bc1bf75a324af6cc6d0413e52f4` |
| Frozen collector | `cb1ad75f4e043d97dfc9303ce622f29724cbc656502866b511f8da920ae23242` |

The collector became healthy in 0.902 seconds without a separately installed
Python. The checks used explicit fake provider directories with usage and
authenticated Claude projects disabled. They exercised loopback-only listening,
authenticated state/health, rejected foreign-origin and missing-CSRF mutations,
Cooking-to-Ready updates, Review and Restore across restart, one identity for a
newer turn, stale-action rejection, private file modes and parent/reader/helper
cleanup. The parent lifetime protocol used SSH stdin. These results do not
establish an actual Swift desktop-owner launch, quarantined Finder installation,
native setup permissions, Keychain access, notifications or provider chat links.

The signed candidate also passed 40 packaged HTTP/SQLite/WebKit workflows,
four frozen reader-lifetime checks and seven parent-lifetime checks. The native
runtime fixture passed launch, private authentication, actual fixture reads and
rename, persistence, stable restart, unexpected-exit cleanup and termination of
an owned helper group that ignores SIGTERM. A source-to-bundle privacy audit
matched all nine inspected modules and 20 local assets. These are fixture and
static-bundle checks, with no real provider or account requests.

Apple accepted both new 0.2.2 notarization submissions, and the release pipeline
completed app/DMG ticket stapling and validation. All 80 independent
final-artifact checks passed. Source, staged, ZIP and DMG executable hashes
matched the candidate. The 69 shared source payload entries matched the staged
app, whose only additional entry was Apple's stapled ticket. Staged, ZIP and
DMG app payloads matched all 70 entries, including file modes and symlinks.
Nested signatures, hardened runtime, stapled tickets, Gatekeeper and DMG
integrity passed; the read-only DMG mount was detached. Apple submission status
was independently confirmed as Accepted for both artifacts.

| 0.2.2 artifact | SHA-256 |
| --- | --- |
| `Agent-Board-0.2.2-macOS-arm64.zip` | `5128ec206d9ce7c5bc4386a220afdf6b8930119616cbfa439a6f5471da37d401` |
| `Agent-Board-0.2.2-macOS-arm64.dmg` | `b87c9ec00b8700b6ba366ba7ec5aa926372dd8f183a68747e67f8dc020aeb101` |

The frozen-collector fixture soak completed on November 3 from 04:41:18 to
12:41:19 EST. Independent review of the full ledger passed all 20 consistency
checks, including the exact pinned candidate hashes, elapsed duration, cycle
sequence, sampled sockets and cleanup events.

| Completed fixture soak measurement | Result |
| --- | --- |
| Measured duration after warmup | 28,800.6 seconds |
| Health/resource samples | 477 |
| Fake lifecycle cycles | 24 |
| Collector restarts | 8 |
| Successful collector starts and stops | 9 each |
| Socket/helper checks | 96 |
| Peak resident memory | 41,120 KiB |
| Largest sample gap | 66.4 seconds |
| Recorded failures | 0 |

All sampled socket records showed only the collector's loopback listener and no
unexpected direct helpers. Collector-generation median resident memory ranged
from 40,304 to 40,624 KiB, with no sustained increase across generations observed.
Eight periodic restarts and tiny fake inputs limit conclusions about memory in a
long uninterrupted session. Health/resources were sampled about once per minute,
and sockets/helpers about once every five minutes; this was not continuous
network capture. The ledger records all nine successful stops and final cleanup.
The soak used only fake files, with zero model calls or account reads. It does
not establish native installation, OS permissions or real-provider behavior.

At the completion of this fixture checkpoint, pointer and modifier-key delivery
after a VM window resize blocked native guest UI testing, and no provider test
turns had been sent. Later visible-interface testing resumed with working pointer
control; modifier combinations remained unreliable. No alternate GUI automation
was substituted. The fixture results above remain separate from the later actual
native/provider results below.

Local evidence is under
`.local/live-validation/20261103-025735/candidate-0.2.2-build4.json`,
`.local/live-validation/20261103-025735/guest-frozen-qa/startup-stack-diagnostic.json`,
`.local/live-validation/20261103-025735/guest-frozen-qa/result-0.2.2-build4.json`, and
`.local/live-validation/20261103-025735/guest-fixture-soak/`.
The completed soak's independent audit is
`.local/live-validation/20261103-025735/guest-fixture-soak/independent-final-review.json`.
Final artifact evidence is under
`.local/live-validation/20261103-025735/notarized-artifact-check-0.2.2/`.
Raw guest diagnostics, fixture data and logs remain local and are excluded from
source exports and private source backups.

## Actual native and provider checks on 2026-11-03

Downloaded 0.2.2 build 4 was installed through guest Safari/Finder and launched
through the normal downloaded-app Gatekeeper dialog. Cancel, relaunch and Start
Monitoring worked with all four optional features off. Usage trackers had no
previous reading with account reads off. Its setup bottom buttons were initially
outside the visible desktop; Escape and Return were verified workarounds.

| Actual interface workflow | Version and outcome |
| --- | --- |
| Two pilots per provider | Build 4 passed exact marker replies and visible file text. Byte/newline and unchanged-original-file checks remain unverified. |
| Exact chat links | Both providers passed on build 4 from visibly empty New chat views away from the target, returning to the matching prompt and reply. |
| Codex active turn, Stop and recovery | Build 4 moved one existing card Ready to Cooking with stale Ready removed; Stop reconciled to one Disconnected card. A new exact reply resurfaced the reviewed interrupted chat as one Ready card. |
| Reviewed history, restart and Restore | Build 4 retained the exact reviewed chat and appearance through native Quit/relaunch; Restore returned one card. |
| Claude input wait and answer | Build 4 detected the actual question-tool form as Needs you; returning and submitting an answer cleared the old flag/detail. Permission request was misleading secondary wording for a question. |
| Claude background failure | Build 4 incorrectly showed Ready while a native background sleep was still running. This actual failure led to a source fix. |
| Background fix re-tests | Build 6 passed natural background Cooking-to-Ready completion with an automatic exact final reply and killed-task-to-Disconnected reconciliation without a false completion marker. |
| Installed updates | Actual Finder Replace and Gatekeeper launch passed for 0.2.3 build 6 and 0.2.4 build 7. Applications Get Info confirmed each version, with existing state and appearance retained. These were updates, not fresh-state installs of those versions. |
| Setup reachability and Cancel | Build 6 fit the outer buttons but lower folder controls remained unreachable. Build 7's actual scrollbar reached both provider folder controls. A draft login toggle was canceled, reopening verified all four options off, and all-off Save passed without optional grants. This did not test login registration. |
| Build 7 question/answer | Actual Needs you, corrected Request details wording, pending-form return and stale-state clearance passed. The provider answer contained the marker plus extra text, so its strict exact-only output-format check did not pass. |
| Build 7 background lifecycle | Actual active background work showed one Cooking card with old Ready removed. Natural completion triggered a separate automatic exact final reply, then the same card returned Ready with prior detail gone. No manual follow-up or Stop was used. |
| Build 7 Review/restart/Restore | Actual Review removed the card; native Quit and Finder Applications relaunch retained appearance and the exact reviewed chat. Restore returned one card with the correct total and badge. No additional provider work was required. |
| About/privacy/support | Build 6 rendered local explanations and coffee support with the destination and QR. No external click, independent QR scan or network/privacy proof is claimed; build 7 parity of this page was not separately tested. |

The actual ledger conservatively counts 12 Claude model turns: 10 manual or
form-answer initiations and 2 automatic background-completion continuations.
Codex has 4, including an interrupted turn. The harness made no API-key calls
or credit purchases. Actual account readings/settings remain local; rounded
shared-account metrics include controller or other activity and cannot isolate
exact test cost. An unchanged rounded reading does not mean zero usage.

## Frozen 0.2.4 build 7 source and artifact scope

The backdated-transcript replacement stale-Ready failure was reproduced in a
source fixture. Its fix is now source/fixture-verified at frozen checkpoint
20261103T210740Z; that destructive history scenario was not exercised through a
real provider's native history. The separate evidence layers are:

| Evidence layer | Passed scope |
| --- | --- |
| Frozen source regression suite | 213 tests using fake files and local fixtures. |
| Independent source/lifecycle checks | 11 indexer, 9 monitor, 6 store, 5 compiled-native and 7 original regressions, plus a 64 MiB append fixture. |
| Focused Chrome UI fixture | 43 checks in 5.172 seconds using actual installed sandboxed Chrome, fake account features off and process cleanup. |
| Exact packaged collector fixture | 40 HTTP/SQLite/browser workflows using actual installed sandboxed Chrome through a test adapter with disposable data; not the native app. |
| Final artifact verification | 85 checks of candidate identity, complete payloads, signatures, hardened runtime, tickets, Gatekeeper and DMG integrity. |

Apple accepted the ZIP and DMG for private 0.2.4 build 7. The artifact's full
source export manifest SHA-256 is
`2bffd35c8d8374c9fbd00f7489242e72b0b7e9c0ef64f45d8bf1a74164caa6f2`.
Human documentation was updated after that frozen export; this doc-only update
does not change its runtime or artifact bytes. Signature/payload checks and
fixtures do not establish untested native or permission workflows. The completed
eight-hour build 4 soak above used fake data, not eight hours of real providers.

Before backup, two UUID literals in `tests/test_chat_links.py` lacked confirmed
synthetic provenance and were replaced with obvious synthetic identifiers. Only
test fixtures changed; the five focused chat-link tests passed. The accepted
runtime and packaged artifacts are unchanged. Sanitized backup source is therefore
not byte-identical complete build source: it combines the frozen runtime with
this reviewed test-only redaction and later documentation. The original manifest
digest is local build provenance, not the sanitized-source manifest. A separate
sanitized manifest must be recorded after export generation. Use an orphan
history and bundle for this new sanitized backup, excluding historical ancestry.
Older private checkpoints retain legacy fixture identifiers and remain preserved;
they must not be made public as-is. No history rewrite or deletion is authorized.
Unredacted source, tests, staged bundles and ancestry are not eligible inputs to
the new backup. Independent inspection verified the final app ZIP/payload has
neither this test fixture nor the legacy identifier values.

One serialized Claude JSONL record larger than 1 MiB is skipped to bound memory,
leaving completion unverified while that record remains. This is a per-record
limit, not total chat or attachment size. Embedded images or large tool outputs
can trigger it, and restart encounters it again. Independent live events may
still show Cooking or Needs you; final Ready cannot be certified. Navigation,
Reviewed and Snooze remain available by the reviewed source behavior. The actual
normal-image/oversized-record and long real-history/cold-disk workflows have not
passed. Continuously changing or ambiguous histories can also remain unconfirmed;
the indexing time budget is soft and checked between bounded operations.

Optional Keychain/account-usage collection, Claude project-coordinator duplicate
behavior and related permissions, Agent Board notifications and launch-at-login
registration remain unverified. No second-OS-user isolation or actual-provider
overnight soak passed. Native GUI Quit was observed, but helper-process cleanup
after those GUI quits was not independently inspected. Minimum-supported-macOS
and Intel runtime compatibility remain unverified. Agent Board was quit and the guest shut down normally after testing, with
reopening windows disabled; the VM was observed off. Temporary guest SSH
authorization was revoked and fresh login with that key failed. Remote Login
remains configured on and needs owner-controlled cleanup before VM reuse.

Raw observations, account readings, guest paths and credentials are excluded
from source exports and private human-summary backups. The candidate is for
private testing and backup only, with no public release or App Store readiness
claim.

## Remaining containment and release limits

Owned-process cleanup covers its process groups and same-group descendants. A deliberately malicious helper can create a separate session and escape that group. A detached-grandchild fixture confirmed this residual boundary, and test cleanup killed every fake PID. No such behavior was observed on the fixed read-only provider paths. Agent Board does not sandbox arbitrary compromised executables.

The selected actual workflows above do not establish every provider, permission,
installation or history path. Version 0.2.1 retains its known startup bug; later
builds have separate fixed-build evidence. Build 7 passed the setup scrollbar
and selected actual provider workflows, but the single-large-record completion
boundary, long real histories, optional permissions and fresh-state coverage of
the final build remain release gates. A rebuilt or newly signed artifact needs
its own artifact verification and relevant native re-tests. Distribution review
and provider-mark permissions are separate requirements. No public release or
App Store readiness is asserted.
