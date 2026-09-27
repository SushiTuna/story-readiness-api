# Installing the Story Board MCP server

`story-board-mcp` lets AI coding agents read and change the Story Board: list boards and stories, create and edit stories, move them between columns, and assess them. It is a local **stdio** server. Your agent starts it as a subprocess, and it reads and writes the same SQLite database as the API (`STORIES_DB_PATH`, default `data/stories.db`). The API doesn't need to be running for the agent to work. The [story-board](../story-board) web app shows the agent's changes when you switch back to its tab.

This guide covers **Claude Code**, **GitHub Copilot CLI** and **OpenAI Codex CLI**. For the list of tools and what gets logged, see [MCP server for AI agents](README.md#mcp-server-for-ai-agents) in the README.

- [1. Prerequisites](#1-prerequisites)
- [2. Claude Code](#2-claude-code)
- [3. GitHub Copilot CLI](#3-github-copilot-cli)
- [4. OpenAI Codex CLI](#4-openai-codex-cli)
- [5. Try it](#5-try-it)
- [6. Install with uvx (no clone)](#6-install-with-uvx-no-clone)
- [Troubleshooting](#troubleshooting)

---

## 1. Prerequisites

1. Install [uv](https://docs.astral.sh/uv/) and **Python 3.12+**.
2. Install the project, which also installs the `story-board-mcp` command:

   ```bash
   cd /path/to/story-refinement
   uv sync
   ```

3. Copy `.env.example` to `.env` and set `TYPESAFE_API_KEY`. Only `assess_story` needs it; the other tools work without it.
4. Check that the server starts:

   ```bash
   uv run --directory /path/to/story-refinement story-board-mcp
   ```

   Nothing is printed, because the server waits for a client on stdin. Press `Ctrl+C` to stop it. To call its tools by hand, run it in the MCP Inspector instead:

   ```bash
   npx @modelcontextprotocol/inspector uv run --directory /path/to/story-refinement story-board-mcp
   ```

Every client below runs the same command:

```text
uv run --directory /path/to/story-refinement story-board-mcp
```

Replace `/path/to/story-refinement` with the absolute path of this repository (`pwd` in the repo prints it). `--directory` makes uv run from the repository, so the relative default `data/stories.db` and the `.env` file are found wherever the agent itself was started.

### Name each agent

Every change an agent makes is logged under a name, and that name is shown on the story's card. The server uses `STORY_AGENT_NAME` if it is set. Otherwise it uses the name the client reports about itself, which you don't control. Set `STORY_AGENT_NAME` in each client's config, as the examples below do, so the activity log says clearly which agent made each change.

| Variable | Default | Purpose |
|---|---|---|
| `STORY_AGENT_NAME` | The client's self-reported name, else `mcp-agent` | Name the changes are logged under |
| `STORIES_DB_PATH` | `data/stories.db` (relative to the repository) | SQLite file. It must be the same file the API uses, or the web app won't show the agent's changes |
| `TYPESAFE_API_KEY` | from `.env` | Needed by `assess_story` |

---

## 2. Claude Code

### Option A: CLI (recommended)

```bash
claude mcp add --transport stdio --scope user story-board \
  -e STORY_AGENT_NAME=claude-code \
  -- uv run --directory /path/to/story-refinement story-board-mcp
```

- Everything after `--` is the command that starts the server.
- `--scope` sets where the entry is stored:
  - `local` (the default): only the current project, stored in `~/.claude.json`.
  - `user`: every project, stored in `~/.claude.json`.
  - `project`: stored in `.mcp.json` in the project root, so it can be committed and shared with the team.
- The server is useful from any repository, so `user` is a good fit.

### Option B: `.mcp.json` (project scope, shared with the team)

Put this in the root of the repository the team opens in Claude Code:

```json
{
  "mcpServers": {
    "story-board": {
      "command": "uv",
      "args": ["run", "--directory", "${STORY_REFINEMENT_DIR:-../story-refinement}", "story-board-mcp"],
      "env": {
        "STORY_AGENT_NAME": "claude-code"
      }
    }
  }
}
```

- Claude Code starts stdio servers from the session's working directory. The default `../story-refinement` therefore works when Claude Code is opened in the root of the sibling `story-board` repository.
- Anyone with a different layout sets `STORY_REFINEMENT_DIR` to an absolute path. Claude Code expands `${VAR}` and `${VAR:-default}` in `.mcp.json`.
- Claude Code asks for approval before using servers from a project `.mcp.json`.

### Check

```bash
claude mcp list              # story-board should be listed and connected
claude mcp get story-board   # command, scope and status
```

Inside a session, `/mcp` shows the server's status and its tools.

### Remove

```bash
claude mcp remove story-board --scope user
```

---

## 3. GitHub Copilot CLI

### Option A: CLI

```bash
copilot mcp add story-board \
  --env STORY_AGENT_NAME=copilot-cli \
  -- uv run --directory /path/to/story-refinement story-board-mcp
```

This adds the server to your user configuration, `~/.copilot/mcp-config.json`. Add `--tools "list_boards,list_stories,get_story,get_activity"` to allow only the read-only tools.

In an interactive session, you can use `/mcp add` instead. Fill in the fields, moving between them with `Tab`, and save with `Ctrl+S`.

### Option B: edit `~/.copilot/mcp-config.json`

```json
{
  "mcpServers": {
    "story-board": {
      "type": "local",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/story-refinement", "story-board-mcp"],
      "env": {
        "STORY_AGENT_NAME": "copilot-cli"
      },
      "tools": ["*"]
    }
  }
}
```

Copilot CLI also reads workspace config from `.mcp.json` or `.github/mcp.json` in a repository. If both exist in the same folder, `.mcp.json` wins.

### Check

```bash
copilot mcp list               # every server, from every config source
copilot mcp get story-board    # this server's config
```

Inside a session, `/mcp` opens the dashboard with each server's status, and `/mcp show story-board` lists its tools.

### Remove

```bash
copilot mcp remove story-board
```

---

## 4. OpenAI Codex CLI

### Option A: CLI

```bash
codex mcp add story-board \
  --env STORY_AGENT_NAME=codex \
  -- uv run --directory /path/to/story-refinement story-board-mcp
```

### Option B: edit `~/.codex/config.toml`

```toml
[mcp_servers.story-board]
command = "uv"
args = ["run", "--directory", "/path/to/story-refinement", "story-board-mcp"]
env = { STORY_AGENT_NAME = "codex" }
# The first start may install dependencies; allow more than the 10-second default.
startup_timeout_sec = 30
```

Other supported keys:
- `cwd`: the working directory to start the server in. It isn't needed here, because `--directory` does the same job.
- `tool_timeout_sec`: default 60. Raise it if `assess_story` times out.
- `enabled = false`: turns the server off without deleting its entry.

Codex also reads a project-scoped `.codex/config.toml`, but only in projects you have marked as trusted.

### Check

```bash
codex mcp list
```

Inside a session, `/mcp` lists the active servers.

### Remove

Delete the `[mcp_servers.story-board]` section from `~/.codex/config.toml`, or set `enabled = false` to keep it but turn it off. Run `codex mcp --help` to see which other `codex mcp` commands your version has.

---

## 5. Try it

1. Start the API and the web app (see the [README](README.md#running) and the story-board README), then open http://localhost:5173.
2. Ask the agent, for example:
   - "List the boards on the story board."
   - "Show me the stories in Refinement on board ST, and which ones are not ready."
   - "Tighten the acceptance criteria of ST-3, then assess it."
   - "Move ST-5 to Blocked, because we are waiting for the payment provider contract."
3. Switch back to the board tab. Stories the agent changed show an **Agent · name** chip. The story's side panel has an **Activity** section listing each change with the agent's note.

Every write tool requires a `note` (1–500 characters) saying why the change was made, so the activity log records the reasoning with each change. There are no delete tools.

---

## 6. Install with uvx (no clone)

[uvx](https://docs.astral.sh/uv/guides/tools/) builds the package from the git repository into its own cached environment and runs `story-board-mcp`, so you don't need a clone or `uv sync`. Two things are different from `uv run --directory`:

- **The `.env` file isn't read**, because the server no longer runs from this folder. Pass `TYPESAFE_API_KEY` (and `STORY_AGENT_NAME` if you want) in the client's server config.
- **Set `STORIES_DB_PATH` to an absolute path.** The default `data/stories.db` is relative to wherever the client starts the server, so each client would get its own empty database. To share data with the board, use the `data/stories.db` of the clone that runs the API, or of the container setup ([Run with containers](README.md#run-with-containers)).

At startup the server logs `Story database: <absolute path>` to stderr, so you can check which file it uses in the client's MCP logs.

Claude Code:

```bash
claude mcp add story-board \
  -e TYPESAFE_API_KEY=your-key \
  -e STORIES_DB_PATH=/abs/path/story-refinement/data/stories.db \
  -- uvx --from git+https://github.com/SushiTuna/ibm-hackathon-template story-board-mcp
```

Other clients take the same command, arguments and environment, for example in JSON:

```json
{
  "mcpServers": {
    "story-board": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SushiTuna/ibm-hackathon-template", "story-board-mcp"],
      "env": {
        "TYPESAFE_API_KEY": "your-key",
        "STORIES_DB_PATH": "/abs/path/story-refinement/data/stories.db"
      }
    }
  }
}
```

- uvx keeps the version it built. To pick up new commits, run `uvx --refresh --from git+https://github.com/SushiTuna/ibm-hackathon-template story-board-mcp` once, or pin a release with `git+https://…@v0.1.0`.
- From a local clone, `uvx --from /abs/path/story-refinement story-board-mcp` works the same way.

**Running the containers?** If agent changes don't show up, or you see `database is locked`, while the API runs in a container, let the agent use the MCP server inside that container instead, so both write the file from the same place. Use this as the command:

```bash
podman exec -i story-api story-board-mcp
```

It uses the container's `.env` and database, so no `env` block is needed. The container must be running.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Failed to spawn: story-board-mcp` | Run `uv sync` in the repository. That installs the command. |
| The client can't find `uv` | Desktop and IDE apps may not have your shell's `PATH`. Use the absolute path that `which uv` prints as the command. |
| The server times out on first start | The first `uv run` may install dependencies. Run `uv sync` once, or raise the client's startup timeout (Codex: `startup_timeout_sec`). |
| Agent changes don't appear on the board | Click back into the board tab (it refetches on focus), and reopen an open story panel. Check that the API and the MCP server use the same `STORIES_DB_PATH`. |
| `assess_story` fails with "Assessment service unavailable" | Set `TYPESAFE_API_KEY` in `.env`. `assess_story` makes one TypeSafe/Jev call each time it runs. |
| "note is required" | Every write tool needs a note. Ask the agent to say why it's making the change. |
| "A blocked story needs a blocked_reason" | Moving a story to `blocked` requires `blocked_reason`. |
| The agent sees an empty board under uvx | `STORIES_DB_PATH` is missing or relative. Check the `Story database:` line in the client's MCP logs and set an absolute path ([section 6](#6-install-with-uvx-no-clone)). |
| Changes are logged under an unexpected name | Set `STORY_AGENT_NAME` in that client's server config. |

---

## Sources

The client commands and config formats come from each client's own documentation:

- Claude Code: [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp), and `claude mcp add --help` (Claude Code 2.1.283)
- GitHub Copilot CLI: [Adding MCP servers for GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers), [Using GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli), and `copilot mcp add --help` (Copilot CLI 1.0.87)
- OpenAI Codex CLI: [Model Context Protocol – Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- uvx: [Using tools – uv](https://docs.astral.sh/uv/guides/tools/)
- MCP Inspector: [modelcontextprotocol/inspector](https://github.com/modelcontextprotocol/inspector)
