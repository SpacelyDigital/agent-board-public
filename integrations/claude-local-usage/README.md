# Claude local usage exporter

This optional plugin lets Agent Board show Claude's 5-hour and weekly allowance
without signing in to Claude, opening its account files or calling its servers.
Claude supplies the last quota figures it already has. The exporter writes only
the percentages, reset times and observation time on this Mac. It does not
export chats, prompts, session IDs, account details, credentials or token counts.
There is no developer service or network call in this plugin.

The integration needs the Code tab's embedded Claude Code **2.1.299 or later**.
That is the minimum for this plugin's manifest without a custom types file.
Mods are supported in Desktop from 2.1.286. Organization policy may disable them.
Local Finder installation was exercised in a disposable Mac VM with Agent Board
builds 16 and 17. The installed plugin also produced a real weekly export while
build 18 monitored it. The tested account exposed no five-hour window. Repeat
provider-version and runtime checks on the final distribution artifact.

## Install locally

1. In Agent Board, open **Setup and Permissions → Connect Claude Usage…** and
   choose **Show Plugin Files**. Finder reveals the bundled `claude-local-usage`
   folder. **Copy Plugin Path** copies the same folder's location.
2. Review the plugin before installing it. In Finder, use **Go → Go to Folder…**
   to open `~/.claude`. Open its `skills` folder, or create one if it is absent.
3. Copy the entire `claude-local-usage` folder into `~/.claude/skills/`.
   **Do not overwrite or merge an existing folder.** Stop if that destination
   already exists. The copied folder must contain `.claude-plugin/plugin.json`
   and `hooks/hooks.json`, with `hooks/register.js` beside that hooks file.
4. Start a new **local** session in Claude Desktop's Code tab. Claude Code
   documents loading personal plugin folders from this location at session
   start, without a marketplace. This plugin's ID is
   `agent-board-local-usage@skills-dir`. No account upload, publication,
   Terminal command or automatic settings edit is needed for this path.
5. Keep **Claude Sessions** selected in Agent Board. The plugin defaults to
   `~/.claude`; no custom configuration is needed. Alternate Claude
   configuration folders are not supported by the Store onboarding.

Personal plugins load automatically in new sessions unless a setting or
organization policy blocks them. Copying this folder installs code that runs
with Claude's permissions. Agent Board does not copy it, change Claude's settings
or make that trust decision automatically. The plugin has no prompt tool, model
call, network client or credential authorization call. A blocked file write
leaves the exporter unavailable; it does not request broader permissions or
bypass those rules.

The first reading may remain unavailable until Claude receives an ordinary
response, or if Claude has no cached subscription figures. Agent Board never
sends a message just to refresh them. App updates do not automatically update
the copied plugin. Final distribution and supported-version coverage remain
release checks. The Desktop
local-marketplace UI route is not established and is not required by these steps.

## Files and freshness

The running exporter writes only these quota files below the Claude folder:

- `agent-board/usage/quota-v1-a.json`
- `agent-board/usage/quota-v1-b.json`

They alternate between observations, so disk use stays bounded. Each v1 envelope
contains a canonical quota-only payload and its SHA-256 checksum. Claude's
documented file-write operation is not atomic. Agent Board accepts an entire
valid envelope and preserves its last verified reading if a write is interrupted.
The checksum detects corruption; it does not authenticate the local provider.
Other programs running as the same local user can change these files.

The observation time is when the plugin saw a cached provider figure, not proof
of a fresh account request. Quotas can be absent or old, and an expired reset
never means Agent Board has verified a new 100% allowance. If you switch Claude
accounts or uninstall the plugin, remove these two files to clear old exports.
The plugin also clears its exported windows when Claude delivers a logout event;
an interrupted logout or an older session may not deliver one. The export has
no account identifier and cannot independently confirm the active account.
Disabling or uninstalling the plugin stops updates. Forgetting the source
folder in Agent Board must stop reading them.

## Provider documentation checked November 5, 2026

- [Mods in Desktop and version support](https://code.claude.com/docs/en/plugins/mods/overview)
- [Quota observations and the session API](https://code.claude.com/docs/en/plugins/mods/reference)
- [Complete upstream type declarations](https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts)
- [Local plugin installation](https://code.claude.com/docs/en/plugins/install)
- [Personal plugin folders that load at session start](https://code.claude.com/docs/en/plugins/create#make-a-plugin-load-in-every-session)
- [Plugin origins and local loading](https://code.claude.com/docs/en/plugins/loading)
- [Manifest and user configuration](https://code.claude.com/docs/en/plugins/manifest-reference)

These sources document a local extension mechanism. They do not constitute
permission to reuse Claude.ai credentials or approval by Apple App Review.
