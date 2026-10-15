# Codex bridge

Standalone private GitHub Issues → **existing local interactive Codex CLI session**.
Python standard library, `gh`, and `codex queue`; no cloud agent, API key, hooks, or application integration.
One installation on one Mac. Normal Codex execution permissions still apply.

## Install and operate

From your normal authenticated macOS terminal:

```sh
cd ~/codex-bridge
# On a new installation only: copy config.example.json to config.json and configure it.
gh auth status
./codex-bridge install
./codex-bridge status
```

`install` checks the existing private relay, adds missing bridge labels, and installs/starts
`~/Library/LaunchAgents/com.norman.codex-bridge.plist`. Existing config and receipts are preserved.
GitHub authentication uses `gh`'s normal configuration/macOS Keychain. The plist records executable
paths and configuration directories, never tokens. Authenticate normally with `gh auth login` if needed.

```sh
~/codex-bridge/codex-bridge start     # enable/start; also re-enables login startup
~/codex-bridge/codex-bridge stop      # disable across logins and unload; stays stopped
~/codex-bridge/codex-bridge restart
~/codex-bridge/codex-bridge status
~/codex-bridge/codex-bridge routes
~/codex-bridge/codex-bridge logs
~/codex-bridge/codex-bridge activity  # includes durable delivery receipts
~/codex-bridge/codex-bridge uninstall
```

Launchd starts the service at login and restarts it after exit, with a 10-second throttle.
No Terminal window is needed. OS file locks prevent duplicate listeners for this installation and
concurrent submissions to the same session across installations. Explicit stop disables the service
before unloading it. Shutdown lets the current queue call persist its result. A forced kill or a
shutdown timeout can leave an uncertain receipt, which is never retried automatically.
Logs rotate at about 1 MB, retaining one prior file. They contain metadata, not task bodies or raw
subprocess output. `status` distinguishes listener state, LaunchAgent state, relay connectivity,
route state, last-poll time, blocked pending issues and durable receipt counts. A paired route's
**live terminal availability remains unverified**. Running is not proof of delivery.
For foreground diagnosis only, use `listen`; `once` performs one real poll. Stop the service first.

## Pair a ChatGPT chat with a terminal

Tell ChatGPT with GitHub **write access**:

> Connect this chat to route `cmbs-costs`. Create one pairing request in
> `norman8823/codex-bridge`, authored by my allowlisted account, labeled only `codex-pair`.
> Use a fresh random single-use code of 16–64 letters/digits/underscores/hyphens and an
> expiry no more than 15 minutes after issue creation. Give me the code. Do not label it `codex-send`.

The pairing issue body must be a JSON object (no Markdown fences):

```json
{"route":"cmbs-costs","code":"FRESH_RANDOM_CODE_AT_LEAST_16_CHARS","expires_at":"UTC_ISO_TIMESTAMP"}
```

Inside the **intended existing interactive Codex session**, say:

> Connect bridge with code CODE. Run `~/codex-bridge/codex-bridge pair CODE`
> using this session's shell tool, without launching another Codex session.

Codex supplies `CODEX_THREAD_ID` to that shell context. The command prints the selected UUID and
posts confirmation of the route to the pairing issue, omitting the code, UUID and local paths.
Check the printed UUID against `/status` if the context is uncertain. **Do not inherit the variable
into a nested Codex session and treat it as the nested session's identity.** The explicit fallback is:

```sh
~/codex-bridge/codex-bridge pair CODE --session UUID_FROM_STATUS
```

This is the operator's manual UUID confirmation and overrides an inherited variable. Missing or
invalid context never chooses the most recent session. Pairing confirms the user's selection;
it does not prove that Codex can queue to the session yet, or verify a ChatGPT conversation identity.
There is no automatic ChatGPT conversation ID. Each chat intentionally chooses a route.
Two chats can select different routes without changing one global destination.

Requests must be open issues in the configured private relay, with an allowlisted author and
latest `codex-pair` label actor. Duplicate matching codes, executable pairing requests, expired,
changed or previously consumed requests are refused. A durable local claim and `codex-paired`
marker prevent code reuse. An interrupted claim requires a new request. Failed confirmation comments
are retried by the listener without repeating the pairing operation. Don't delete the local registry.

## Send approved tasks

Tell ChatGPT:

> Use route `cmbs-costs` for this chat. For each task I approve, create exactly one issue in
> `norman8823/codex-bridge`, with the approved title, label `codex-send`, and this JSON body:
> route, a fresh UUID delivery_id, and the complete approved instructions. Do not send unapproved tasks.

```json
{"route":"cmbs-costs","delivery_id":"a85dd9e4-565c-4d58-a4eb-fc9bb77aeaa1","instructions":"Complete approved instructions, constraints and acceptance criteria."}
```

Generate a **new** delivery UUID for each new task; retain it if an issue creation response is uncertain.
Title comes from the issue title. Both author and latest `codex-send` label actor must be allowlisted.
The full current issue is fetched again before submission. Missing/unknown/held routes, duplicate JSON
fields, duplicate attempted delivery IDs and old-format tasks fail closed. Pending refusals appear in
`status`; correct a never-attempted pending issue only after checking why it was blocked.
Pairing issues never execute. Task text is passed as an argument array to `codex queue`, never a shell.
GitHub edits/revocations are not atomic with a local queue call; restrict relay write access to trusted people.

## Changing sessions, `/clear`, and recovery

**There is no automatic detection of `/clear`, terminal closure, or a session switch.** A remembered
UUID may still accept a queue message while its old chat is no longer visible. Before `/clear`,
resuming a different chat, or closing the destination, hold its route:

```sh
~/codex-bridge/codex-bridge unpair cmbs-costs
```

After the change, ask ChatGPT for a **new pairing request/code** and confirm it inside the new session.
The logical route name stays the same. Pair again the same way to move a route to another terminal.
Holding a route waits for any in-flight dispatch lock; it cannot recall an already queued message.
If you already cleared without holding the route, hold it immediately and inspect prior receipts.
The bridge cannot retroactively prove that a task reached the terminal you are now viewing.

Receipts remain durable across restart and re-pairing. Historical issues #1–3 are never replayed.
The legacy `session` config key/`set-session` are preserved for compatibility but **never select a
new task's destination**. Existing config and receipts need no migration.

| State | Meaning / action |
| --- | --- |
| Pending | No queue attempt; held or invalid envelopes appear in last-poll health |
| Dispatching | Durable intent before queue invocation; interrupted intent becomes uncertain |
| Queued | Codex returned a message ID; destination receipt, execution and completion unverified |
| Failed | Queue failed; route held; no automatic retry |
| Uncertain | Timeout, interruption or unrecognized response; reconcile manually; route held |

`no rollout found` is a failure requiring explicit re-pairing and reconciliation. **It is not success
and is not retried.** Re-pairing never releases an attempted issue or delivery UUID for another attempt.
For any failed/uncertain/obsolete-session attempt, inspect the original terminal/history and receipt's
attempted UUID/message ID. Only after establishing that it was not executed may you approve a new
issue with a new delivery ID referencing the old issue. If uncertain, leave it held. Never delete
receipts or dispatch labels to force a retry. Even a nonzero exit is not proof of non-delivery.
GitHub outages preserve local state and tasks; status-label and pairing-confirmation retries do not
requeue instructions. Corrupt state fails closed.

The destination can post an explicit result to its originating issue when the task allows external
reporting (`gh issue comment NUMBER --repo norman8823/codex-bridge --body-file FILE`). Include outcome,
summary, changed files, tests and blockers. The listener never infers acknowledgment or completion.

Uninstall removes only this LaunchAgent and preserves config, routes, receipts and logs. No global
Codex hooks or settings are modified. For a revert, stop/uninstall before restoring older code;
never restore an older receipt file or remove historical delivery labels.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

Tests use fake GitHub/subprocess responses and no paid APIs. See VALIDATION.md for the bounded local
validation and the distinction between tested code, service operation and real destination receipt.
