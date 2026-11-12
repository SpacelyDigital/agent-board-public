# Real-environment validation plan

The owner approved this follow-up to the isolated hardening pass. Clean-VM
fixtures found a startup bug, and later actual native/provider tests found an
active-background lifecycle bug. Both have fixed-build evidence below, with
remaining release gates stated explicitly. Actual tests now total 12 conservative
Claude model turns and 4 Codex turns. Private version 0.2.4 build 7 is notarized
and has selected actual install/setup/provider/restart results below. The simulation phase used disposable provider records, fake
credentials and local HTTP fixtures with no model calls. The Codex conversation
doing this engineering uses normal allowance and shares the account counters;
its usage cannot be isolated from test usage through rounded displays alone.

## Execution checkpoint on 2026-11-03

The disposable Apple-silicon VM runs macOS 26.6.2 (25G83), with no active
Xcode/command-line developer tools or inherited Agent Board data at baseline.
The owner completed the VM account setup and reports signing in to both
provider desktop apps. Read-only checks verified the OpenAI desktop app
`com.openai.codex` version 26.1027.61615 build 24141 and Claude 4.4212.0;
both installed bundles passed deep strict signatures and Gatekeeper assessment.

Agent Board 0.2.1 build 3 has Developer ID signed, hardened-runtime ZIP and DMG
artifacts with accepted notarization and independent signature, ticket and
payload checks. Later guest testing found its frozen collector could hang in
Python HTTP-server reverse DNS during startup. Its private draft is now marked
with the known issue and is not a release candidate.

Version 0.2.2 build 4 removes the startup hostname lookup. Two added HTTP
regression tests and the complete 173-test source suite passed. The exact signed
candidate passed 34 frozen-executable checks in the macOS guest, including a
0.902-second healthy startup without separately installed Python, authenticated
loopback state, Review/Restore persistence, newer-turn protection and helper
cleanup. This used fake files, disabled account reads, separate CLI staging and
SSH stdin for parent lifetime. It did not test the actual Swift desktop owner,
native setup or provider integrations.

The signed candidate also passed 40 packaged HTTP/SQLite/WebKit checks, four
frozen reader-lifetime checks, seven parent-lifetime checks and native runtime
launch/authentication/persistence/restart/helper-cleanup fixtures. Apple accepted
both new 0.2.2 notarization submissions, and the pipeline validated the stapled
tickets. All 80 independent final-artifact checks passed for exact candidate
hashes, matching complete payloads, signatures, tickets, Gatekeeper and DMG
integrity. At that artifact checkpoint, these checks did not establish native UI
or provider workflows; later actual results follow below.

The eight-hour fixture soak completed on November 3 from 04:41:18 to 12:41:19 EST,
covering 28,800.6 seconds, 477 samples, 24 fake lifecycle cycles, eight restarts
and nine successful collector starts/stops. All 96 sampled socket/helper checks
showed only the loopback listener and no unexpected direct helpers. No failure
events were recorded. Peak resident memory was 41,120 KiB, and the largest
sample gap was 66.4 seconds. Independent ledger review passed all 20 consistency
checks. Periodic restarts, sampled observations and fake inputs limit this proof
to the frozen collector fixture workflow; it made no model calls or account reads.

The initial 0.2.1 Safari DMG transfer stopped at 358 KB. A retry completed, and guest
metadata confirmed the expected 12,349,613-byte file size. Guest Gatekeeper
accepted it as Notarized Developer ID. macOS Downloads privacy controls blocked
SSH full-file and extended-attribute reads. The downloaded copy's hash,
quarantine, download-origin metadata and direct signature checks are therefore
unverified. This initial transfer does not establish a successful quarantined
launch; later versions have separate actual Finder/Gatekeeper results below.

At the earlier fixture checkpoint, pointer and modifier-key delivery after a
VM window resize blocked native UI testing and no provider turns had been sent.
Later visible-interface testing resumed with working pointer control; modifier
combinations remained unreliable. No alternate GUI automation was substituted.
The historical fixture facts above are not replaced by the actual tests below.

## Actual native/provider checkpoint on 2026-11-03

Safari/Finder installation and normal downloaded-app Gatekeeper launch passed
for 0.2.2 build 4. Native Cancel, relaunch and Start Monitoring worked with all
four optional features off. Actual Finder updates to notarized 0.2.3 build 6 and
0.2.4 build 7 retained existing cards and appearance. Applications Get Info
confirmed the versions. Those were updates, not fresh-state installs of the
later builds. Downloaded guest-copy hashes and quarantine metadata were not
independently read back; actual Gatekeeper launch is a distinct observation.

| Actual workflow | Recorded outcome |
| --- | --- |
| Two pilots per provider | Build 4 passed exact replies and visible file markers; byte/newline and unchanged-original-file checks remain unverified. |
| Exact chat navigation | Both providers passed from visibly empty New chat views away from the target, returning to the matching prompt and reply. |
| Codex active/Stop/recovery | Build 4 cleared stale Ready when the card moved Cooking, reconciled native Stop to Disconnected, then resurfaced the reviewed interrupted chat after a new exact reply. |
| Review/restart/Restore | Build 4 passed; build 7 independently retained appearance and the exact reviewed chat through native Quit and Applications relaunch, then Restore returned one card and the correct badge. Helper termination after these GUI quits was not independently inspected. |
| Claude background failure and repair | Build 4 showed Ready during active background work. Build 6 passed natural completion and killed-task reconciliation. Build 7 separately passed actual Cooking-to-Ready natural completion with a separate automatic exact final reply. |
| Build 7 question/answer | Actual wait detection, corrected Request details wording, pending-form return and old-state clearance passed. That provider answer included extra text, so strict exact-only response formatting did not pass. |
| Setup lower controls | Build 6 lower controls were unreachable. Build 7 actual scrollbar drag reached both folder controls; Cancel discarded a login-toggle draft, reopening showed all four options off and all-off Save passed without optional grants. Login registration was not tested. |
| Local About/privacy/support | Build 6 rendered explanations, coffee destination and QR. No external click, QR scan, network/privacy proof or separately checked build 7 parity is claimed. |

The conservative count is 12 Claude model turns: 10 manual/form initiations plus
2 automatic background-completion continuations. Codex has 4, including an
interrupted turn. No API-key calls or credit purchases were made by the harness.
Actual account readings and settings stay local. Rounded shared-account metrics
include controller or other activity and cannot establish exact test cost; an
unchanged rounded reading does not mean zero usage.

The backdated-transcript replacement stale-Ready fix is source/fixture-verified
at frozen checkpoint 20261103T210740Z, not a destructive native-history pass.
The distinct evidence layers are:

| Evidence layer | Passed scope |
| --- | --- |
| Frozen source regression suite | 213 tests using fake files and local fixtures. |
| Independent source/lifecycle checks | 11 indexer, 9 monitor, 6 store, 5 compiled-native and 7 original regressions, plus a 64 MiB append fixture. |
| Focused Chrome UI fixture | 43 checks in 5.172 seconds using actual installed sandboxed Chrome, fake account features off and process cleanup. |
| Exact packaged collector fixture | 40 HTTP/SQLite/browser workflows using actual installed sandboxed Chrome through a test adapter with disposable data; not the native app. |
| Final artifact verification | 85 checks of candidate identity, complete payloads, signatures, hardened runtime, tickets, Gatekeeper and DMG integrity. |

Apple accepted both final 0.2.4 build 7 artifacts. Their source export manifest
SHA-256 is
`2bffd35c8d8374c9fbd00f7489242e72b0b7e9c0ef64f45d8bf1a74164caa6f2`.
These human documentation updates are separate from that frozen artifact source;
no runtime or artifact bytes were changed. The earlier eight-hour build 4 soak
used fake data and does not establish an actual-provider overnight soak.

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

The candidate is for private testing/backup only. No public or App Store release
was performed. The stages below remain the original checklist; only bounded
outcomes above are marked passed, with other targets explicitly unverified.

## Original planning estimates

The certificate, notarization and VM setup exceeded the original estimates.
The ranges below are retained as the original plan, not measured completion
times or a renewed completion promise. Actual stage times should be reported
only where the execution record supports them.

For the first real Mac/OS pass, reserve about **two hours of exclusive desktop
control**, with a practical range of **1.5 to 2.5 hours**, plus **15 to 20 minutes
with you present** for sign-in, Keychain and any administrator prompts. Total
elapsed time including signing and Apple's notarization queue is likely **two
to four hours**. These are planning estimates, not measured guarantees.

An **eight-to-twelve-hour soak** follows in the background without extra model
prompts. You can use the Mac during that soak. Testing the minimum claimed OS
or another Mac adds roughly one to two hours per environment; most of that
should happen on a separate test machine or guest desktop.

A VM only avoids disrupting your Mac if it has its own independently controlled
desktop. Driving a VM window with the host mouse still requires you to leave the
host keyboard and mouse alone during automation. A separate test Mac is best.
Downloading, building, analysis and notarization waiting do not need exclusive
control of your screen. We can split the exclusive portion into shorter blocks.

## Stages and acceptance checks

| Stage | Planned time | New provider model turns | Your involvement |
| --- | --- | --- | --- |
| Signing and preparation | 15–45 minutes, with an unpredictable Apple queue | 0 | About 15–20 minutes for account/certificate/sign-in prompts; background work afterward |
| Clean install and first-open permissions | 25–40 minutes | 0 | Exclusive test desktop; help only if a password is required |
| Small usage pilot | 10–15 minutes | 2 per provider | Exclusive test desktop; no other account work during quota measurement if practical |
| Live chat lifecycle and deep links | 30–45 minutes | Up to 10 more per provider | Exclusive test desktop; occasional provider approval or destination confirmation |
| Privacy, account isolation and recovery | 20–30 minutes | 0 planned | Exclusive test desktop, using the existing test chats and synthetic canaries |
| Quit, restart, login, sleep and upgrade | 15–25 minutes | 0 planned | Exclusive desktop; login may require your presence |
| Background soak | 8–12 hours | 0 extra prompts | Normal computer use is fine |

### 1. Prepare an isolated test Mac

Use a spare Apple silicon Mac, or a fresh macOS guest/user with no personal
repositories, source checkout, Python installation, inherited Agent Board grants
or unrelated automations. Keep your current installed board and data intact.
Use your ordinary test-provider sign-ins only after approving the live phase.
Never copy owner credentials, Keychain databases or personal chats into fixtures.

Install a Developer ID Application certificate locally through Xcode, configure
notarization credentials in Keychain, then sign the exact tested build, submit
it to Apple, staple the tickets and record artifact hashes. Your paid developer
membership alone does not install the certificate. Do not send private keys or
passwords through chat or put them in GitHub.

Test a real browser-downloaded, quarantined DMG with Gatekeeper enabled. Drag it
to Applications and launch through Finder, then test offline relaunch with the
stapled ticket. Verify it needs no Terminal command, host Python or source files.

### 2. Exercise first-open choices before real account reads

Start with local monitoring, optional account reads, Claude projects,
notifications and login choices in their default states. Confirm no collection
before Start Monitoring and no automatic account feature opt-in. Test Cancel,
missing provider apps, a blocked source folder, allowed folder access, and a
missing/denied Claude Keychain item. Denial must preserve the local board.

Exercise notification allow, deny, disable and late authorization completion.
Verify no backlog notification, one alert per new event, Open and Snooze,
background exclusions and a newer event escaping an older snooze.
Only grant the named Claude Safe Storage item after choosing that explicit
button. You handle password or account-authentication dialogs yourself.

### 3. Measure two short turns before setting the final usage budget

Record provider model/effort/speed, plan, five-hour and weekly percentages and
reset times. Use empty disposable workspaces and fresh short conversations.
Send two small test turns per provider, record local usage data where available,
then inspect the usage counters again. Avoid crossing a reset boundary.

If counters are too coarsely rounded or unrelated account work is happening,
do not pretend that a precise percentage can be inferred. Report the observed
delta and keep the turn cap. Review the pilot before continuing if its cost is
unexpectedly high. Do not retry a test indefinitely to make a counter move.

### 4. Run the real provider workflows with a bounded turn count

The initial budget is **8 to 12 short turns per provider**, including the pilot,
not hundreds of model requests. Across Claude and Codex that is 16 to 24 turns.
Reserve at most four additional turns per provider for an approved reproduction
or second-environment smoke. Do not exceed **16 per provider** without another
budget decision. These are user turns; a coding agent may internally make
several inference requests during one turn. The actual checkpoint above counts
a native form answer and an observed automatic notification continuation
conservatively as separate model turns.

Use standard speed and a lower-cost suitable model available on the account,
small outputs and tiny local tasks. No repository-wide analysis, image/video
generation, web research, autonomous subagents or repeated long-running agents.
Do not use API keys, purchase credits or enable paid overage. If the account
already permits automatic overage, resolve that setting before the live phase.

Spread the turns across three to five disposable chats and cover: Cooking to
Ready; a same-chat follow-up; a safe command permission allowed and denied;
a user-input wait; interruption and recovery; concurrent chats; and a Claude
project coordinator/task handoff without duplicate cards. Use the test
workspace only. Open each row and verify the exact destination. Review, Restore,
rename, move, snooze, preferences and most restart checks reuse existing test
states and do not require another prompt. Where the automation surface cannot
inspect Codex's own UI, you confirm the destination rather than treating a
successful URL-open command as proof.

There is no honest pre-test promise that this consumes a particular percentage
of a subscription. OpenAI says task size, model, context, reasoning, tools and
caching affect usage. Anthropic also ties usage to conversation size, model,
features and effort, with Claude product surfaces sharing the allowance.
The pilot establishes the account-specific estimate. As an additional proposed
stop rule, check after each live turn and pause at five percentage points of
consumption in any relevant window; rounded counters can overshoot by one turn.
This threshold is a safety limit for the proposed run, not a prediction of cost.

Sources: [official OpenAI usage guidance](https://learn.chatgpt.com/docs/pricing#what-are-the-usage-limits-for-my-plan),
[Claude usage limits](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work).

### 5. Verify privacy on the actual recipient environment

Observe Agent Board and every helper process at the isolated VM/network
boundary. Use connection metadata rather than capturing credentials or
intercepting real TLS payloads. With account features off, investigate every
non-loopback request. With account reads explicitly on, document the provider
requests and confirm no prompt/model endpoint or developer telemetry endpoint
is called by Agent Board. Keep provider-app background traffic separate.
The Ko-fi link should produce traffic only after the explicit link action.

Create a second actual macOS user and seed synthetic, distinguishable canaries.
Verify the other user cannot read the first user's data, token files or private
loopback state. Fixture-only same-user directory isolation is supporting evidence,
not a substitute for this OS permission test. Never use personal secrets as
canaries. Board data, preferences, logs and recovery copies must stay in that
recipient's local Library; nothing is uploaded to the developer.

Account usage/projects are optional provider network reads, so enabling them is
not an entirely offline mode. They use the user's provider sign-in and do not
send local transcripts or board data to the developer. No analytics, crash
uploads, licensing backend or remote board service is part of this build.

### 6. Test native failure and maintenance paths

Force-quit the native app during a harmless active test read. Its collector,
readers and watchers must stop. Restart must create one owned collector and
retain board state. Check port collision recovery, app-window close/reopen,
sleep/wake, network offline/online, provider signed-out states, login enable and
disable, upgrade over the previous build and clean removal.

Corruption/disk-full/permission tests stay on disposable board copies even in
this phase. A corrupt database must retain its originals and show its recovery
notice. A busy or full disk must not be misclassified as corruption or silently
reset the board. Verify no chat is acknowledged twice or hidden by a stale event.

Run a quiet overnight soak. Measure CPU, memory, process counts, log size and
source refresh delays, including background animation pause. Do not generate
new model work to keep the test active. On a minimum-OS environment, repeat
install/permission/lifecycle smoke; reserve additional provider turns only if
needed and inside the approved cap.

## Release decision

The real pass requires zero unresolved data-disclosure, unauthorized activity,
acknowledgement-loss or persistent-lifecycle failures; all promised installation
and permission paths must pass on a fresh signed environment. Record exact
macOS/provider versions, artifact hashes, turn counts, measured usage deltas,
failures and rerun evidence. Any untested scenario remains explicit.

App Store submission is a separate decision. The current app is an unsandboxed
Developer ID download architecture that reads other apps' local records and
uses a private provider project API. It is not ready for App Store submission
just because functional tests pass. Apple's Mac App Store rules require
appropriate sandboxing, and its software rules restrict API use. Review source
access, entitlements, helper execution, provider authorization, marks and payment
rules before deciding whether an App Store variant is feasible.
[Apple App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/#software-requirements).

A private beta on a few consenting testers' own machines should follow the real
pass. The app must not automatically send their logs or chats back. Any report
is a deliberate, redacted communication they choose to send. No App Store
upload, public release or tester outreach is performed by this plan.

## Disk-write release gate

Use process-level `proc_pid_rusage` disk-write counters for the actual final collector. Run a disposable 200-session idle fixture for 120 seconds and require less than 10 KiB/s after warmup. Confirm repeated unchanged observations do not add SQLite changes or WAL bytes. In the sandbox, confirm a cleanly closed Codex index is loaded into a verified in-memory snapshot once and reused across 30 unchanged scans without snapshot disk writes, with invalidation on database or WAL changes.

Measure one hour of normal use below 50 MiB written with Codex open, then repeat with Codex closed. Record process identity, uptime, elapsed time, read/write deltas and source-health failures. Check macOS DiagnosticReports after 24 hours for new collector disk-write reports. A short fixture pass does not complete these live gates. Preserve the previous installed app and a consistent board backup before replacement.
