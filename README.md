# Codex bridge

A standalone, outbound-only GitHub Issues listener for an **existing local Codex CLI session**.
Python standard library, `gh`, and `codex` are the only dependencies. No API keys, web server,
cloud agent, daemon, or application changes. Installed at `/Users/norman/codex-bridge`.

## Start and operate

Run this in a dedicated Terminal window (keep your interactive Codex terminal open):

```sh
~/codex-bridge/codex-bridge start
```

Or double-click `Start Bridge.command` to open a dedicated Terminal window.
The listener stays in the foreground, polls every 15 seconds, and stops with Ctrl-C.
From another terminal:

```sh
~/codex-bridge/codex-bridge status
~/codex-bridge/codex-bridge activity
~/codex-bridge/codex-bridge stop
```

Stop finishes an active submission before exiting. Wait for `status` to say Stopped before
changing configuration. To target a different existing session:

```sh
~/codex-bridge/codex-bridge set-session YOUR-EXISTING-SESSION-UUID
~/codex-bridge/codex-bridge start
```

The command refuses a change while this listener runs or another bridge listener owns
the new target. It preserves all receipts, so changing sessions never resends old issues.
No command creates, resumes, or automatically chooses another Codex session.

## ChatGPT dispatch

Private relay: https://github.com/norman8823/codex-bridge

Tell ChatGPT, with its GitHub connection enabled:

> Send this to Codex: create one issue in norman8823/codex-bridge with a descriptive
> title, the complete approved task in its body, and label codex-send. Include requirements,
> constraints, acceptance criteria and any execution limits. Do not create duplicate issues.

ChatGPT must have **write access** through its connected GitHub integration; read-only access
alone cannot dispatch. Creating an issue with the label in the same request is supported.
The integration must act as an identity in `authorized_senders` (currently `norman8823`).
Both the issue author and the most recent actor applying `codex-send` must be allowlisted.
Comments never dispatch work. Unlabeled issues, PRs, closed issues, other repositories,
and public repositories are ignored/refused. Never put credentials in instructions.
Only trusted people should have write access to this relay: authorized issue contents can
be edited by repository writers. Final instructions are fetched again before submission;
GitHub edits/revocations are not atomic with a local queue call.

Harmless test body:

> Bridge test: acknowledge receipt. Do not modify files, run tests or perform external actions.

Delivery does not expand Codex execution permissions. Normal Codex sandbox and approval
requirements continue to apply. The bridge passes issue text as one argument to
`codex queue --thread UUID --message TEXT`, using an argument array without a shell.
It never interprets task text as commands, approves actions, merges, deploys, or calls a paid API.

## Delivery state and recovery

| State | Meaning |
| --- | --- |
| Open + codex-send, no delivery label | Pending explicitly approved instruction |
| codex-dispatching | Durable submission intent; delivery may be in progress |
| codex-queued | Queue exit 0 and a message identifier were recorded; **not task completion** |
| codex-failed | Queue command failed or submission could not start; no automatic retry |
| codex-uncertain | Timeout, interrupted process, or unrecognized response; investigate manually |

Status labels are additive: `codex-send` and `codex-dispatching` remain as history.
A final label takes precedence. Local `.runtime/receipts.json` contains the repository,
issue number, target session, body SHA-256, status, timestamp, queue exit code when available,
and message ID when confirmed. `.runtime/activity.log` contains metadata only; task bodies
and subprocess stderr are not logged. The runtime directory and config are excluded from Git.
Receipt updates use atomic replacement and fsync. Corrupt state stops delivery.

Each issue is attempted at most once by this installation, including after restart, relabeling,
or changing the target. A restart converts any local dispatching receipt into uncertain.
GitHub status updates retry independently without resubmission. A GitHub dispatch marker
also blocks delivery if local state is lost. Never delete receipts or delivery labels to retry.

For an uncertain or failed issue, inspect `activity`, the original Codex terminal, and its
session history. A nonzero queue exit does not justify a blind retry. If you establish that
the instruction was never delivered, explicitly authorize a **new issue** referencing the
old issue. If delivery is still uncertain, leave it held. Queue success alone never proves
that Codex executed or completed the work.

File locks prevent duplicate listeners per installation and per target session across
installations for the same macOS user. This utility is intended for one Mac; do not run
separate listeners against this relay from other machines. It is not a distributed queue.
Locks are released by the OS on exit; never delete lock files to force another listener.

## Completion reports

Queued instructions include a completion convention. When the task permits external
reporting, the existing Codex agent can write a concise Markdown result to a temporary file
and post it to the originating issue:

```sh
gh issue comment ISSUE_NUMBER --repo norman8823/codex-bridge --body-file /tmp/codex-result.md
```

Use this content format:

```text
Outcome: completed | blocked | failed
Summary: ...
Files changed: ... (or none)
Tests and results: ... (or not run, with reason)
Remaining decisions/blockers: ... (or none)
```

This reports work explicitly; the listener does not infer completion or scrape/upload session
history. Tasks that forbid external actions receive their acknowledgement only in the Codex
terminal. ChatGPT can inspect the issue and comments on a subsequent turn.

## Setup on another installation

Use Python 3.9+, GitHub CLI, and the existing Codex CLI supporting `codex queue`.
Authenticate with `gh auth login --hostname github.com`. Confirm access using
`gh repo view norman8823/codex-bridge --json isPrivate,url`.
If configuring a new repository, create it explicitly private using `gh repo create OWNER/REPO --private`.
Never substitute an application repository or public repository.

Copy the tracked `config.example.json` to `config.json`, configure your own session UUID,
repo, authorized GitHub usernames and polling interval, then run:

```sh
~/codex-bridge/codex-bridge setup-relay
python3 -m unittest discover -s ~/codex-bridge/tests -v
```

`setup-relay` verifies the configured existing repository is private and creates missing
labels. `once` performs a single real polling cycle; use it only when delivery is intended.

## Troubleshooting

- **GitHub unavailable:** run `gh auth status` in your normal terminal, check network and private
  repository access. Polling resumes automatically after transient GitHub errors. An API failure
  before submission never launches Codex. GitHub credentials stay in `gh`/macOS Keychain.
- **Session missing/offline:** inspect the original Codex terminal. The queue command is the authority
  on reachability. The bridge never substitutes a session. If queue accepts a message but the terminal
  is offline, queued still does not mean received; check the original terminal before proceeding.
- **No delivery:** verify the issue is open, has `codex-send`, has no previous delivery markers,
  has a nonempty body, and its author and label actor are allowlisted. Only full UUID targets are accepted.
- **Unexpected queue format:** a success without a recognizable message UUID is uncertain. Check
  the original session before adjusting the parser or authorizing a new issue.
- **Bridge stops:** malformed state/config or local I/O failures stop the process conservatively.
  Inspect local files, preserve receipts, fix the cause and restart. An interrupted dispatch is held.
- **Long stop:** a single queue call has a 45-second timeout; GitHub calls have 30-second timeouts.
  Let an in-flight polling cycle finish. Ctrl-C requests a clean stop; force-killing creates uncertainty.
- **Running status:** status probes the OS file lock, not a stale PID file. Runtime details include
  the listener PID and session. The bridge does not send keystrokes or attach to the Codex terminal.

No launchd service is installed. Closing the dedicated bridge terminal stops polling.
