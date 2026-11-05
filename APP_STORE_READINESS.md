# Mac App Store readiness

Status on November 5, 2026: **not ready for submission**. The owner approved a
free launch with optional Ko-fi support. No App Store upload, release approval
or successful final signed package is established.

## Exact current candidate

The current engineering preview is 0.2.5 build 25, explicit `us-external` support,
Apple silicon and a macOS 13 deployment target. Its app is ad-hoc signed and its
installer is unsigned. The final product identifier remains unverified.

| Artifact | SHA-256 |
| --- | --- |
| Main executable | 0741bf90dd144df51551b0250c891c3609bcf3c1e2f59cedcdef177519c9eaf5 |
| Packaged helper | 794ff04ff74bb61f24c7b25d34075dc54135477e7444490ff749cd87aaa2cc1d |
| Unsigned package, 10,363,593 bytes | 2d3a4531e16986adf56730bdd5418e915998ebaefe9b26bebcc52e0b2beac75e |
| Builder checks | 15a72a976d8519f21c9df966147f383394b2428057b924223829c99ea0ca1a0c |

The candidate freezes 85 runtime/build inputs and 163 allowlisted source files.
The full source suite passed 412 tests in 14.434 seconds. Fifteen focused checks
and all 40 tests in the two affected modules passed. These are supporting tests,
not installed-app proof.

## Why build 24 cannot ship

Installed build 24 reported that the packaged collector distribution profile was
invalid. Exact archived entry code and the helper’s signed metadata reproduced
the rejection: the entry allowed only hidden support, while the reviewed build
explicitly enabled US-only support. This happened before monitoring or account
helper dispatch. A standalone helper launch outside its valid native-parent
context aborted before Python and is not treated as product startup proof.

The same install also denied the recipient access to bundled license notices.
The package BOM recorded both license directories as root-owned 0700. Their
423,155 content bytes exactly matched the Finder size deficit. The test user
actually received Finder’s permission-denied alert. The package contained the
files, so byte/mode equality with its original app did not establish recipient
readability. The earlier structural audit remains preserved with that limitation.

## Build 25 correction and current evidence

Only `collector_entry.py`, `install.py`, `build_store.py` and two existing test
files change from 24. The entry accepts exactly hidden/us-external signed
settings, strict Boolean sandbox metadata, and an exact two-key sandbox policy
with integer version 1 and Boolean True. Account, saved-credential and external
reader helper modes stay blocked. The policy resource itself does not change.

Only generated license copies are normalized to 0755 directories and 0644 files
before signing. Source permissions and executable bits are preserved. A read-only
build guard requires recipient-readable regular files and traversable/readable
directories without changing signed content. It retains the existing internal
link checks. The original 24 code fails both new regression controls.

The builder completed a fresh app and unsigned package. Its BOM shows 97 actual
app entries, all uid 0/gid 0, readable regular files and readable/traversable
directories. The two license directories are 0755 and 32 license files 0644.
The original source license directories deliberately remained 0700 for this build.
Builder proof is separate from independent package inspection and a real install.

Independent package inspection passed the 85 frozen inputs, 163 source files,
nine compiled product modules, 23 resources, signatures, exact entitlements,
installer bytes/modes/links and recipient access. It explicitly compared the
changed entry code and kept the other eight reader/server/quota modules and
production Runtime as unchanged controls. No installer scripts or excluded
account/reporting payload entered the package.

A valid derivative native sandbox parent then passed 54 checks with the exact
packaged helper: 19 normal-stop and 35 Sample-isolation checks. Authenticated
startup, sandbox/account restrictions and cleanup passed with no account grants,
provider prompts or Storefront request. The 51-entry helper tree and production
Runtime stayed exact, all owned processes ended and generated runtime access
files were removed. This is not a production GUI or recipient-install result.

## Actual installed build 25, phase 1

The unsigned native installer upgraded the existing test-user installation.
Finder reported version 0.2.5 and 22,866,404 logical bytes, matching the audited
app. Launch with both provider apps closed resumed monitoring with native
attention count 4. Existing real Claude and Codex Ready cards and both connected
source footers were observed. The build 24 startup error did not recur. The
full four-card summary was not captured in one screenshot.

The ordinary recipient opened the license folder and its nested standalone
Python folder without changing permissions. Python license text was readable
in Quick Look. This supplies recipient evidence for the correction separately
from the BOM and structural checks.

Native About, its local Privacy link and back navigation worked. The optional
Ko-fi control and bundled QR appeared through normal production eligibility,
with no forced storefront result. A click opened guest Safari at exactly
`https://ko-fi.com/spacelydigital` without a query. No payment or camera QR scan
occurred. Country was not independently read; non-US, offline, changed-storefront,
receipt and TestFlight behavior were not exercised.

Normal Quit and manual reopening retained Ledger/Bureau green/Dark/Sound off
and both connected sources. The final app Quit and orderly guest shutdown were
visually confirmed. In this first phase, no provider prompt, source grant, source-file edit,
permission change, signing or App Store upload occurred. This was an existing-grant
upgrade, not a clean first-permission install. Guest helper PIDs/network,
post-OS-restart build 25 launch, login launch, quota parity, spoken VoiceOver
and actual macOS 13 app workflows remain unverified. Cached quota rendering
was visible; a five-hour reading remained unavailable.

## Later actual build 25 workflows

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

Manual launch after an OS restart was observed during the real-provider run;
automatic login launch was not tested. These runs used the same frozen preview
and existing test-user sources. They are not final Store identity, clean new-user
installation, full duplicate census, guest PID/network or quota-parity proof.

## Preserved earlier evidence

| Candidate | Established scope | Important limit |
| --- | --- | --- |
|21 | Source/package/reader fixtures, derivative native helper checks; actual recovery, one Codex Cooking/closed-window completion/notification click and restart persistence | Does not prove later main/helper builds |
|22 | Accessibility proposal and supporting fixtures; actual 200% header/panel overflow | Preserved failing candidate |
|23 | Actual native install/launch, enlarged text reflow, reachable controls, title/actions/menu fit, normal Quit/relaunch presentation persistence | No new provider turn, spoken VoiceOver or guest helper PID proof |
|24 |403 source tests, native fake-storefront checks and structural package checks | Actual startup and recipient license access failed |
|25 |412 source tests, independent package/BOM checks, 54 derivative-parent checks; actual upgrade, two real provider turns, handoffs/review, source reconnection and bounded keyboard/persistence | No clean first-grant, negative storefront, full accessibility or final Store proof |

The 350-case WebKit matrix supports five layouts at 270, 390, 720, 900 and 1440 CSS
pixels, in light/dark modes. Actual 23 captures support only the exercised native
window/layout/theme flows. A valid native zoom menu is not a VoiceOver pass.

## Privacy and support parity

No app-operated analytics, advertising, crash upload, developer reporting,
registration or synchronization endpoint exists. The Store archive excludes
prototype credential/account/network readers. Local chat previews, state,
bookmarks and diagnostics remain on the recipient’s Mac. Monitoring does not
send prompts or invoke provider commands. Quota readings are local cached data.
The optional Claude plugin exports only quota/reset/observation fields locally.

The approved free price does not depend on donating. Hidden support is the
builder default. The explicit US variant uses Apple StoreKit only while the
native About page is active and again before opening the fixed Ko-fi URL.
Unknown/non-US results remain hidden. A passive connection check is not an
Internet-reachability guarantee. No storefront is saved, logged or sent to the
developer; displaying the bundled QR makes no Ko-fi request.

A user-chosen email or Ko-fi visit/payment can send their selected message,
contact and transaction information through those external services. It would
be inaccurate to promise that voluntary contact/donation data never reaches the
developer. The app attaches no board data or diagnostics automatically.
`Spacelydigital@gmail.com` is the approved public support email.

The bundled policy and PRIVACY.md disclose these boundaries.

[Support](https://spacelydigital.github.io/agent-board-support/) and
[Privacy](https://spacelydigital.github.io/agent-board-support/privacy.html)
are live. Four website files returned HTTP 200 with the reviewed bytes;
independent source readback and actual desktop/narrow browser navigation passed.
The public privacy page includes StoreKit eligibility, voluntary support and
GitHub hosting-IP disclosures. The website contains no app source or account data.

## Required before submission

1. Complete clean first-grant setup, negative storefront conditions and remaining
   final-artifact workflows. The bounded build 25 provider/recovery checks above
   passed; repeat affected workflows if signing, identity or product bytes change.
2. Install and run the final candidate on macOS 13. Native VoiceOver spoken
   labels/announcements, ordinary keyboard navigation, reduced motion, effects
   and energy behavior still require actual assistive/user observation.
3. Validate live provider state transitions, same-chat handoff, Review/Restore,
   revoked/regranted sources, real quota limitations and bounded restart/error
   behavior on the exact release artifact. Historical tests are not substitutes.
4. Prove use of the existing Apple application/installer signing identities
   through the owner’s Keychain handoff, verify the final registered identifier
   and any required profile/capability route, and inspect final package chains.
   Identity visibility and an old notarized download do not prove this step.
5. Enter the verified support/privacy URLs, free listing price and final metadata
   in App Store Connect. Complete screenshots, privacy answers, provider
   authorization/branding and App Review instructions, then authorized Apple
   upload/processing and release-specific receipt/distribution checks.

No release gate is waived by source-test counts, a dummy native parent,
structural signatures, a listed certificate or a successful website repository.

## Backup and publication scope

The build 25 clean source and six-asset local/private backups were independently
read back, including all source bytes and the restored app tree/signatures.
Those immutable backups predate the later provider/recovery phases and website
update. Their evidence remains private. This public source snapshot updates only
nonruntime documentation; it contains no private repository ancestry, account
files, guest files, credentials, keys, raw logs or validation screenshots.
No open-source license has been selected; third-party notices remain intact.
