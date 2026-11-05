# App Review sample board

The Store app includes a local **Sample board** for exploring the dashboard
without installing Claude or Codex, signing in, selecting provider folders or
installing Python. Its two chats and usage readings are synthetic. They do not
demonstrate provider compatibility, real chat links or actual account quotas.

## Built-in review workflow

1. On first open, choose **Try Sample Board** in Setup. If live setup was already
   completed, use the app menu's **Show Sample Board** instead. The dashboard
   should identify itself as Sample board, show sample usage readings and begin
   with one Codex sample and one Claude sample in Cooking.
2. Choose **Needs you** in the sample controls. Expect the same two chats to need
   attention. Choose **Cooking** to resume them. Their previous attention flags
   should clear, leaving exactly two Cooking cards.
3. Choose **Ready**. Expect two completed sample replies with previews. Mark the
   Codex reply **Reviewed**, then rename or snooze the Claude example using its
   chat menu. Ordinary board controls operate on these examples.
4. Quit Agent Board using its app menu, then reopen it. The app should return to
   Sample board with the Reviewed acknowledgement, remaining reply, custom name
   and any unexpired snooze preserved.
5. Use **Return to Live Board** in the app menu. Previous live consent and folder
   selections are retained. If live setup was never completed, Setup opens;
   leave folders unselected and choose **Start Monitoring** to inspect the
   disconnected state. Sample chats and sample usage must not appear there.
6. Use **Show Sample Board** again. Its prior sample state should remain. The
   optional **Reset samples** control clears only the example chats and their
   preferences, then creates two Cooking examples. It does not reset the live
   board or modify provider files.

Sample mode does not access provider folders or accounts, open provider chats,
request provider permissions or send native chat notifications. Sample values
are explicitly labeled. Its database, display preferences and attention ledgers
are separate from the live board. Entering samples stops the live collector and
releases active folder access; returning to live restores only saved consent.

This is the reviewer demonstration path. Real source permissions, provider
versions, chat handoff, the optional local Claude usage plugin, notifications
and opening at login require separate real-environment evidence. Provide App
Review the exact submitted build and its verified provider requirements, as
requested by [Apple's review guidance](https://developer.apple.com/help/app-review/before-submitting-for-review/complete-review).

## Optional synthetic source pack

`tools/review_samples.py` is a separate developer fixture tool for exercising
the actual local transcript readers and native folder picker. It is not
required for the built-in Sample board. It requires **Python 3.11 or newer with
sqlite3 serialization support**, which a clean Mac may not include.

Run it only in a disposable macOS user or VM with no existing provider roots.
`python3 review_samples.py init` creates synthetic files in the current user's
standard `.codex`, `.claude` and `Library/Application Support/Claude` folders.
It refuses root/sudo and any existing root, including files or symlinks. There
is no force, merge, alternate-home command-line option or cleanup command. Do
not delete real data to make it run.

On the live board, select those exact three folders in **Setup and Permissions**
and choose **Save**. Advance the generated chats with these commands:

```sh
python3 review_samples.py cooking
python3 review_samples.py needs-you
python3 review_samples.py cooking
python3 review_samples.py ready
python3 review_samples.py status
```

Each state should contain one generated chat per provider, with no duplicate
attention flags after resuming Cooking. The Ready preview is “The synthetic
review sample is ready.” Review one reply, quit and reopen to check persistence.
`--provider codex` or `--provider claude` advances just one example. The initial
quota readings are fabricated and become stale; lifecycle commands do not
refresh them. The optional Claude plugin is not installed.

The tool appends only to its verified synthetic files and private control
journal. Replaced, additional, edited or interrupted files cause it to stop.
Discard the disposable user or VM after testing. Do not redistribute generated
folders, which contain machine-specific paths, timestamps and file identities.
Their fake IDs cannot establish genuine provider chat links.

## Evidence limits

`tests/test_sample_board.py` exercises the authenticated local HTTP interface,
sample transitions, stale acknowledgements, Review/rename/snooze persistence,
recovery after an interrupted reset, sample-only reset, denied provider opens
and isolation from the live database, including linked sample storage rejection.
Dependency guards assert that no provider reader or account helper is constructed.
`tests/test_review_samples.py` instead
exercises generated source files through the local readers and board store.

These are supporting synthetic checks. Neither test suite establishes an
unmodified packaged app's native UI, transferred folder grants, actual provider
behavior or App Store acceptance. Record those results separately with the
exact tested artifact hashes; do not describe this document as proof they pass.
