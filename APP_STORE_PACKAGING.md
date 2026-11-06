# Mac App Store packaging

Agent Board has separate direct-download and Store build paths. Neither a local
package nor a passing engineering test means Apple has approved the app.
`build_store.py` never installs the app, opens a provider, discovers signing
credentials, calls StoreKit, submits an upload, or changes the existing app.
Previous build directories are preserved. Use a new immediate child of `build/`
for each run.

## Local engineering package

After creating the pinned portable build environment, set `AGENT_BOARD_BUILD`
to a new positive build number and choose an unused output. The builder refuses
to replace a preserved build. The following is a template, not a claim that the
next preview has passed:

```sh
.local/build-venv/bin/python build_store.py --preview \
  --bundle-id local.agentboard.store-preview \
  --version 0.2.5 --build-number "$AGENT_BOARD_BUILD" \
  --output "build/store-preview-$AGENT_BOARD_BUILD"
```

This creates an ad-hoc signed, sandboxed `Agent Board.app`, an unsigned installer
package, and `STORE-PACKAGE-CHECKS.json`. It cannot be submitted as a distribution
build. The supplied identifier is explicit, and the tool does not claim that it
is registered. As of November 5, 2026, the owner has registered the exact release
App ID `com.spacelydigital.agentboard`. App Store Connect record 6831478051 now
exists as **Agent Board: AI Reply Alerts**. Draft configuration and release gates
remain open; see [current release status](APP_STORE_READINESS.md). The preview template uses a separate local identity
and does not establish that product record or release approval.

The app and helper each contain a privacy manifest, the collector contains an
immutable sandbox policy, and all native dependencies stay inside the app. The
main app has only the reviewed sandbox entitlements. The helper has only sandbox
inheritance, and libraries have no executable entitlements. Local monitoring
needs the recipient's folder selection. This build does not obtain credentials
or call installed provider command-line tools.

## Distribution package

The Store channel requires an **Apple Distribution** application identity and a
**Mac Installer Distribution** installer identity. A Developer ID certificate
and notarized DMG belong to direct distribution and do not replace these. Apple
documents an installer package as the Store submission format. See [creating
distribution-signed code](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/)
and [packaging Mac software](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution/).

### Transporter checkpoint, November 6, 2026

This shipping-status snapshot ends on November 6, 2026 at 06:17 UTC.

The owner explicitly approved Verify and Deliver for the exact signed build 27
package. Verify succeeded at 01:01 local time (America/New_York), with sole
warning 90889 that the missing provisioning profile prevents TestFlight use.
Deliver completed at 01:07 as “Delivered with Warning”. The issue view showed
the same sole warning and “The app is processing”. This describes 01:07,
not the latest App Store Connect state.

Build 27 was subsequently selectable and attached to version 0.2.5, with
save/reload verification and **Missing Compliance** status. The encryption
question was opened with no radio selected and canceled without saving an
answer. Delivery and selectable/attached status do not establish overall Apple
processing, SDK/privacy or export-compliance acceptance. The recorded delivery
UUID and exact byte count are retained in the engineering checkpoint.

The warning was independently reviewed against Apple’s
[TN3125](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles):
a Mac app using only unrestricted entitlements can use the profile-free Mac App
Store route, while TestFlight requires a profile. The exact package remains
ineligible for TestFlight without separately reviewed provisioning. No profile
or signature was changed solely to suppress this warning. The error-code wording
is the operator’s observed Transporter result; the independently verified rule
is the App Store/TestFlight distinction.

Completed Transporter delivery does not establish completed Apple processing,
SDK/privacy-origin or export-compliance acceptance, App Review, release or a
customer’s Apple-resigned download. Keep the sole warning and processing state
in the dated checkpoint; track the later Missing Compliance attachment status
separately.

### Signing status as of November 6, 2026

Application and Mac Installer Distribution signing completed for build 27 on
November 5, 2026.
The registered app identifier is `com.spacelydigital.agentboard`, with explicit
`us-external` support and the reviewed profile-free unrestricted route. The
builder’s complete package checks and independent exact app/installer audit
passed. Original and extracted payloads matched all 97 entries, including types,
bytes, modes and links. Signatures, exact entitlements, recipient-readable
licenses, source/module/resource exclusions and quarantine absence passed.
No private signing material is exported by this workflow.

The installer audit verified the actual `AppleMacInstallerSubmission` certificate
role, its expected team and Apple chain, and the package signature. `pkgutil`
displayed “signed by a developer certificate issued by Apple (Development)”.
That status text alone does not classify this submission certificate as an app
development identity; its submission role was checked independently. The audit
made no trust override and claims no fresh online revocation, direct-download
Gatekeeper acceptance, Apple processing or customer download.

The package is 10,374,279 bytes with SHA-256
`ef1d0fdcbe701ce055e2e6d395ae4d57454a7ff9c771d820d144ca63ed1ef2ad`.
The builder report is pinned in [ENGINEERING_CHECKPOINT.json](ENGINEERING_CHECKPOINT.json).
No installed signed-distribution workflow, minimum-OS pass, overall Apple
processing or release approval follows from successful signing. The native
macOS 26 Installer later reported success, and normal launch reached initial
Setup and the Codex read-only picker without a Gatekeeper override. No source
grant or full monitoring/provider workflow has been verified. The separate
ad-hoc preview 27 passed bounded no-source, Privacy/navigation, Light persistence,
Sample-review/return and window-reopen checks on macOS 13. Connected and complete
minimum-OS workflows remain unverified. App Store Connect record creation
succeeded under the owner’s new listing name. Draft version 0.2.5, the support
URL, manual release, subtitle and Developer Tools category were saved and
verified after reload. Remaining fields and granular native observations are
recorded in
[current release status](APP_STORE_READINESS.md).

The first build 26 distribution attempt signed its application components;
deep strict component checks passed. Packaging then stopped because the
`codesign -R` verification requirement was missing its leading `=` and was
interpreted as a filename. The failed output remains preserved. A narrowly
corrected build 27 subsequently completed signing and builder package checks.
The failed 26 output remains history rather than a valid package. Build 27’s
independent exact-package review later passed. Those historical builder/audit
results did not establish native or Transporter outcomes; the later delivery,
attachment and bounded installed-app observations above are recorded separately.

Earlier checkpoint notes recorded unavailable installer metadata and a timed-out
15-second application-signing probe. Those observations are preserved history,
not the current signing state. The separately tested build 25 preview remains
ad-hoc signed with an unsigned installer. TestFlight or restricted capabilities
need their own matching provisioning route; the reviewed sandbox-only route is
described below.

The following template uses the registered identifier and identities explicitly
supplied by the owner. Set `AGENT_BOARD_BUILD`, `AGENT_BOARD_TEAM_ID`,
`AGENT_BOARD_APP_IDENTITY` and `AGENT_BOARD_INSTALLER_IDENTITY` locally. The
command does not discover, create or export credentials, and does not upload:

```sh
.local/build-venv/bin/python build_store.py --distribution \
  --bundle-id com.spacelydigital.agentboard --version 0.2.5 \
  --build-number "$AGENT_BOARD_BUILD" --team-id "$AGENT_BOARD_TEAM_ID" \
  --app-signing-identity "$AGENT_BOARD_APP_IDENTITY" \
  --installer-signing-identity "$AGENT_BOARD_INSTALLER_IDENTITY" \
  --profile-free \
  --support-strategy us-external \
  --output "build/store-distribution-$AGENT_BOARD_BUILD"
```

The explicit `--profile-free` path permits only the reviewed unrestricted
sandbox entitlements, adds no application/team entitlement claims and embeds no
profile. It still requires Apple-issued application and installer signatures,
the requested signing team and every package check. It is not a TestFlight
configuration. Apple's [TN3125](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles)
explains that a Mac app without restricted entitlements can use the App Store
without a profile, while TestFlight always needs one. This path has fixture
coverage and a successful build 27 distribution package at the builder level.
Independent full-package verification passed; complete installed/native and
overall Apple processing/compliance acceptance remain separate gates.

To use the provisioned path, replace `--profile-free` with
`--provisioning-profile /path/to/AppStore.provisionprofile`. Both cannot be
supplied together. TestFlight or future restricted capabilities also need their
own reviewed configuration; supplying a profile alone does not establish
TestFlight compatibility. Profile preflight rejects device
lists, development debugging, Developer ID profiles, expiration, wildcard app
identifiers, wrong teams, and a main signing certificate absent from the profile.
The main app's explicit application identifier and team must match. Profile
inspection is a diagnostic preflight; Apple's DER profile interpretation and
upload validation remain external gates. Apple explains this boundary in
[Inside Code Signing: Provisioning Profiles](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles).

The resulting package must pass deep signature verification, exact main/helper
entitlement checks, library checks, minimum OS and architecture checks, installer
signature checks, and a complete unpack-and-compare of file bytes, executable
modes and links. It must install only this app into `/Applications` and contain no
installer scripts. This check does not install the package. The report always
keeps `submission_ready` false because local packaging cannot establish App
Store registration, Apple processing, TestFlight behavior, or App Review.

The Store profile sets `AgentBoardStoreDistribution` in both signed bundle
manifests. The default `hidden` strategy omits the external support section and
Ko-fi assets. Distribution packaging requires an explicit `--support-strategy
hidden` or `--support-strategy us-external` choice. The latter bundles only the
local QR SVG and permits native presentation on About after a fresh `USA`
StoreKit storefront result, with a new check before browser handoff. Unknown,
other or unavailable results hide support. The app does not persist or log the
storefront, make purchases or receive payment results. Eligibility does not
establish App Review approval for the external link.

Direct downloads keep their existing support page. The reviewed
`static/about/store.html` is mapped to the packaged About index and has no
static support call to action. The native app creates the optional section
only after its checks. The build report records the strategy, asset mapping
and verified bytes.
The actual frozen Python archive must exclude the direct-only usage, project,
credential/network and process-reader modules. Generic Python standard library
capabilities remain subject to the operating system's sandbox and the app's
source-access policy.

The Store About page also exposes the owner-approved public support address
`Spacelydigital@gmail.com`. Its exact `mailto:` link has no subject, body,
attachment or other query. It opens the recipient's mail client without sending
anything. The recipient decides whether to write and send a message. This does
not add background reporting. Optional Ko-fi visits and payments are separate
browser actions under that website's policies, which may share supporter
contact and transaction details with Spacely Digital.

## Privacy and purchase data

Both manifests declare no tracking and no collected data. App preferences use
only the app's own defaults (`CA92.1`). File metadata is read in the app's
container (`C617.1`) and recipient-selected folders (`3B52.1`). Elapsed-time and
timer calculations stay local (`35F9.1`). The helper does not declare UserDefaults
access. These declarations cover actual functionality; they do not claim every
symbol present in CPython is invoked. See Apple's [privacy manifest
format](https://developer.apple.com/documentation/bundleresources/privacy-manifest-files)
and [API categories and reasons](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacyaccessedapitypes/nsprivacyaccessedapitype).
Apple's current required-reason enforcement documentation names its mobile,
TV, spatial and watch platforms. Including these accurate macOS manifests is
not a claim that every listed rule is mandatory for this Mac app.

No receipt, purchase identifier, analytics identifier, or recipient log is sent
to the developer. Packaging refuses any app that already contains a recipient's
`_MASReceipt`. The product has no purchase or subscription implementation. If
the owner later chooses a paid licensing model that needs purchase validation,
prefer on-device StoreKit verification and graceful offline handling. Apple's
[`AppTransaction.shared`](https://developer.apple.com/documentation/storekit/apptransaction/shared)
may require network connectivity, and `refresh()` may ask for App Store sign-in.
Neither is called by this build or its local tests. Purchase validation is a
separate commercial decision and cannot be demonstrated with an ad-hoc app.

The icon is original, code-drawn artwork without provider marks. To reproduce:

```sh
swift -module-cache-path .local/icon-swift-cache tools/build_icon.swift .local/AgentBoard.iconset
iconutil -c icns .local/AgentBoard.iconset -o assets/AgentBoard.icns
```

## Remaining release gates

1. Preserve verified draft version 0.2.5, manual release, support URL, subtitle
   and category. Complete remaining App Store Connect fields, including approved free
   price and privacy URL;
   finish seller/SKU, age rating, availability and export-compliance answers.
   The bundled runtime includes third-party OpenSSL/HACL* cryptographic code,
   so local-only monitoring does not support a no-crypto or Apple-OS-only claim.
   The owner’s exemption/documentation determination remains pending.
2. Preserve the independently verified signed package and exercise its actual
   final-identity installation, grants/recovery and native workflows. Exact local
   app/package proof is complete; SDK-origin treatment, fresh online revocation
   and Apple processing/delivery are separately scoped.
3. Owner-authorized Transporter Verify and Deliver are complete with the sole
   profile/TestFlight warning above. Build 27 is attached with Missing Compliance.
   Resolve the unanswered encryption/compliance gate, retain SDK/privacy checks
   and test the distribution-delivered artifact on a clean Mac. If TestFlight is
   used, it needs its separately reviewed provisioned configuration. Engineering
   signatures do not establish receipts, update migration or TestFlight parity.
4. Complete actual Codex/Claude workflows, permission denial/revocation, deep
   links, notifications, app updates, VoiceOver and keyboard navigation, offline
   operation, missing providers, and recovery with the exact candidate. Synthetic
   source fixtures remain supporting evidence rather than real-provider proof.
5. Review storefront screenshots and precise feature/privacy claims. The Store
   sandbox profile must not advertise direct-distribution-only account features.
   Apple App Review remains required and cannot be guaranteed in advance.
