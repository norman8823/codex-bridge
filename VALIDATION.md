# Installation and end-to-end validation

Validated on 2026-10-15. Standalone installation: `/Users/norman/codex-bridge`.

- Codex CLI: `0.162.1`; local `queue --help` confirms existing-session delivery.
- GitHub CLI: `2.94.0`; authenticated as `norman8823` through macOS Keychain.
- Python: `3.14.6`; implementation uses only standard-library modules.
- Relay: `norman8823/codex-bridge`, verified private through the GitHub API.
- All five dispatch/status labels verified/created.
- Target: `01a14055-7b04-7483-9c0a-e01f79e7f738`.
- The original interactive process was observed holding this session's writer lock.

## Regression tests

`python3 -m unittest discover -s tests -v`: **28 tests passed**.

Coverage includes correct session/body delivery, missing label, unauthorized author and label
actor, wrong repo, closed issues and PRs, prior delivery markers, repeated polls and restarts,
queue failure, timeout, crash during submission, missing session/executable, GitHub outage and
recovery, privacy rejection, status-sync failure without resubmission, unexpected output,
durable intent before invocation, authorization-time edits, corrupt state, session changes,
invalid session IDs, target locking, stop requests, paginated GitHub API calls, approval
revocation and sanitized error handling. Tests use mocked subprocesses and GitHub responses.

## Real end-to-end test

Issue: https://github.com/norman8823/codex-bridge/issues/1

Created through this conversation's connected GitHub integration, authored by `norman8823`,
with `codex-send` and this exact body:

> Bridge test: acknowledge receipt. Do not modify files, run tests or perform external actions.

- Submission: `2026-10-15T19:04:45Z`.
- Queue exit code: `0`.
- Queue message ID: `01a140f3-dacf-78d3-a678-16bec06b98b1`.
- Durable local receipt stored in `.runtime/receipts.json`.
- Issue acquired `codex-dispatching` and `codex-queued`.
- Original session recorded the relayed issue at `2026-10-15T19:04:51.910Z`.
- Original session's final response at `2026-10-15T19:04:54.420Z`:

> Completed: bridge message received. No files changed, tests run, or external actions performed. No blockers or remaining decisions.

This response was read from the original session's local transcript. The target agent did
not post externally because this test explicitly prohibited external actions. This validation
report is posted separately by the installer to make the observed result inspectable.

## Operating checks

The foreground listener was opened in a dedicated macOS Terminal window. Running status,
a clean stop, stopped status, and restart were verified. The test issue was not resubmitted
on restart. No launchd service, separate Codex agent, application integration, or paid API
workload was created. All implementation files are confined to the standalone bridge directory.

Implementation files: `bridge.py`, `codex-bridge`, `Start Bridge.command`, `config.example.json`,
`.gitignore`, `tests/test_bridge.py`, `README.md`, and this validation report. Local ignored
configuration targets the session above and allowlists only `norman8823`.

## Issue #4: explicit pairing and LaunchAgent repair (2026-10-15)

Implemented one bounded repair; no automatic session inference or lifecycle hooks.

- Existing 28 regression cases retained, with task fixtures adapted to explicit envelopes and routes.
- Added 35 focused cases: expiry, one-time codes, ambiguous/unauthorized requests, manual UUID
  fallback (including inherited nested context), two routes/sessions, clear/re-pair isolation,
  missing/unknown/held routes, no-rollout failures, duplicate delivery IDs, interrupted submissions,
  historical receipts, bounded logging and launchd control/policy.
- Validation command: `python3 -m unittest discover -s tests -v`. The initial run passed 62 cases;
  one new test incorrectly expected corrupt route JSON to return from polling rather than abort.
  Corrected that assertion to require the existing fail-closed exception. Its focused rerun passed:
  `PYTHONPATH=tests python3 -m unittest test_reliability.RoutingTests.test_corrupt_route_registry_fails_closed -v`.
  All 63 cases were thereby validated; no production implementation change was needed during validation.
- `git diff --check` passed. No paid API calls or other repository changes.
- Local config and global Codex configuration retain their pre-repair SHA-256 hashes.
  Historical receipts #1–3 remained byte-identical through installation and the service tests;
  the single new delivery subsequently added only its own receipt.

Live macOS service validation:

- Installed `~/Library/LaunchAgents/com.norman.codex-bridge.plist` and started it successfully.
- Confirmed authenticated access to the existing private relay from the LaunchAgent's environment.
- Explicit stop unloaded the job and persisted launchd's disabled state. It stayed stopped for
  12 seconds, exceeding the 10-second restart throttle. Unloading is asynchronous; the first
  immediate check observed the job during unload, and the subsequent read confirmed it absent.
- Explicit start re-enabled it. Killed only the confirmed idle listener once, before introducing
  test routes/tasks. Launchd automatically restarted it with a different PID (63596 → 63605).
- Config and historical delivery receipts were unchanged by the crash/restart.
- Service left enabled/running. Useful diagnostics are bounded in `.runtime/activity.log`.

One real pairing/delivery test, explicitly designated by the user to this bridge-repair session:

- `CODEX_THREAD_ID` was present and matched the session identifier established for this conversation.
- Pairing issue: https://github.com/norman8823/codex-bridge/issues/5
- GitHub briefly omitted the new issue from the list immediately after creation. The initial lookup
  refused before claiming the code; the same one request was confirmed after it became visible.
  No second pairing request or extra delivery was created.
- Route: `bridge-repair-test`; non-sensitive pairing confirmation posted successfully.
- Task issue: https://github.com/norman8823/codex-bridge/issues/6
- Task: acknowledge only; no files, tests, agents or external actions.
- Delivery ID: `22b51421-7f4d-4666-8323-1bd0ab055a3c`.
- Queue exit 0; message ID: `01a1417c-2dcd-73f2-82b5-8a2d9da51244`.
- Attempted session: `01a14164-29af-7c33-a220-bcd8395e8bff` (the designated current session).
- Durable local queued receipt and GitHub dispatching/queued labels recorded once.
- **Destination acknowledgment is pending while this implementation turn is active.** Queue
  acceptance is not claimed as receipt, execution or completion. No second message will be sent.
- Test route held after queueing; holding does not cancel or resend the accepted message.
- No real `/clear` test was performed. Re-pair behavior is covered by regression tests.

Limitations: the bridge cannot automatically observe terminal closure or `/clear`. Hold the route
before switching/clearing/closing, then explicitly confirm a new pairing request inside the new
session. Manual `--session UUID` confirmation is required if the shell context is absent or uncertain.
Live terminal receipt/completion remain unverified until explicitly acknowledged by the destination.
