# Usage reference

For the usual workflow, run `python3 chat.py`. The launcher manages the receiver, HTTPS tunnel, DevRev webhook, DM, and artifact transfers.

## Launcher options

| Option | Purpose |
| --- | --- |
| `--agent ai_agent-48` | Select an agent without prompting |
| `--session NAME` | Resume a named DM for this account and agent |
| `--login` | Prompt for a replacement token |
| `--forget-token` | Delete the token from the OS secret store and exit |
| `--no-save-token` | Do not store a newly entered token; existing credentials can still be read |
| `--base-url URL` | Override the API base, including its API path |
| `--state-dir PATH` | Use a different local state directory |
| `--timeout SECONDS` | Wait this long for each response; default 600 |

`DEVREV_TOKEN` supplies a token without prompting. `DEVREV_BASE_URL` overrides the default API base. A custom base receives your bearer token, so use an endpoint you trust. Token storage is scoped to the API base.

Only one chat process can use a given state directory at a time. Use separate directories for simultaneous chats. Default state is `.agent-state/chat/` beside the script. Keep any custom state directory out of version control too.

## What happens on each turn

1. Queued files are uploaded through `artifacts.prepare` and its signed storage URL.
2. The script calls `ai-agents.events.execute-async` with a DM as `parent` and `session_object`, `agent_options.runtime = "container"`, the registered webhook, and artifact IDs in `message_context.artifacts`.
3. Signed `ai_agent_response` callbacks are saved locally and correlated using client metadata, session, and agent.
4. The final response's artifact references are resolved through `artifacts.locate` and downloaded. Storage requests do not receive the DevRev bearer token.

A new DM is private and non-default, with the caller and selected agent as members. Its ID is saved locally for reuse. The script never silently substitutes a non-DM parent.

## Small scripts and individual operations

`agent.py` exposes the same building blocks for automation. It reads `DEVREV_TOKEN`; it does not read the launcher's OS keychain. For a simple paste-and-go token prompt, use `chat.py`; the pasted token is visible in the terminal.

```sh
python3 agent.py --help
python3 agent.py upload examples/input.csv
python3 agent.py download '<artifact-id>' --output result.csv
python3 agent.py download-files '<saved-final-event.json>' --directory downloads
```

To submit with an existing active DevRev webhook and running receiver:

```sh
python3 agent.py invoke \
  --agent ai_agent-48 \
  --webhook '<webhook-id>' \
  --session report \
  --message 'Summarize this CSV and return a results file.' \
  --file examples/input.csv \
  --wait --events events.jsonl \
  --result final.json --download-dir downloads
```

`--session report` creates or reuses a DM automatically. `--parent '<dm-id>'` explicitly selects an existing DM. The receiver's event-log path must match `--events`; invoking alone does not start a receiver. Prefer `chat.py` when you want automatic setup.

Other subcommands include `serve`, `secret`, `webhook-create`, `webhook-get`, `parent-create`, and `watch`; each has `--help`. `--base-url` is a global option and goes before the subcommand. Top-level API `request_id` is opt-in via `--api-request-id` for deployments supporting it; callback correlation always uses `client_metadata.tool_request_id`.

You can also import `agent.Client`, `agent.ensure_dm`, `agent.invoke_body`, and `agent.watch` in your own Python scripts. These are small implementation helpers, not a versioned SDK.

## Troubleshooting

**Token rejected.** Run `python3 chat.py --login`. A valid token still needs access to the agent, chats, artifacts, webhooks, and async execution APIs.

**Agent unavailable.** Check the ID and workspace. Enter `ai_agent-` followed by the number; the launcher expands it into the full organization-scoped identifier.

**API returns 404 or rejects runtime options.** Confirm your DevRev deployment supports these APIs and container execution. The default uses `/internal`; access to ordinary public endpoints alone may not be sufficient. Use `--base-url` only when you know the correct alternative for your deployment.

**Webhook verification or tunnel fails.** The machine needs outbound access to DevRev, GitHub releases, and Cloudflare, and DevRev must reach the temporary HTTPS endpoint. Inspect `.agent-state/chat/connector.log`. Quick tunnels are temporary development infrastructure, so use a managed HTTPS receiver for a durable service.

**Response timed out.** Use `/wait`. A timeout or ambiguous network failure does not prove the agent stopped; the launcher does not automatically resubmit. Request receipts are under `.agent-state/chat/runs/`. Quitting stops local callbacks, not necessarily the remote run.

**File download failed.** The final event is saved under `runs/`; retry its artifact references with `agent.py download-files`. Existing files are never overwritten. A textual link without an artifact ID is not downloaded automatically.

**Cleanup failed.** Reopen the same state directory with the same account to retry deletion of the recorded webhook. If necessary, use the webhook ID in `pending-webhook.json` to remove it in DevRev. Abrupt process termination can leave remote resources behind; keep state until cleanup succeeds.

## Development

```sh
python3 -m unittest discover -v
```

Tests cover signed callbacks, replay handling, correlation, artifact transfers, DM reuse, credential handling, agent selection, and launcher behavior. They use local servers and mocks, not a live account. CI runs on macOS and Linux. Windows is not supported by the current file-locking and tunnel setup.
