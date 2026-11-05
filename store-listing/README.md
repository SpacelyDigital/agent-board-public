# Store listing drafts

These files prepare the Mac App Store listing and static website copy. The
listing has not been uploaded to Apple or approved for release. The separate
Support and Privacy website was owner-approved and published.

- `APP_STORE_METADATA.md` contains proposed listing text, verified website URLs
  and screenshot notes.
- `REVIEW_NOTES.md` records implementation, bounded validation and review steps.
- `PRIVACY_ANSWERS.md` maps the local-only Store build to privacy questions.
- `site/` contains the reviewed policy/support source. The published policy also
  discloses its hosting provider's visitor-IP security logs.

The live [Support](https://spacelydigital.github.io/agent-board-support/) and
[Privacy](https://spacelydigital.github.io/agent-board-support/privacy.html)
pages passed exact HTTP byte readback, independent source inspection and actual
live desktop/narrow navigation. The site has no scripts, remote assets,
analytics, forms or log uploader. Its mail link opens the user's mail app with
no prefilled message or attachment. Monitoring does not load the website.

The owner approved a free app and **Spacelydigital@gmail.com** as the public
support contact. Final seller details, registered product identifier,
availability and App Review contact still need actual App Store Connect
configuration. A working website is not Apple approval of the app or listing.

The current preview passed bounded actual provider/notification, review,
source-reconnection and About/Privacy keyboard checks. Final Store signing,
clean first-grant setup, actual macOS 13, complete accessibility and quota
limitations remain release gates. Keep the source, fixture, actual-user and
final-Store scopes separate before submitting these drafts.

Optional Ko-fi support does not unlock features. Hidden support is the builder
default; the explicit `us-external` variant uses native Apple storefront checks
before showing the fixed external link and bundled QR, and rechecks on click.
Normal display/handoff was observed, but independent country readback and
negative non-US/unknown/offline conditions remain unverified. Worldwide
external-link approval is not claimed. No payment or transaction-verification
feature is implemented by these files.

The privacy copy covers the sandboxed Store build and local quota exports.
Prototype saved-account readers are outside that package and are not supported
Store integrations. No new project license grant is made by publishing source;
keep dependency and provider notices intact.
