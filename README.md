# DevRev Agent Chat

[![Tests](https://github.com/shashankcube/devrev-agent-chat/actions/workflows/tests.yml/badge.svg)](https://github.com/shashankcube/devrev-agent-chat/actions/workflows/tests.yml)

Chat with a DevRev AI agent from your terminal. Paste a token once, choose an agent, and start talking—with file uploads, live progress, and automatic downloads of generated files.

An unofficial Python tool for the DevRev async agent API. Requires Python 3.9+ on macOS or Linux, a DevRev token, and an agent with access to the async container runtime. No pip dependencies.

## Start

```sh
git clone https://github.com/shashankcube/devrev-agent-chat.git
cd devrev-agent-chat
python3 chat.py
```

On first launch:

```text
DevRev token (hidden):
Agent (for example ai_agent-48): ai_agent-48
```

Use your agent's ID in the format `ai_agent-<number>`. The script validates it, sets up the connection, and opens a chat. On later launches, press Enter to reuse your last agent or enter another ID.

Your token is remembered in macOS Keychain or Linux Secret Service (`secret-tool`). If a secret store is unavailable, it is used only for the current run. You can also double-click **Start Chat.command** on macOS.

## Send a file, get a file

```text
You> /attach examples/input.csv
You> Calculate quantity × unit_price for each row and return the results as a CSV file.
```

The file is uploaded as a DevRev artifact. Agent progress appears as callbacks arrive; generated artifact files are downloaded automatically and their local paths are printed. File generation depends on the agent's capabilities.

| Command | Action |
| --- | --- |
| `/attach PATH` | Queue a file for your next message; up to 10 files |
| `/files` | List queued files |
| `/clear` | Clear queued files |
| `/new` | Start a fresh DM with the selected agent |
| `/wait` | Continue waiting after a response timeout |
| `/quit` | Close the connection and remove the temporary webhook |

## What is automatic?

The launcher starts a local receiver, opens a temporary HTTPS tunnel through Cloudflare, and **registers a webhook in DevRev** for `ai_agent_response` events. It handles verification and signature checks, then creates or reuses a private DM with the agent. Every invocation uses that DM as both its parent and session object, and requests the container runtime.

You see live progress events and the final message. This API flow does **not** provide token-by-token text streaming. Runtime availability and routing depend on your DevRev deployment; the default API base is `https://api.devrev.ai/internal`.

## Useful options

```sh
python3 chat.py --agent ai_agent-48       # Skip agent selection
python3 chat.py --session research       # Create or resume a named conversation
python3 chat.py --login                  # Replace the remembered token
python3 chat.py --forget-token           # Remove the remembered token
python3 chat.py --no-save-token          # Do not save a newly entered token
```

The default conversation resumes across launches for the same account and agent. `/new` starts a new conversation for the current run; use `--session NAME` for a conversation you want to resume by name.

## Data and credentials

- The DevRev token goes only to the configured DevRev API endpoint. It is never saved in a plaintext config file.
- Webhook payloads, including agent responses, pass through Cloudflare to your computer. The launcher downloads a pinned, checksum-verified `cloudflared` binary from Cloudflare's official GitHub releases.
- Local state, event logs, request receipts, and downloaded files live in `.agent-state/`, which is excluded from Git.
- Normal exit deletes the temporary webhook and stops the tunnel. Failed webhook cleanup is retried on the next launch. DMs and uploaded artifacts remain in DevRev.

## More

- [Options, scripting, and troubleshooting](docs/usage.md)
- [DevRev async agent API guide](https://developer.devrev.ai/beta/guides/agents-async-api)
- [DevRev chats.create reference](https://developer.devrev.ai/beta/api-reference/chats/create)

Run the local tests with `python3 -m unittest discover -v`. They use synthetic fixtures and local mock servers; no DevRev token is required. Contributions are welcome—include tests for behavior changes, and keep credentials and real conversation data out of commits and issues.

[MIT license](LICENSE).
