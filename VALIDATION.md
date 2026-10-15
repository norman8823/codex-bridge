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
