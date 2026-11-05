# Mac App Store packaging

Agent Board has separate direct-download and Store build paths. Neither a local
package nor a passing engineering test means Apple has approved the app.
`build_store.py` never installs the app, opens a provider, discovers signing
credentials, calls StoreKit, submits an upload, or changes the existing app.
Previous build directories are preserved. Use a new immediate child of `build/`
for each run.

## Local engineering package

After creating the pinned portable build environment, run:

```sh
.local/build-venv/bin/python build_store.py --preview \
  --bundle-id local.agentboard.store-preview \
  --version 0.2.5 --build-number 15 --output build/store-build15
```

This creates an ad-hoc signed, sandboxed `Agent Board.app`, an unsigned installer
package, and `STORE-PACKAGE-CHECKS.json`. It cannot be submitted as a distribution
build. The supplied identifier is explicit, and the tool does not claim that it
is registered. A release identifier such as `com.spacelydigital.agentboard` must
first be confirmed by the owner in the intended Apple Developer team and App
Store Connect product. The example above uses a separate local identity. Build
14 is already preserved; the example uses a new output rather than replacing it.

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

The current local metadata check found Apple Distribution and Developer ID
application identities. It did not find a Mac Installer Distribution identity
or a matching macOS Agent Board provisioning profile. A profile is needed for
TestFlight or restricted capabilities, rather than the current sandbox-only
configuration. Visible identity labels
do not establish signing availability. No private signing material was read or
exported, and portal registration was not queried. The engineering candidate
remains ad-hoc signed; distribution signing is still a separate step. A bounded
15-second local application-signing probe timed out and left its disposable
artifact unsigned. It changed no Keychain permissions and does not establish
that the existing signing identity is usable or unusable.

The following template is for a later, owner-authorized signing step. It uses
identities explicitly supplied by the owner;
it does not locate or create credentials:

```sh
.local/build-venv/bin/python build_store.py --distribution \
  --bundle-id com.example.agentboard --version 0.2.5 --build-number 15 \
  --team-id ABCDEFGHIJ \
  --app-signing-identity 'Apple Distribution: Company (ABCDEFGHIJ)' \
  --installer-signing-identity '3rd Party Mac Developer Installer: Company (ABCDEFGHIJ)' \
  --profile-free \
  --support-strategy hidden \
  --output build/store-distribution-build15
```

The explicit `--profile-free` path permits only the reviewed unrestricted
sandbox entitlements, adds no application/team entitlement claims and embeds no
profile. It still requires Apple-issued application and installer signatures,
the requested signing team and every package check. It is not a TestFlight
configuration. Apple's [TN3125](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles)
explains that a Mac app without restricted entitlements can use the App Store
without a profile, while TestFlight always needs one. This path has fixture
coverage; actual distribution signing and Apple processing remain unverified.

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

1. Confirm the product identifier, seller identity, pricing/SKU, support URL,
   privacy-policy URL, age rating, export-compliance answers and commercial terms.
2. Confirm the existing application signing identity is usable, supply the
   missing installer distribution identity and, when required, matching profile, then
   independently verify the exact signed package. No credentials are needed
   for the local preview.
3. Upload only after owner authorization, resolve Apple processing diagnostics,
   and test the resulting TestFlight build on a clean Mac. Engineering signatures
   do not establish Store identity, receipts, update migration or TestFlight parity.
4. Complete actual Codex/Claude workflows, permission denial/revocation, deep
   links, notifications, app updates, VoiceOver and keyboard navigation, offline
   operation, missing providers, and recovery with the exact candidate. Synthetic
   source fixtures remain supporting evidence rather than real-provider proof.
5. Review storefront screenshots and precise feature/privacy claims. The Store
   sandbox profile must not advertise direct-distribution-only account features.
   Apple App Review remains required and cannot be guaranteed in advance.
