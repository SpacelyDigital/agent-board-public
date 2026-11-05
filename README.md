# Agent Board

Agent Board is a local Mac dashboard for chats in the Codex Mac app and Claude
Desktop’s Code tab. It shows which chats are working, waiting for your input or
have a reply ready. Open returns to the original chat. Reviewed moves an item
into dashboard history until new activity without changing the source chat.

## Current engineering candidate

The current source snapshot is **0.2.5 build 25**, an Apple-silicon App Sandbox
engineering preview. Its tested app was ad-hoc signed and its installer unsigned.
It is **not ready for App Store submission**. macOS 13 is the deployment target;
actual installation and launch on macOS 13 and Intel support remain unverified.

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

This public snapshot comes from the reviewed clean build 25 source, with current
nonruntime documentation. It does not publish the older private repository's
history, user data or private validation evidence. Local and private source/
artifact backups were independently verified and remain separate from release
approval. See [APP_STORE_READINESS.md](APP_STORE_READINESS.md) for scope and gates.

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
background effects have no sound. Sound on plays a short chime for new Ready or
Needs you events. Native notifications require separate opt-in. VoiceOver,
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
checkpoints remain preserved. No final Store upload or release has occurred.

Agent Board is independent of OpenAI and Anthropic. Their names belong to their
owners; provider compatibility and distribution authorization remain release
review items. The direct prototype’s authenticated-provider path is separate
and is not cleared for distribution.

## License status

No open-source license has been selected for Agent Board's own code or artwork.
The notices in [THIRD_PARTY.md](THIRD_PARTY.md) and `licenses/` apply to their named
third-party components. They do not license Agent Board's code, name, artwork
or provider logos. This snapshot adds no license grant.
