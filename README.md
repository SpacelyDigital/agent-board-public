# Agent Board

Agent Board is a local Mac dashboard for chats in the Codex Mac app and Claude
Desktop’s Code tab. It shows which chats are working, waiting for your input or
have a reply ready. Open returns to the original chat. Reviewed moves an item
into dashboard history until new activity without changing the source chat.

## Shipping candidate, November 6, 2026

The current locally signed candidate is **0.2.5 build 28**, using the registered
App ID `com.spacelydigital.agentboard`, the profile-free unrestricted route and
explicit `us-external` optional support. Apple Distribution application signing,
Mac Installer Distribution signing, builder checks and independent exact-package
verification passed. The signed package is 10,839,062 bytes. Exact hashes and
scope are recorded in [ENGINEERING_CHECKPOINT.json](ENGINEERING_CHECKPOINT.json).

All **427 combined source tests** passed with no failures, errors or skips.
These source and static package checks do not establish installed behavior.
The bounded actual native snapshot ends at `2026-11-06T09:46:10.517577+00:00`.
On macOS 26, Installer reported success and the installed Info.plist view showed version
0.2.5 build 28. Startup waited beyond the old timeout. After the owner allowed
the macOS data prompt, the live board recovered without Retry or source
reselection. The GUI does not establish child, token or generation identity.

Ten recorded Preview-button actions produced no visible error. Fart with Sound
on persisted through normal Quit and Finder reopen. Generic test notifications
were visible after explicit sends with Sound on and Sound off. Physical sound,
audible mute and the selected notification sound file were not verified.
Actual build 28 provider transitions, login/restart and macOS 13 remain unverified.
Build 28 has not undergone
Transporter Verify or Deliver, Apple upload, TestFlight, notarization, App Review
or release. Submission readiness remains false.

Build 28 keeps the collector running after the original 100 health attempts and
shows a waiting state while initial source validation and scanning finish. It
uses a monotonic 300-second hard deadline, one child/token/generation and
serialized authenticated health checks. The deadline is cooperative on the
main queue and cannot forcibly interrupt synchronous native bookmark or file
operations. Controlled source/native checks support this change; they are not
packaged startup or macOS permission proof. See
[remaining release gates](APP_STORE_READINESS.md).

## Historical build 27 shipping checkpoint

The following build 27 status was recorded on November 6, 2026 at 06:17 UTC.
Its application/installer signing, builder checks and independent exact-package
verification passed. On macOS 26, Installer reported success and normal launch
reached initial Setup and the Codex read-only picker without a Gatekeeper
override. That snapshot included no source grant or provider workflow.
A later build 27 login/permission run timed out; allowing the late macOS consent
and choosing Retry recovered monitoring. Its cause was not traced and remains a
historical failure. These observations do not establish build 28 outcomes.

The App Store Connect record exists as **Agent Board: AI Reply Alerts**.
Draft version 0.2.5, the support URL, manual release, subtitle and Developer Tools
category were saved and verified after reload. Historical build 27 Transporter
Verify succeeded with warning 90889; Deliver completed, and build 27 was saved
and reloaded as attached to version 0.2.5 with Missing Compliance. The encryption
question was canceled unanswered. This history does not describe build 28 Apple
verification, delivery or attachment. The profile-free route is not TestFlight
validation, and no overall processing/compliance, App Review or release
acceptance is claimed.

On macOS 13, the separate ad-hoc preview 27 passed bounded no-source dashboard,
local Privacy navigation, Light-mode Quit/reopen persistence, Sample-board
review/return and closed-window reopen checks. Connected-provider, complete
minimum-OS and signed-distribution acceptance remain open. See
[packaging status](APP_STORE_PACKAGING.md). The following build 25 results
remain historical evidence for that earlier preview.

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
backups remain unchanged. This checkpoint does not establish a final build 28
source/artifact backup. Private repository ancestry, user data and raw validation evidence remain
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
sounds and a Preview button: soft-ding, chime, bell, glass, marimba, arcade,
radar, space-signal, pop and fart. Sound on uses your selection for new Ready or
Needs you events; Sound off mutes both web and native alerts. The selected sound
persists locally. Native notifications require separate opt-in. VoiceOver,
actual sound output and minimized/closed-window energy use remain release gates.

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
loopback traffic. The bounded Codex index fallback uses an unnamed private
local copy; see [PRIVACY.md](PRIVACY.md).

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
checkpoints remain preserved. Historical delivered build 27 is attached to
version 0.2.5 with Missing Compliance. No build 28 Apple verification, upload,
App Review or release result is established.

Agent Board is independent of OpenAI and Anthropic. Their names belong to their
owners; provider compatibility and distribution authorization remain release
review items. The direct prototype’s authenticated-provider path is separate
and is not cleared for distribution.

## License status

No open-source license has been selected for Agent Board's own code or artwork.
The notices in [THIRD_PARTY.md](THIRD_PARTY.md) and `licenses/` apply to their named
third-party components. They do not license Agent Board's code, name, artwork
or provider logos. This snapshot adds no license grant.
