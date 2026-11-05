# Proposed App Store privacy answers

Scope: the sandboxed Store build with local source readers and optional local
quota exports. These answers must be checked against the final submitted binary.

| Question | Proposed answer | Basis and condition |
| --- | --- | --- |
| Do you or your third-party partners collect data from this app? | No | App code processes and stores the board only on the recipient's Mac. No developer endpoint, analytics SDK, crash uploader or external UI resource is included. |
| Is data used to track users? | No | No advertising identifiers, tracking SDKs or tracking domains are used. |
| Are chats, usage statistics or diagnostics sent to Spacely Digital? | No | Local readers and the quota exporter have no developer reporting destination. |
| Does Agent Board create an account? | No | Provider accounts belong to the provider apps; Agent Board has no sign-in or registration. |

Apple defines data processed only on the device as outside its privacy-label
collection definition. The label must still be revisited if a future version
adds remote content, authentication, purchases, support submissions or an SDK
that retains data off the device. [Apple's privacy-label guidance](https://developer.apple.com/app-store/app-privacy-details/)

The manifests declare `NSPrivacyTracking = false`, empty tracking domains and
empty collected-data types. The app and helper each include their manifest in
`Contents/Resources`. Local-purpose API declarations do not authorize unrelated
data use. Current Apple documentation requires collection descriptions on all
platforms and lists required-reason API platforms without macOS. The bundle
still needs final archive and Apple processing validation. [Manifest format](https://developer.apple.com/documentation/bundleresources/privacy-manifest-files),
[macOS placement](https://developer.apple.com/documentation/bundleresources/adding-a-privacy-manifest-to-your-app-or-third-party-sdk)

The Python runtime, its native libraries and every other bundled dependency
remain the app developer's responsibility. No Apple-listed manifest SDK was
identified in the current dependency inventory; that observation must be
rechecked if dependencies change. System WebKit is not a bundled analytics SDK.
[Apple's SDK requirements](https://developer.apple.com/support/third-party-SDK-requirements/)

Apple's App Store and macOS reporting are separate services controlled by Apple
and user settings. Do not promise that Apple never supplies platform metrics or
diagnostics to developers. Agent Board has no such uploader of its own. The
owner should not add an Apple analytics download or reporting integration to
the app as a workaround. [Apple analytics privacy](https://developer.apple.com/app-store-connect/analytics/#data-privacy)

The static privacy page is a draft awaiting owner hosting approval. App Store
Connect requires a real publicly reachable privacy-policy URL. The support
page has no form or request to send logs. The actual host's ordinary request
logging and retention need a separate owner decision.

The owner-approved public support address is **Spacelydigital@gmail.com**.
Its link opens an external mail app with no message body or attachments. Sending
is a separate user action. A voluntary email gives its sender address, message
and chosen contents to the support recipient and email services; this is not
automatic app telemetry or a support uploader. The public policy discloses that
handoff. Reassess privacy answers if support is later embedded in the app or
collects data automatically.

The optional `us-external` build strategy shows a Ko-fi support link and local
QR image only after a fresh StoreKit result identifies the United States
storefront. The default strategy hides both. Storefront information is used
only for availability in memory, is not logged or attached to a profile, and
is not sent to the collector or developer. No purchase, transaction lookup,
receipt validation or account sign-in is performed by Agent Board.

Choosing the link or scanning its bundled QR opens a fixed external Ko-fi page
without app data. Ko-fi may collect website and payment data and share the
supporter's contact and transaction details with Spacely Digital. The app
neither receives payment results nor unlocks features. The About section and
public policy disclose this optional handoff. These proposed privacy answers
still require final Apple review for the exact external flow; they are not a
claim that voluntary donations cannot identify their payer. The app itself
has no payment or reporting SDK. [Ko-fi privacy](https://more.ko-fi.com/privacy)
