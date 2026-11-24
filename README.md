# science-bridge

Drive and monitor **Claude Science** from **Claude Code**, so you can also do it from your phone.

Claude Science is a great research workbench, but it lives in one browser tab on one Mac. Long runs, such as a weekend retrain or a 500-candidate screen, mean checking back at the laptop. science-bridge connects it to Claude Code:

- **MCP tools.** Any Claude Code session can list sessions, read transcripts, send messages, approve plans, cancel, resume and start new sessions.
- **`scb watch`.** A watcher that prints one line whenever something needs you: an approval card or question waiting, a plan to approve, a run that finished, failed, stalled or crashed, the daemon down, or the nightly backup failing.
- **Approval cards from anywhere.** Network, credential, local-exec and cost cards, plus the agent's questions, can be answered remotely (allow once, allow for the session or project, always, deny, or answer).
- **`/science-hub`.** A skill that turns a Claude Code session into a hub. It watches those events and sends **native push notifications through the Claude app**, via Remote Control. Reply from your phone and it relays your answer into Claude Science.

```
Claude Science daemon ─▶ scb watch ─▶ Monitor in hub session ─▶ PushNotification ─▶ 📱 Claude app
                                                   ▲                                     │
                    science-bridge MCP tools ◀─────┴──────────── your reply ◀────────────┘
```

## Install

Requires macOS, [uv](https://docs.astral.sh/uv/), Claude Code, and Claude Science running.

```bash
cd ~/repos/science-bridge
uv sync
uv run scb doctor                      # database, daemon, login all ok?

# MCP server, available in every Claude Code session
claude mcp add --scope user science-bridge -- uv run --directory ~/repos/science-bridge scb-mcp

# the /science-hub skill
ln -s ~/repos/science-bridge/skills/science-hub ~/.claude/skills/science-hub
```

## Use

**From your phone, for long runs:**

```bash
~/repos/science-bridge/bin/science-hub   # starts `claude /science-hub` and keeps the Mac awake
```

Then, in that session, run `/remote-control` and open it in the Claude app. You'll get pushes like:

> Model retrain wants plan approval: retrain on the v4 dataset, 3 h est. Reply "approve" or edit.

Reply in the app: *"approve"*, *"yes, for this session"*, *"v4"*, *"ask it why the validation score dropped"*, *"cancel the backfill"*.

> Sensor backfill wants network access to api.example.org (pulling raw readings). Reply: once / this session / project / deny

**From any Claude Code session:** just ask, e.g. "what's running in Claude Science?" or "tell the backfill session to use the v2 API".

**From the terminal:**

```bash
scb status            # daemon + running / waiting sessions
scb ls -p retrain     # sessions in projects matching "retrain"
scb read 26e2b3f6 -n 5 [-t]   # last 5 turns (-t includes tool calls)
scb send 26e2b3f6 "also plot the residuals by site"
scb watch             # event stream
```

Session refs can be a full id, an 8-character prefix, or part of the name.

## How it works

- **Reads** come straight from Claude Science's SQLite database (`~/.claude-science/orgs/<org>/operon-cli.db`). The database is opened **read-only**, so the bridge never writes to it.
- **Cards** are read from each live frame's output JSON (`frame_blobs`, kind `output`, key `pending_input_requests`) and answered with `POST /api/frames/<frame>/resolve-input`.
- **Messages** are sent with `POST /api/request` (`{target_agent, root_frame_id | project_id, input_data: {request}}`), the same call the chat box makes.
- **Writes** go through the daemon's own HTTP API, the same endpoints the Claude Science UI calls, so the daemon stays in charge of its data. Auth works the same way as the UI. `claude-science url` mints a one-time login nonce, which is exchanged for a session cookie. The cookie is stored at `~/.science-bridge/cookies.txt` (mode 0600) and renewed automatically.
- **Watcher state** lives in `~/.science-bridge/watch-state.json`. Restarting the watcher, which the hub does every 30 min when the Monitor expires, never repeats or drops an event.

## Event kinds

| Kind | Meaning |
|---|---|
| `CARD` | an approval card or `ask_user` question opened, including ones from sub-agents |
| `NEEDS_YOU` | session status became `awaiting_plan_approval` or `awaiting_user_response` |
| `DONE` | a running session completed |
| `FAILED` | `failed`, `error` or `cancelled` |
| `STALLED` | still `processing`, but no update on it or any sub-agent for `--stall-min` (default 45) minutes |
| `DAEMON` | the Claude Science daemon went down or came back up |
| `BACKUP` | [claude-science-backup](https://github.com/adamkoeppel/claude-science-backup)'s log reports a failure |

## Limits and caveats

- **Unofficial.** This uses Claude Science's internal API and schema, built against **v0.1.72**. A Claude Science update can break it, so run `scb doctor` after updates.
- **Approval cards** are answered through the same `resolve-input` call the card buttons use. The card's contents come from the database, with secret-looking fields redacted. Cards are reverse-engineered, so if one kind looks odd in `list_cards`, answer it in the window and file an issue.
- **The Mac has to stay awake and online**, which long Claude Science runs need anyway. `bin/science-hub` uses `caffeinate`.
- **Pushes need the hub session connected via Remote Control.** Without that, they appear as desktop notifications only.

## Development

```bash
uv run --group dev pytest -q
```
