# Terraform State Inspector MCP Server

An MCP server that gives LLMs read-only access to your Terraform state. Ask natural-language questions like "what resources exist in prod?", "find all buckets without encryption", or "how does staging differ from prod?" — and get structured answers without leaving your editor.

## Tools

| Tool | Description |
|------|-------------|
| `tf_summary` | High-level overview: resource counts by type/module/provider, state metadata |
| `tf_list_resources` | List all resources with optional type/module filters |
| `tf_get_resource` | Full attribute dump for a specific resource by address |
| `tf_search_attributes` | Find resources by attribute path + value (compliance checks) |
| `tf_get_outputs` | List all Terraform outputs and their current values |
| `tf_dependency_graph` | Walk the dependency tree for a given resource |
| `tf_diff_state` | Compare current state against another snapshot (added/removed/changed) |
| `tf_refresh_cache` | Force-reload state from backend after an apply |

All tools support `response_format: "markdown"` (default) or `"json"`.

## Quick Start

### 1. Install

```bash
uv sync
```

### 2. Configure a backend

**Local state file:**
```bash
export TF_STATE_PATH=/path/to/terraform.tfstate
```

**GCS backend:**
```bash
export TF_STATE_BACKEND=gcs
export TF_STATE_BUCKET=my-tf-state-bucket
export TF_STATE_PREFIX=env/prod
```

**S3 backend:**
```bash
export TF_STATE_BACKEND=s3
export TF_STATE_BUCKET=my-tf-state-bucket
export TF_STATE_KEY=env/prod/terraform.tfstate
```

**Terraform Cloud:**
```bash
export TF_STATE_BACKEND=tfc
export TF_CLOUD_TOKEN=your-api-token
export TF_CLOUD_ORG=your-org
export TF_CLOUD_WORKSPACE=your-workspace
```

### 3. Run

**stdio (for Claude Desktop, Cursor, etc.):**
```bash
uv run server.py
```

**Streamable HTTP (for remote/multi-client):**
```python
# In server.py, change the last line:
mcp.run(transport="streamable_http", port=8080)
```

### 4. Connect to your client

**Claude Desktop (`claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "terraform-state": {
      "command": "uv",
      "args": ["--directory", "/path/to/terraform-state-mcp", "run", "server.py"],
      "env": {
        "TF_STATE_PATH": "/path/to/terraform.tfstate"
      }
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add terraform-state -- uv --directory /path/to/terraform-state-mcp run server.py
```

## Development

```bash
# Install all dependencies including dev tools
uv sync

# Run linting
uv run ruff check .

# Run tests
uv run pytest
```

## Example Queries

Once connected, you can ask things like:

- "Give me a summary of the Terraform state"
- "List all GCS buckets in the state"
- "Show me the attributes of `google_compute_instance.web`"
- "Find all resources without the `env` tag"
- "What depends on `module.vpc.google_compute_network.main`?"
- "Compare this state against staging"
- "How many resources are in each module?"

## Architecture

```
┌─────────────┐     stdio/HTTP     ┌──────────────────┐
│  LLM Client │ ◄────────────────► │  MCP Server      │
│  (Claude)   │                    │  (FastMCP)        │
└─────────────┘                    │                   │
                                   │  ┌──────────────┐ │
                                   │  │ State Cache  │ │
                                   │  └──────┬───────┘ │
                                   │         │         │
                                   └─────────┼─────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              │              │              │
                         ┌────▼───┐    ┌─────▼────┐   ┌────▼────┐
                         │ Local  │    │ GCS/S3   │   │  TF     │
                         │ File   │    │ Bucket   │   │  Cloud  │
                         └────────┘    └──────────┘   └─────────┘
```

## Design Decisions

- **Read-only by default** — no apply/destroy tools. Safe to use in production.
- **Cached state** — parsed once, refreshed on demand or every 5 minutes. Large state files (~100MB) won't be re-parsed on every tool call.
- **Backend-agnostic** — swap backends via env vars without code changes.
- **Fuzzy matching** — `tf_get_resource` suggests similar addresses if you mistype.

## License

MIT
