# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Terraform State Inspector MCP Server — a read-only MCP server that gives LLMs structured access to Terraform state files. Supports local state files and remote backends (GCS, S3, Terraform Cloud).

## Setup & Running

```bash
# Install all dependencies (including dev tools)
uv sync

# Run the server (stdio mode, default)
uv run server.py

# Run with a specific backend
TF_STATE_PATH=/path/to/terraform.tfstate uv run server.py
```

## Development Commands

```bash
uv run ruff check .    # Lint
uv run pytest          # Run tests
```

## Architecture

Single-file server (`server.py`) built on FastMCP (`mcp.server.fastmcp`). Key components:

- **StateLoader** — backend-agnostic state fetcher with 5-minute TTL cache. Dispatches to local file, gsutil (GCS), aws CLI (S3), or httpx (Terraform Cloud API).
- **Pydantic input models** — one per tool, all with `extra="forbid"` and `ConfigDict(str_strip_whitespace=True)`. Each includes a `response_format` field (markdown/json).
- **Tools** — 8 MCP tools all prefixed `tf_`. All are read-only except `tf_refresh_cache`. Tools receive a Pydantic model and `ctx` (for accessing the lifespan-scoped `StateLoader`).
- **Lifespan** — `app_lifespan()` creates a single `StateLoader` instance shared across all tool calls via `ctx.request_context.lifespan_state["loader"]`.

## Key Conventions

- All tool functions are async and access state via `ctx.request_context.lifespan_state["loader"]`.
- Resources are extracted into a flat list of dicts by `_extract_resources()`, which handles module prefixes and index keys.
- Every tool supports dual output: markdown (human-readable) and JSON (structured).
- Dependencies: `mcp[cli]`, `pydantic`, `httpx`. Python >= 3.10.
