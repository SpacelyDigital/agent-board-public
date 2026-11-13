# Mac distribution

Full build 68 is the current verified Mac and GitHub release. Store build 70 is
a processed engineering upload with a smaller feature set. For the next full
release, allocate one version and build number, freeze source once, and bind the
install, GitHub artifacts and remote deployments to its release receipt. Verify
each target after deployment before recording parity. TestFlight has its own
Apple signing and delivery evidence and must meet the same feature acceptance.

The direct download builder requires `--version` and `--build-number`. It checks
both the app and collector identity before packaging and writes to build-specific
paths. A local preview or a different feature set is recorded separately from
the full release.

## Historical Store packaging

The preserved Store engineering candidate is **0.2.5 build 25** with explicit
US-only optional support. It is an ad-hoc signed Apple-silicon app and unsigned
installer, not a final Store package. Submission readiness is false. The owner
approved a free launch; optional donations do not unlock features.

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

## Shipping preparation, November 6, 2026

This shipping-status snapshot ends on November 6, 2026 at 06:17 UTC.

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
[release gates](APP_STORE_READINESS.md). Build 25 evidence above remains separate.

## Local preview build

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
scope does not describe later build 27 runtime changes. It includes no historical
private repository ancestry. Publication of source is not an App Store release or a new
license grant. No open-source license has been selected for Agent Board itself.
Keep `THIRD_PARTY.md`, `licenses/` and provider-icon notices unchanged.

Public source/artifact preparation must exclude recipient chats, account files,
bookmarks, credentials, keys, guest files, raw logs, screenshots and private QA.
Regenerate the public source manifest/archive after approved documentation
changes; do not reuse the older private source ZIP as if its bytes were current.
