# App Review notes

Agent Board is a free status board for the Codex app and Claude Code in Claude Desktop on this Mac. It shows which coding chats are working, waiting for input or have a reply ready, and opens the original chat in its app. It has no Agent Board account, developer server, analytics, ads or crash uploader. It requires Apple silicon and macOS 13 or later.

## Reviewing without provider apps

On first launch, choose Try Sample Board in Setup. If you cancel Setup, the app stays open with Set Up Monitoring and Try Sample Board available. Show Sample Board is also available from the Agent Board menu.

The sample board uses built-in fictional chats. Use its Cooking, Needs you and Ready controls to change their state. Mark a reply Reviewed, restore it from history, snooze it, rename it and try the appearance settings. Sample chats do not open provider apps or generate live-chat notifications. Return to Live Board preserves the live board and folder choices.

## Live monitoring

Setup lets the user select three folders with read-only access: Codex (~/.codex), Claude Code (~/.claude), and Claude Desktop's Claude Code sessions folder (~/Library/Application Support/Claude/claude-code-sessions). The broader Claude application-support folder is not accepted. Existing broad grants must be selected again at the narrower location.

Agent Board reads local session records and retains titles, identifiers, timestamps, status and short reply or permission previews. It never edits or sends messages in the original chats. A bounded in-memory snapshot of the Codex index may be reused when its selected folder cannot support a read-only SQLite open. It is not written to temporary storage. Regular ChatGPT and Claude account chats and account usage are not included in this Store edition. Codex weekly usage can be read from verified local session records.

## Network entitlements

A bundled helper serves the board to the app's own window on 127.0.0.1, protected by a per-launch secret. This local connection requires the client and server network entitlements. The Store edition has no provider account readers or saved-credential access.

## Controls and privacy

Notifications and Open at Login are off until enabled. Closing the window keeps monitoring in the menu bar; Quit stops the helper. Forget Source Folders stops monitoring and discards the grants. The bundled About and privacy pages are available offline. The support email opens the mail app without sending or attaching anything automatically.

## Optional support configuration

For a candidate built with hidden support, no payment link or storefront check is present. If the selected candidate instead uses us-external, replace this paragraph with: About checks Apple's current App Store storefront. Only a United States storefront can display the optional support link and bundled QR code, and the app rechecks before opening the fixed destination. This unlocks nothing; unknown or other storefronts keep it hidden.
