# Agent Board

The direct-download edition includes optional account-chat and account-usage readers. The sandboxed Store edition covers local Codex and Claude Code sessions and excludes those account readers. Store packaging and submission readiness are tracked in [APP_STORE_READINESS.md](APP_STORE_READINESS.md); older Store build records are historical.

Agent Board is a local Mac dashboard for Codex, ChatGPT, Claude Code and Claude
chats. It shows which chats are working, waiting for your input or
have a reply ready. Open returns to the original chat. Reviewed moves an item
into dashboard history until new activity without changing the source chat.

## Current direct build, November 12, 2026

The direct Mac candidate uses the app identifier `local.agentboard.display`.
This is distinct from the historical App Store candidate below. The release's
`BUILD-RECEIPT.json` records its exact version, build, signing, notarization and
validation results. `SOURCE-MANIFEST.json` identifies the reviewed source files.
It is not an App Store release.

The board reuses its database connection and skips unchanged writes. Codex index
reads also reuse a validated connection; when a read-only folder cannot support
SQLite sidecars, a bounded snapshot stays in memory rather than being copied to
disk each scan. Review and Snooze no longer wait for a full provider scan.
Measured resource checks are required in addition to the unit suite before release.

ChatGPT conversations now use exact IDs from OpenAI's recent-conversation list
and the desktop app's local catalog, with reply-status requests directly to OpenAI. Codex windows no
longer create additional ChatGPT cards. Older observations that could not verify
conversation identity are preserved locally outside conversation lists after a private local backup.
Usage percentages and credit balances display whole numbers. Claude account
access failures give actionable guidance, and explicitly disabled usage credits
are shown as turned off.

Usage requests back off after failures and respect the provider's retry delay.
The last successful reading remains visible with its age and retry state.
Healthy slower polling shows the reading's age and next scheduled check;
failed or overdue checks and expired reset windows remain marked stale.
Both providers use the same row sizing, including when one has an error.
Claude project status observation times are kept separate from message times,
so resuming work can clear an older attention state before a new reply appears.
Idle and resolved project threads check their newest replies with a bounded read
and verify the thread state again before reporting completion. The verified
assistant message owns the reply identity, so changing the thread state alone
cannot resurface a reviewed reply. Cached reply verification expires after one
minute. Older system notices cannot hide a recent reply behind the history limit.
Ordinary Claude chats use verified account metadata for both web and desktop
activity. The board follows exact conversation IDs while the browser is in the
background or the desktop app is showing another chat. Completion requires a
new settled reply marker and message identity; a rename cannot create a reply
alert. A private account-scoped checkpoint preserves Reviewed state across
restarts and catches replies that finish between checks. Existing chat cards
keep their identities and review state.
If Claude omits activity fields, the incomplete response cannot erase known
chats or create a completion. The prior verified reading keeps its original
expiry while the reader retries; a prolonged gap is shown as unavailable.

Conversation collectors retry after temporary storage or read failures. Connection
health expires when a collector stops refreshing, and a stalled refresh cannot
hold the dashboard request open indefinitely. Saved review state is retained.
Account readers also recover if a worker cannot start. Reply markers are committed
only when their lifecycle state is saved, and selecting an old ChatGPT branch
does not create a new reply alert. Usage values are rounded once when displayed.

Local transcript verification follows the verified content history when files
are replaced or reindexed. An unchanged replacement cannot turn an old reviewed
reply into a new alert; a changed transcript still requires verification.
Recently active Codex histories receive scan time while older histories continue
to make progress within the same read budget. A large history backlog cannot
consume every polling cycle before an active chat is checked.

Recent ChatGPT conversations are discovered directly from OpenAI even when the
desktop catalog has not refreshed. This is not an account-wide history export.
A Ready event uses the final assistant reply's verified identifier and
completion state. The bounded latest-turn check also catches replies that
finish between status polls. Title changes and old completed conversations do
not create new reply alerts. Account-scoped completion markers survive restarts.
Catalog-only chats remain accessible in Background with their status marked
unavailable. Claude discovery reads up to 100 recent conversations per check.
Both account readers require the corresponding account's existing sign-in on
this Mac; they do not depend on the active browser tab or Accessibility access.
Missing access remains visible rather than manufacturing completion.
Saved account-chat replies are checked against the current sign-in before they
can count as attention or send notifications. Replies from another sign-in stay
saved locally with an unconfirmed status; returning to their original
account restores their status without changing review history.

## Historical shipping candidate, November 6, 2026

This shipping-status snapshot ends on November 6, 2026 at 06:17 UTC.

The current shipping preparation is **0.2.5 build 27**, using the registered App
ID `com.spacelydigital.agentboard` and explicit US-only optional support. Both
application and installer signing completed, and the builder’s exact-package
checks passed. Independent verification of the exact signed app and installer
also passed. On macOS 26, Installer reported success and normal launch reached
initial Setup and the Codex read-only picker without a Gatekeeper override.
No source grant or provider workflow has been exercised yet. On macOS 13, the separate ad-hoc preview 27 passed bounded
no-source dashboard, local Privacy navigation, Light-mode Quit/reopen persistence,
Sample-board review/return and closed-window reopen checks. No connected-provider,
complete minimum-OS or signed-distribution pass follows from those checks.

The App Store Connect record now exists as **Agent Board: AI Reply Alerts**.
Draft version 0.2.5, the support URL and manual release were saved and verified
after reload; the subtitle and Developer Tools category were also saved and
verified. On November 6, Transporter Verify succeeded with warning 90889 and
Deliver completed at 01:07 local time. Build 27 was later selectable and its
attachment to version 0.2.5 was saved and reloaded, with status Missing Compliance.
The encryption question was canceled unanswered. This profile-free package is
not TestFlight-eligible; no overall processing/compliance, App Review or release
acceptance is claimed.
Submission readiness remains false. See
[packaging status](APP_STORE_PACKAGING.md) and
[remaining release gates](APP_STORE_READINESS.md). The following build 25
results remain historical evidence for that earlier preview.

## Preserved build 25 checkpoint

The preserved checkpoint is **0.2.5 build 25**, an Apple-silicon App Sandbox
engineering preview. Its tested app was ad-hoc signed and its installer unsigned.
At that checkpoint it was **not ready for App Store submission**. macOS 13 was
the deployment target; its actual minimum-OS installation/launch had not been
verified. Later preview 27 checks are scoped above; Intel support is not claimed.

All **412 source tests** passed. Independent package inspection and **54 checks**
under a derivative native sandbox parent are supporting evidence, separate from
actual user workflows. Build 25 fixes the installed build 24 startup-profile
rejection and recipient-inaccessible license folders.

The actual existing-grant upgrade resumed real monitoring with both sources
connected. Recipient license folders and Quick Look text were readable.
About/local Privacy/back, normal optional support handoff and presentation
persistence after Quit/reopen passed. Storefront country was not independently
read; negative non-US/offline conditions and payment were not tested.

A later build 25 run submitted two real provider prompts, one each in Claude
and Codex. Matching Ready cards, exact-chat handoff and Review/Restore were
observed. Codex also showed one Cooking card, completed while the board window
was closed, and its current notification opened the matching chat. Reviewed
state persisted after normal Quit and manual reopen. Claude Cooking and live
quota parity were not established; five-hour usage remained unavailable.

A separate recovery run used no new provider prompt. In-app Forget Source
Folders removed source connections from attention; reselecting the same three
previously approved roots restored monitoring and the existing Reviewed state.
This is app bookmark recovery, not macOS TCC revocation or a clean first-grant
test. About/Privacy/back links worked with Tab and Return. Final normal Quit and
orderly test-system shutdown were visually observed. Complete keyboard,
VoiceOver and process/network cleanup proof remain separate.

This source includes later shipping changes alongside the preserved build 25
history. The separate public build 25 snapshot and its verified local/private
backups remain unchanged; a final build 27 source/artifact backup is not claimed
here. Private repository ancestry, user data and raw validation evidence remain
excluded from clean source distribution. See
[APP_STORE_READINESS.md](APP_STORE_READINESS.md) for scope and release gates.

## Using the board

- **Cooking** means the source reports ongoing work.
- **Needs you** means an input or permission request is waiting.
- **Ready** means a reply is available, not that the project succeeded.
- **Disconnected** means current work cannot be confirmed. Last known replies
  can stay visible for review without entering the attention count.
- **Reviewed** moves the displayed event into history. Restore returns it.

Opening a chat does not mark it reviewed. New activity brings a reviewed chat
back. Review and Snooze use the displayed event revision so a stale card cannot
silence newer work. Internal workers and archived sessions stay in Background.
Names, placement, review state and appearance persist locally.

Appearance provides five layouts with Ledger as the default, and fifteen themes
with light and dark modes. View offers bounded text zoom, Keep on Top and Full
Screen. Effects respect Reduce motion and pause when web content is hidden;
background effects have no sound. Appearance offers ten original local alert
sounds and a Preview button. Sound on uses your selection for new Ready or
Needs you events. Native notifications require separate opt-in. VoiceOver,
actual sound output and minimized/closed-window energy use remain release gates.
If macOS rejects a notification request, Agent Board retries briefly while the
same event is still eligible. Accepted requests are not repeated. Reviewed or
changed events, disabled notifications and expired retries are discarded.

Close the window to keep monitoring from the menu bar. Quit stops the owned
collector. Setup and Permissions controls source folders, notifications and
opening at login. Retry Monitoring remains available after a collector failure.

Claude’s five-hour and weekly readings share one panel; Codex has a weekly
panel. Store builds use local cached observations and show age, stale and
unavailable states. The optional Claude plugin exports quota windows and reset
times locally. Real weekly readings were observed on earlier builds; the tested
account did not expose a five-hour window. Cached values do not confirm a fresh
allowance or the active account. Agent Board never sends a prompt to refresh them.

## Privacy and optional support

Agent Board has no developer-operated reporting endpoint, analytics SDK,
advertising tracker or crash uploader. It does not automatically send chats,
account details, usage statistics or diagnostics to Spacely Digital. Board data,
source-folder bookmarks, preferences and logs stay on the recipient’s Mac.
The Store package excludes the prototype’s saved-credential, cloud-project and
provider-command helpers. It reads selected local folders and authenticated
loopback traffic. The bounded Codex index fallback uses a verified in-memory
snapshot without temporary disk writes; see [PRIVACY.md](PRIVACY.md).

Agent Board is free. The default Store support setting is hidden. The explicit
US-only setting permits a bundled QR and fixed Ko-fi link only after the native
About page receives an eligible Apple StoreKit storefront result. Unknown,
non-US or unavailable results keep support hidden, and clicking rechecks
eligibility. The app does not persist or report the storefront. The QR is local.
It does not unlock features or verify donations. Normal support display and fixed-URL handoff were observed once. Independent
country readback and non-US/offline conditions remain unverified.

If you choose to email **Spacelydigital@gmail.com**, the mail app opens without
sending or attaching anything automatically. If you choose to visit or donate
through Ko-fi, that service may share your chosen contact and transaction details
with Spacely Digital. These voluntary flows are separate from board monitoring.
Apple’s system services and the provider apps operate under their own policies.

[Support](https://spacelydigital.github.io/agent-board-support/) and
[Privacy](https://spacelydigital.github.io/agent-board-support/privacy.html)
are live. Four website files returned HTTP 200 with the reviewed bytes;
independent source readback and actual desktop/narrow browser navigation passed.
The public privacy page includes StoreKit eligibility, voluntary support and
GitHub hosting-IP disclosures. The website contains no app source or account data.

## Development and distribution

Use disposable source roots for development and keep account readers off:

```sh
python3 server.py --port 8870 --data-dir .local/dev \
  --codex-root .local/fixtures/codex --claude-root .local/fixtures/claude \
  --no-usage --no-projects
python3 -m unittest discover -s tests
```

Prepare those fixture directories first. The port and state folder should differ
from the installed app. [APP_STORE_PACKAGING.md](APP_STORE_PACKAGING.md) describes
explicit local packaging inputs; the build tool does not install or upload.
[DISTRIBUTION.md](DISTRIBUTION.md) and [APP_STORE_READINESS.md](APP_STORE_READINESS.md)
separate source, fixture, native and actual-provider evidence. All prior private
checkpoints remain preserved. Delivered build 27 is attached to version 0.2.5
with Missing Compliance; no App Review or release result is established.

Agent Board is independent of OpenAI and Anthropic. Their names belong to their
owners; provider compatibility and distribution authorization remain release
review items. The direct prototype’s authenticated-provider path is separate
and is not cleared for distribution.

## License status

No open-source license has been selected for Agent Board's own code or artwork.
The notices in [THIRD_PARTY.md](THIRD_PARTY.md) and `licenses/` apply to their named
third-party components. They do not license Agent Board's code, name, artwork
or provider logos. This snapshot adds no license grant.
