# Mac distribution

The current local Store candidate is **0.2.5 build 28**, Apple Distribution
signed with a Mac Installer Distribution package, the profile-free unrestricted
route and explicit `us-external` optional support. Its builder and independent
exact-package checks passed. The package is 10,839,062 bytes with SHA-256
`ef254f3a65dd67be2b96f1288b4c2cac229f36f4516f00697f587cb9bb85bfcb`.
The bounded actual native snapshot ends at `2026-11-06T09:46:10.517577+00:00`.
The macOS 26 Installer reported success and the installed Info.plist view showed version
0.2.5 build 28. Initial waiting continued beyond the old timeout; after owner
consent, the connected live board recovered without Retry or source reselection.
The GUI does not establish child/token/generation identity.

Ten recorded Preview-button actions produced no visible error. Fart with Sound
on persisted across normal Quit and Finder reopen. Generic test notifications
were visible after explicit sends with Sound on and Sound off. Physical sound,
audible mute and the selected notification sound file were not verified;
actual build 28 provider transitions, login/restart, macOS 13 and full native
acceptance remain open. No build 28 Transporter Verify/Deliver, Apple upload,
TestFlight, notarization, App Review or release is established. Submission
readiness is false. The owner approved a free launch; donations unlock nothing.

All 427 combined source tests passed with no failures, errors or skips. The
package audit checked 118 app entries, nine compiled Python modules, 33 mapped
helper resources and ten main PCM sounds against 96 frozen runtime/build inputs.
The frozen source has 180 selected files plus its generated manifest. These
source/static checks do not replace installed user workflows. See
[APP_STORE_READINESS.md](APP_STORE_READINESS.md) for exact scope and remaining gates.

Build 28 keeps initial scanning active after the original health-attempt limit,
with a waiting state and monotonic 300-second deadline. The deadline cannot
forcibly interrupt synchronous native calls. Controlled delay checks passed;
complete macOS permission recovery testing remains open. The ten local sound choices
use matching source/helper/main WAV bytes, the existing preference bridge and
Sound off for native/web mute. WebKit checks passed; native audibility remains
an installed-build gate.

## Preserved build 25 evidence

All 412 source tests passed before freezing 85 runtime/build inputs and 163 source
files. Build 25 fixes the actual 24 startup rejection and recipient-inaccessible
license folders. Its fresh builder output and root-owned package BOM show
readable licenses. Independent package inspection and 54 valid-native-parent
startup/isolation checks passed. The actual existing-grant upgrade resumed real
monitoring with native attention 4 and both sources connected, opened ordinary
license notices, and passed About/local Privacy/back, normal support URL handoff,
Quit/manual reopen and visual final shutdown. Later runs used two real provider
prompts in total and verified exact handoffs, Codex closed-window completion/
notification, Review/Restore and persistence. A separate no-prompt run recovered
the same previously approved source selections and exercised About/Privacy/back
with Tab/Return. Country/negative-storefront, clean first-grant and full
accessibility remain unverified. See [APP_STORE_READINESS.md](APP_STORE_READINESS.md)
for exact hashes and scope limits.

The app bundles its Python runtime and interface resources. Recipients should
not install Python, Xcode or command-line tools. Apple silicon/macOS 13 is the
target. A separate ad-hoc preview 27 passed bounded no-source, navigation,
presentation persistence, Sample-review/return and window-reopen flows on macOS 13.
Connected/full minimum-OS and signed-distribution workflows remain unverified;
Intel support is not claimed.

## Historical build 27 shipping preparation

The following snapshot ends on November 6, 2026 at 06:17 UTC and describes
build 27 only.

Build 27 uses the registered release App ID
`com.spacelydigital.agentboard`; application/installer signing and builder package
checks and independent exact signed app/installer verification passed.
App Store Connect record 6831478051 exists as **Agent Board: AI Reply Alerts**;
draft version 0.2.5, support URL, manual release, subtitle and category were
verified after save/reload. Owner-authorized Transporter Verify and Deliver
completed on November 6 with sole warning 90889. Build 27 is saved/reloaded as
attached to version 0.2.5, with Missing Compliance; the encryption dialog was
canceled unanswered. Installer on macOS 26 reported success; normal launch
reached Setup and the Codex read-only picker, with no grant yet. No provider,
full minimum-OS, overall processing/compliance or App Review pass
is claimed; SDK/export and remaining listing fields stay open.
See [packaging status](APP_STORE_PACKAGING.md) and
[release gates](APP_STORE_READINESS.md). Build 25 evidence above remains separate. A later actual build 27 login/permission
timeout recovered after late macOS consent and manual Retry. Its cause was not
traced; it remains a historical failure rather than build 28 native proof.

## Preserved local preview recipe

Use the pinned portable build environment, then select a new output directory:

```sh
python3 bootstrap_build.py
.local/build-venv/bin/python build_store.py --preview \
  --bundle-id local.agentboard.store-preview --version 0.2.5 \
  --build-number 25 --support-strategy us-external --output build/store-build25
```

A preserved output directory must not be overwritten. The actual 25 recipe used
the pinned Python 3.12.16 toolchain. The bootstrap can download build tooling;
it is a developer step and is not part of recipient installation.

The default support strategy is `hidden`. Explicit `us-external` permits only a
native, fresh eligible-US storefront check to reveal/open the fixed Ko-fi link
and local QR. The observed normal support display/handoff does not establish negative
storefront conditions or App Review acceptance. The tool builds locally and never installs or uploads.

For a distribution build, provide the final registered identifier and explicit
application/installer identities, team and reviewed provisioning route as
specified in [APP_STORE_PACKAGING.md](APP_STORE_PACKAGING.md). Do not use a
Developer ID identity for a Mac App Store package. Any profile-free route must
remain explicitly selected and validated against the actual capabilities;
TestFlight eligibility is separate.

## Package and recipient checks

Inspect the complete app and expanded installer for exact bytes, Unix types,
file/directory modes, internal links, entitlements, signature chains, privacy
manifests and allowed resources. Check intended recipient access as well as
original/payload equality. The 24 audit preserved 0700 root-owned license folders
exactly, and the recipient could not read them. Build 25 adds a failing guard
for that condition and stages only public license copies as 0755/0644 before
signing. Do not chmod a signed app or installed user data to hide a build defect.

Require the actual packaged helper to start under its valid native parent with
account reads off, authenticate health, stop cleanly and preserve isolation.
Launching an inherited-sandbox helper directly can abort before Python and does
not establish the installed workflow. A derivative native fixture also does not
replace the final app’s installation or GUI tests.

Before release, install the exact final artifact as an ordinary recipient and
verify setup, source choice/forget/regrant, unreadable-source recovery, real
provider changes and links, Review/Restore, notifications, login choice,
restart, window closure, Quit, privacy/support and license access. Test macOS 13,
VoiceOver, enlarged text, effects and normal keyboard operation separately.
Record actual results without promoting previous-build or fixture success.

## Data and optional external services

The Store archive excludes the prototype’s saved-sign-in, provider-command and
cloud-project helpers. It has no developer-operated reporting or telemetry
endpoint. Board state, bookmarks and logs stay local. Quotas use local cached
observations and the optional quota-only Claude plugin; they may be unavailable
or stale. Monitoring does not send model prompts.

Apple StoreKit may process a current-storefront request while About is active
and again on a support click. The app does not persist/report its result. The
QR is bundled and has no remote image service. A user-chosen mail or Ko-fi
visit/payment can share their selected contact/message/transaction information
with those services and Spacely Digital. No board or log is attached.
The approved support email is **Spacelydigital@gmail.com**.

[Support](https://spacelydigital.github.io/agent-board-support/) and
[Privacy](https://spacelydigital.github.io/agent-board-support/privacy.html)
are live. Four website files returned HTTP 200 with the reviewed bytes;
independent source readback and actual desktop/narrow browser navigation passed.
The public privacy page includes StoreKit eligibility, voluntary support and
GitHub hosting-IP disclosures. The website contains no app source or account data.

## Clean checkpoint and backups

The build 25 clean source and six-asset local/private backups are complete and
independently verified. Source/archive bytes, restored app types/modes/links,
signatures and prior private checkpoints were checked. Those backups are the
closed first-phase snapshot; later validation does not rewrite them.

The preserved public build 25 source snapshot used that reviewed clean build 25
source with publication-time nonruntime documentation. That documentation-only
scope does not describe later build 27 or 28 runtime changes. It includes no historical
private repository ancestry. Publication of source is not an App Store release or a new
license grant. No open-source license has been selected for Agent Board itself.
Keep `THIRD_PARTY.md`, `licenses/` and provider-icon notices unchanged.

Public source/artifact preparation must exclude recipient chats, account files,
bookmarks, credentials, keys, guest files, raw logs, screenshots and private QA.
A build 28 source export, archive and remote backup have not been established by
this checkpoint. Regenerate the clean source manifest/archive only after approved
documentation changes; do not reuse the older private source ZIP as current.
