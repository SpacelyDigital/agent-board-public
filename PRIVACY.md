# Agent Board privacy

Agent Board is a local Mac dashboard for existing Codex and Claude Code chats.
It has no analytics, advertising, tracking pixels, crash-upload service or
developer-operated backend. It does not send chats, account details, usage
statistics, diagnostics or crash reports to Spacely Digital or a reporting
service. There is no Agent Board account, device registration or reporting ID.
The dashboard is served only on this Mac's loopback address, `127.0.0.1`.

## Data read and stored on this Mac

The collector reads existing provider metadata and session records to identify
chats and their activity. These include chat and project names, local project
paths, identifiers, timestamps, lifecycle events and short literal reply or
permission previews. Codex and Claude use incremental verification of complete local transcript history, with
per-poll byte/time limits and limits on retained fields and structural complexity.
Large image and tool-output strings are validated incrementally and discarded
without copying them into the board. Changed
files can require re-reading previously indexed history to verify their prefix;
malformed, overly complex or continuously changing histories may leave
completion unconfirmed. An incomplete history is never proof of completion.

The Agent Board database stores titles, paths, identifiers, status, timestamps,
short previews, review and snooze state, and your dashboard naming and
placement preferences. It also stores compact file-verification metadata such
as hashes, byte counts and a generation marker. Raw transcript records, partial
lines and parser state are not persisted in that board database. It does not
copy complete transcripts or tool output into the board. The database and local
service logs in the direct-download prototype are stored in:

```
~/Library/Application Support/Agent Board/data
```

The Store preview uses its own macOS app container. It reads only
the Codex, Claude Sessions and Claude Desktop folders chosen through the native
folder controls. Read-only bookmarks are kept on the same Mac and transferred
only to the bundled collector. Forget Source Folders stops the collector before
discarding those bookmarks. Unsupported account helpers and saved-credential
reads are disabled in that build, including when invoked directly.

If a read-only open of the selected Codex index fails, Agent Board may make a
bounded local copy of that index in a private temporary file on your Mac. The
copy can contain index metadata beyond the fields shown on the board. This
fallback does not copy session or transcript files, change original sources or
folder permissions, or upload the copy.

Before copying any index data, Agent Board removes the empty temporary file’s
folder entry. The copy remains in an unnamed local file that the operating
system releases when the reader closes it or the reader process ends.

When confirmed database corruption requires a fresh board, the app preserves
the old database and matching journal files in a private local recovery folder
and displays a recovery notice. Invalid discovery metadata is backed up before
repair. These copies may contain the same board fields and are never uploaded.
A preserved damaged file is not a guarantee that its contents can be recovered.

Browser appearance, layout and sound preferences use local web storage. The Mac
app uses a nonpersistent WebKit store and saves approved display preferences in
local macOS app preferences so they survive a restart or collector port change. Native notification preferences and event
acknowledgements are stored in local macOS app preferences. Nothing in these
stores is synchronized by Agent Board.

Reviewing, renaming, moving or snoozing a card changes the local board only.
Agent Board does not edit, delete, archive or send messages in a provider chat.

## Store usage meters

The Store profile excludes the private prototype's authenticated provider and
cloud-project readers. It does not read saved sign-ins or make provider account
requests. Codex quota readings come from verified local session records. An
optional bundled Claude plugin, installed by the user in Claude, writes cached
quota percentages, reset times and observation times to two files under the
selected Claude folder. It exports no chats, prompts, credentials or account
identifiers and makes no network requests.

Agent Board displays the original observation time and marks old or incomplete
readings stale. Reading a cached file does not confirm the active account or a
renewed allowance after a scheduled reset. Missing readings stay unavailable;
the app does not send a prompt to refresh them.

## Private prototype account requests

Optional account features in the private direct-download prototype use existing
sign-ins. They are network requests, even though the dashboard and its database
are local. The Claude saved-sign-in integration is not cleared for public or
App Store distribution; the sandbox proof build does not use it.

- **Claude usage and projects:** Read-only HTTPS requests to the fixed
  `https://api.anthropic.com` origin retrieve subscription limits, project status,
  titles, thread activity and bounded reply previews. The app reads the active
  Claude desktop account and organization from its existing local cache. It
  uses the named **Claude Safe Storage** Keychain item to unlock the existing
  credential in memory, then sends that credential only in authentication
  headers to Anthropic. An exact-origin, read-only HTTPS boundary rejects other
  destinations and redirects, ignores proxy configuration, and validates TLS.
  The app does
  not save or log the credential and does not refresh it or initiate a sign-in.
- **Codex usage:** The app invokes the installed Codex local app-server with
  analytics disabled and requests only its account rate limits. Codex uses its
  existing sign-in and handles its own provider connection. Agent Board does
  not start a chat or model turn.

Agent Board does not submit prompts, start agent work, upload local chat
transcripts, or transmit data to the developer. Provider requests necessarily
include authentication, request identifiers and normal connection information,
such as your IP address, to the relevant provider. OpenAI and Anthropic process
those requests under their own terms and privacy policies.

The interface uses system fonts and bundled scripts, styles, logos and QR assets, with no
remote tracking resources. Its content policy limits interface connections to
the local dashboard. Provider errors are sanitized, and local source-scan errors
do not record exception text that could contain chat content or credentials.

A missing or denied credential leaves the relevant feature unavailable rather
than causing Agent Board to ask for a password or send a model request.

## Apple distribution and system diagnostics

Agent Board has no diagnostic uploader or developer analytics integration.
Apple separately offers system and App Store settings that let people share
usage and crash diagnostics with app developers. Those platform services are
controlled by Apple and the Mac owner's settings, not by Agent Board. Removing
app telemetry does not disable Apple's services. No App Store submission or
TestFlight enrollment has been performed for the sandbox engineering build.

See [Apple's description of developer analytics](https://developer.apple.com/app-store-connect/analytics/).

## Permissions and local connections

The Mac app asks for consent before it starts monitoring. The Store profile
offers read-only source-folder selection, optional notifications and opening at
login. It does not offer saved-account access. In the private direct-download prototype, account
usage, Claude cloud projects, notifications and launch at login are off by
default. Change those choices with **Agent Board → Setup and Permissions**.

If you choose **Allow Claude Account Access**, the app requests access to the
named existing Claude Keychain item through macOS. Background credential reads
never request a permission prompt. Notifications are optional and subject to
macOS permission. Agent Board does
not require camera, microphone, screen recording or Accessibility access. Its
collector needs read access to the existing provider files used for discovery;
macOS may restrict those files or the existing Claude Keychain item.

The packaged app uses a per-launch secret for its trusted native dashboard
and restricts accepted hosts and origins. Source development mode can use a
regular local browser with a per-process action token. Neither mode exposes
the dashboard on your local network or publishes it to the internet.

Quit Agent Board to stop monitoring. In the Store preview, choose **Agent Board →
Open Local Data Folder**, quit the app and move that data folder to Trash to
remove its board database and logs. The private direct-download prototype stores
these under `~/Library/Application Support/Agent Board`.
Display web storage and macOS app/notification preferences are separate local
stores; removing the board database does not reset those display settings.

## Links and support

Opening a chat switches to the corresponding installed provider app using its
existing chat identifier. It does not submit a prompt or create a new chat.

The Store preview offers `mailto:Spacelydigital@gmail.com` for support. Clicking
it opens the user's email app without a prefilled message or attachments.
Nothing is sent automatically. If the user sends an email, their email address
and chosen message are shared with that support address and the email services
involved. This is separate from the app's monitoring and usage meters.

Agent Board is free. The private direct-download prototype includes an optional
`https://ko-fi.com/spacelydigital` support link. A Store build includes it only
when explicitly packaged with the `us-external` strategy and a fresh Apple
StoreKit storefront result is `USA`. Other or unavailable storefront results
keep the link and QR code hidden. The default Store strategy is `hidden`.

The Store app uses Apple's StoreKit service to check availability while its
About page is active and again before opening the link. It observes local
connection availability without making a test request to a website. It does
not save or log the storefront, add it to a user profile, or send it to the
collector or developer. A known unavailable connection or missing, changed or
slow storefront result leaves support hidden. Local connection availability
does not guarantee that the Internet is reachable.

The QR code is generated locally and bundled with Agent Board; displaying it
makes no request to Ko-fi or a QR-code service. Clicking the link or scanning
the code opens the fixed Ko-fi page without board, account, chat or usage
parameters. Ko-fi handles the visit and any optional payment under its own
terms and privacy policy. It may share a supporter's contact and transaction
details with Spacely Digital. This voluntary website handoff is separate from
app monitoring. Agent Board does not receive or verify payment results and
does not unlock features for supporters.

Claude and Codex names and logos belong to their respective owners. Agent Board
is independent and is not affiliated with Anthropic or OpenAI.
