"""
Terraform State Inspector MCP Server

An MCP server that exposes read-only tools for querying, analyzing, and reporting
on Terraform state files. Supports local state files and remote backends (GCS, S3,
Terraform Cloud).

Usage:
    # Local state file
    TF_STATE_PATH=/path/to/terraform.tfstate python server.py

    # GCS backend
    TF_STATE_BACKEND=gcs TF_STATE_BUCKET=my-bucket TF_STATE_PREFIX=env/prod python server.py

    # S3 backend
    TF_STATE_BACKEND=s3 TF_STATE_BUCKET=my-bucket TF_STATE_KEY=env/prod/terraform.tfstate python server.py

    # Terraform Cloud
    TF_STATE_BACKEND=tfc TF_CLOUD_TOKEN=xxx TF_CLOUD_ORG=myorg TF_CLOUD_WORKSPACE=prod python server.py
"""

import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STATE_PATH = "terraform.tfstate"
CACHE_TTL_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# State loader – abstracts over local files & remote backends
# ---------------------------------------------------------------------------

class StateLoader:
    """Loads Terraform state from various backends."""

    def __init__(self):
        self.backend = os.getenv("TF_STATE_BACKEND", "local")
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0

    async def load(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Load and cache the Terraform state."""
        now = time.time()
        if (
            not force_refresh
            and self._cache is not None
            and (now - self._cache_time) < CACHE_TTL_SECONDS
        ):
            return self._cache

        raw = self._fetch_raw()
        self._cache = json.loads(raw)
        self._cache_time = time.time()
        return self._cache

    def _fetch_raw(self) -> str:
        if self.backend == "local":
            path = os.getenv("TF_STATE_PATH", DEFAULT_STATE_PATH)
            return Path(path).read_text()

        if self.backend == "gcs":
            bucket = os.environ["TF_STATE_BUCKET"]
            prefix = os.getenv("TF_STATE_PREFIX", "")
            obj_path = f"{prefix}/default.tfstate" if prefix else "default.tfstate"
            result = subprocess.run(
                ["gsutil", "cat", f"gs://{bucket}/{obj_path}"],
                capture_output=True, text=True, check=True,
            )
            return result.stdout

        if self.backend == "s3":
            bucket = os.environ["TF_STATE_BUCKET"]
            key = os.environ["TF_STATE_KEY"]
            result = subprocess.run(
                ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"],
                capture_output=True, text=True, check=True,
            )
            return result.stdout

        if self.backend == "tfc":
            return self._fetch_tfc()

        raise ValueError(f"Unsupported backend: {self.backend}")

    def _fetch_tfc(self) -> str:
        """Fetch state from Terraform Cloud API."""
        import httpx

        token = os.environ["TF_CLOUD_TOKEN"]
        org = os.environ["TF_CLOUD_ORG"]
        workspace = os.environ["TF_CLOUD_WORKSPACE"]
        base = "https://app.terraform.io/api/v2"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }

        # Get workspace ID
        resp = httpx.get(f"{base}/organizations/{org}/workspaces/{workspace}", headers=headers)
        resp.raise_for_status()
        ws_id = resp.json()["data"]["id"]

        # Get current state version
        resp = httpx.get(f"{base}/workspaces/{ws_id}/current-state-version", headers=headers)
        resp.raise_for_status()
        download_url = resp.json()["data"]["attributes"]["hosted-state-download-url"]

        # Download state
        resp = httpx.get(download_url, headers=headers)
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------------------
# State parsing helpers
# ---------------------------------------------------------------------------

def _extract_resources(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract a flat list of resource instances from state."""
    resources = []
    for res in state.get("resources", []):
        module_addr = res.get("module", "")
        res_type = res.get("type", "")
        res_name = res.get("name", "")
        provider = res.get("provider", "")
        mode = res.get("mode", "managed")

        for inst in res.get("instances", []):
            index_key = inst.get("index_key")
            base_addr = f"{module_addr + '.' if module_addr else ''}{res_type}.{res_name}"
            if index_key is not None:
                address = f'{base_addr}["{index_key}"]' if isinstance(index_key, str) else f"{base_addr}[{index_key}]"
            else:
                address = base_addr

            resources.append({
                "address": address,
                "type": res_type,
                "name": res_name,
                "module": module_addr or "(root)",
                "mode": mode,
                "provider": provider,
                "attributes": inst.get("attributes", {}),
                "sensitive_attributes": inst.get("sensitive_attributes", []),
                "dependencies": inst.get("dependencies", []),
            })
    return resources


def _match_filter(resource: Dict[str, Any], type_filter: Optional[str], module_filter: Optional[str]) -> bool:
    """Check if a resource matches the given filters."""
    if type_filter and type_filter.lower() not in resource["type"].lower():
        return False
    if module_filter and module_filter.lower() not in resource["module"].lower():
        return False
    return True


def _format_resource_summary(res: Dict[str, Any]) -> str:
    """Single-line summary of a resource for list views."""
    return f"- **{res['address']}** (`{res['type']}`) — provider: `{res['provider']}`"


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class ListResourcesInput(BaseModel):
    """Input for listing resources in state."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    type_filter: Optional[str] = Field(
        default=None,
        description="Filter resources by type substring (e.g., 'google_compute', 'aws_s3')",
    )
    module_filter: Optional[str] = Field(
        default=None,
        description="Filter resources by module path substring (e.g., 'module.networking')",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for readable text, 'json' for structured data",
    )


class GetResourceInput(BaseModel):
    """Input for getting a specific resource."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    address: str = Field(
        ...,
        description="Full Terraform resource address (e.g., 'google_compute_instance.web', 'module.vpc.aws_subnet.public[0]')",
        min_length=1,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SearchAttributesInput(BaseModel):
    """Input for searching resources by attribute values."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    attribute_path: str = Field(
        ...,
        description="Dot-separated attribute path to search (e.g., 'network_interface.0.access_config', 'versioning.0.enabled', 'tags.env')",
        min_length=1,
    )
    value: Optional[str] = Field(
        default=None,
        description="Expected value to match (string comparison). If omitted, returns all resources where the attribute exists.",
    )
    type_filter: Optional[str] = Field(
        default=None,
        description="Narrow search to resources whose type contains this substring",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetOutputsInput(BaseModel):
    """Input for getting Terraform outputs."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name_filter: Optional[str] = Field(
        default=None,
        description="Filter outputs by name substring",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DependencyGraphInput(BaseModel):
    """Input for getting a resource's dependency graph."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    address: str = Field(
        ...,
        description="Resource address to get the dependency tree for",
        min_length=1,
    )
    depth: int = Field(
        default=3,
        description="Maximum depth of dependency traversal",
        ge=1,
        le=10,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DiffStateInput(BaseModel):
    """Input for comparing two state snapshots."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    other_state_path: str = Field(
        ...,
        description="Path or URI to the second state file to compare against the current state",
        min_length=1,
    )
    type_filter: Optional[str] = Field(
        default=None,
        description="Narrow diff to resources whose type contains this substring",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class RefreshInput(BaseModel):
    """Input for refreshing the cached state."""
    model_config = ConfigDict(extra="forbid")


class SummaryInput(BaseModel):
    """Input for getting a high-level state summary."""
    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Lifespan – initialise the state loader once
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan():
    loader = StateLoader()
    yield {"loader": loader}


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("terraform_state_mcp", lifespan=app_lifespan)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="tf_list_resources",
    annotations={
        "title": "List Terraform Resources",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_list_resources(params: ListResourcesInput, ctx=None) -> str:
    """List all resources currently tracked in Terraform state.

    Provides a filterable inventory of every resource instance, including its
    address, type, module, and provider. Use type_filter and module_filter to
    narrow results.

    Returns:
        str: Resource listing in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    resources = _extract_resources(state)
    filtered = [r for r in resources if _match_filter(r, params.type_filter, params.module_filter)]

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"total": len(filtered), "resources": [
            {"address": r["address"], "type": r["type"], "module": r["module"], "provider": r["provider"]}
            for r in filtered
        ]}, indent=2)

    if not filtered:
        return "No resources found matching the given filters."

    lines = [f"## Terraform Resources ({len(filtered)} found)\n"]
    # Group by module
    by_module: Dict[str, List[Dict]] = {}
    for r in filtered:
        by_module.setdefault(r["module"], []).append(r)
    for mod, res_list in sorted(by_module.items()):
        lines.append(f"\n### Module: `{mod}`\n")
        for r in sorted(res_list, key=lambda x: x["address"]):
            lines.append(_format_resource_summary(r))
    return "\n".join(lines)


@mcp.tool(
    name="tf_get_resource",
    annotations={
        "title": "Get Resource Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_get_resource(params: GetResourceInput, ctx=None) -> str:
    """Get the full attributes of a specific resource by its Terraform address.

    Returns all attribute values, dependencies, and sensitive attribute markers
    for the specified resource instance.

    Returns:
        str: Full resource details in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    resources = _extract_resources(state)

    match = next((r for r in resources if r["address"] == params.address), None)
    if not match:
        # Fuzzy search
        candidates = [r for r in resources if params.address.lower() in r["address"].lower()]
        if candidates:
            suggestions = "\n".join(f"  - `{c['address']}`" for c in candidates[:10])
            return f"Resource `{params.address}` not found. Did you mean:\n{suggestions}"
        return f"Resource `{params.address}` not found. Use tf_list_resources to see all resources."

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(match, indent=2, default=str)

    lines = [
        f"## Resource: `{match['address']}`\n",
        f"- **Type**: `{match['type']}`",
        f"- **Module**: `{match['module']}`",
        f"- **Provider**: `{match['provider']}`",
        f"- **Mode**: `{match['mode']}`",
        f"- **Dependencies**: {', '.join(f'`{d}`' for d in match['dependencies']) or 'none'}",
        "",
        "### Attributes\n",
        "```json",
        json.dumps(match["attributes"], indent=2, default=str),
        "```",
    ]
    if match["sensitive_attributes"]:
        lines.append(f"\n### Sensitive Attributes\n")
        for sa in match["sensitive_attributes"]:
            lines.append(f"- `{sa}`")
    return "\n".join(lines)


@mcp.tool(
    name="tf_search_attributes",
    annotations={
        "title": "Search Resources by Attribute",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_search_attributes(params: SearchAttributesInput, ctx=None) -> str:
    """Search for resources whose attributes match a given path and optional value.

    Useful for compliance checks like 'find all buckets without encryption',
    'find instances with public IPs', or 'find resources tagged env=prod'.

    The attribute_path supports dot-notation for nested attributes (e.g.,
    'versioning.0.enabled', 'tags.env').

    Returns:
        str: Matching resources with their attribute values.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    resources = _extract_resources(state)

    def _resolve_path(attrs: Dict, path: str) -> Any:
        """Walk a dot-separated path into a nested dict/list."""
        parts = path.split(".")
        current: Any = attrs
        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    matches = []
    for r in resources:
        if params.type_filter and params.type_filter.lower() not in r["type"].lower():
            continue
        resolved = _resolve_path(r["attributes"], params.attribute_path)
        if resolved is None:
            continue
        if params.value is not None and str(resolved).lower() != params.value.lower():
            continue
        matches.append({"address": r["address"], "type": r["type"], "attribute_value": resolved})

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"total": len(matches), "matches": matches}, indent=2, default=str)

    if not matches:
        return f"No resources found with attribute `{params.attribute_path}`" + (
            f" = `{params.value}`" if params.value else ""
        ) + "."

    lines = [f"## Attribute Search: `{params.attribute_path}`" + (f" = `{params.value}`" if params.value else "") + f"  ({len(matches)} matches)\n"]
    for m in matches:
        lines.append(f"- **{m['address']}** (`{m['type']}`) → `{m['attribute_value']}`")
    return "\n".join(lines)


@mcp.tool(
    name="tf_get_outputs",
    annotations={
        "title": "Get Terraform Outputs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_get_outputs(params: GetOutputsInput, ctx=None) -> str:
    """List all Terraform outputs and their current values.

    Returns:
        str: Outputs listing in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    outputs = state.get("outputs", {})

    if params.name_filter:
        outputs = {k: v for k, v in outputs.items() if params.name_filter.lower() in k.lower()}

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(outputs, indent=2, default=str)

    if not outputs:
        return "No outputs found" + (f" matching `{params.name_filter}`." if params.name_filter else ".")

    lines = [f"## Terraform Outputs ({len(outputs)})\n"]
    for name, info in sorted(outputs.items()):
        val = info.get("value", "")
        sensitive = info.get("sensitive", False)
        out_type = info.get("type", "unknown")
        display_val = "(sensitive)" if sensitive else json.dumps(val, default=str)
        lines.append(f"- **{name}** ({out_type}): `{display_val}`")
    return "\n".join(lines)


@mcp.tool(
    name="tf_dependency_graph",
    annotations={
        "title": "Resource Dependency Graph",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_dependency_graph(params: DependencyGraphInput, ctx=None) -> str:
    """Get the dependency tree for a specific resource.

    Walks the dependency chain up to the specified depth, showing what each
    resource depends on.

    Returns:
        str: Dependency tree in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    resources = _extract_resources(state)

    addr_map = {r["address"]: r for r in resources}

    if params.address not in addr_map:
        return f"Resource `{params.address}` not found. Use tf_list_resources to see available resources."

    def _build_tree(addr: str, current_depth: int, visited: set) -> Dict:
        if addr in visited or current_depth > params.depth:
            return {"address": addr, "truncated": True}
        visited.add(addr)
        node = {"address": addr, "type": addr_map[addr]["type"] if addr in addr_map else "unknown"}
        deps = addr_map.get(addr, {}).get("dependencies", [])
        if deps and current_depth < params.depth:
            node["depends_on"] = [_build_tree(d, current_depth + 1, visited) for d in deps]
        elif deps:
            node["depends_on_count"] = len(deps)
        return node

    tree = _build_tree(params.address, 0, set())

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(tree, indent=2)

    def _render_tree(node: Dict, indent: int = 0) -> List[str]:
        prefix = "  " * indent + ("└─ " if indent > 0 else "")
        lines = [f"{prefix}**{node['address']}** (`{node.get('type', '?')}`)"]
        if node.get("truncated"):
            lines[-1] += " *(circular/truncated)*"
        for child in node.get("depends_on", []):
            lines.extend(_render_tree(child, indent + 1))
        if "depends_on_count" in node:
            lines.append(f"{'  ' * (indent + 1)}└─ ... {node['depends_on_count']} more dependencies")
        return lines

    header = f"## Dependency Graph: `{params.address}`\n"
    return header + "\n".join(_render_tree(tree))


@mcp.tool(
    name="tf_diff_state",
    annotations={
        "title": "Diff Two State Snapshots",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_diff_state(params: DiffStateInput, ctx=None) -> str:
    """Compare the current state against another state file.

    Identifies resources that were added, removed, or changed between the two
    snapshots. Useful for reviewing what changed after an apply or comparing
    environments (staging vs prod).

    Returns:
        str: Diff report in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    current_state = await loader.load()

    other_raw = Path(params.other_state_path).read_text()
    other_state = json.loads(other_raw)

    current_resources = {r["address"]: r for r in _extract_resources(current_state)}
    other_resources = {r["address"]: r for r in _extract_resources(other_state)}

    all_addrs = set(current_resources.keys()) | set(other_resources.keys())
    if params.type_filter:
        all_addrs = {
            a for a in all_addrs
            if params.type_filter.lower() in current_resources.get(a, other_resources.get(a, {})).get("type", "").lower()
        }

    added = sorted(a for a in all_addrs if a in current_resources and a not in other_resources)
    removed = sorted(a for a in all_addrs if a not in current_resources and a in other_resources)
    changed = []
    for a in sorted(all_addrs):
        if a in current_resources and a in other_resources:
            if current_resources[a]["attributes"] != other_resources[a]["attributes"]:
                changed.append(a)

    diff = {"added": added, "removed": removed, "changed": changed, "unchanged": len(all_addrs) - len(added) - len(removed) - len(changed)}

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(diff, indent=2)

    lines = [f"## State Diff\n"]
    lines.append(f"Comparing current state against `{params.other_state_path}`\n")
    lines.append(f"- **Added**: {len(added)}")
    lines.append(f"- **Removed**: {len(removed)}")
    lines.append(f"- **Changed**: {len(changed)}")
    lines.append(f"- **Unchanged**: {diff['unchanged']}\n")

    if added:
        lines.append("### Added Resources\n")
        for a in added:
            lines.append(f"- `{a}` (`{current_resources[a]['type']}`)")
    if removed:
        lines.append("\n### Removed Resources\n")
        for a in removed:
            lines.append(f"- `{a}` (`{other_resources[a]['type']}`)")
    if changed:
        lines.append("\n### Changed Resources\n")
        for a in changed:
            lines.append(f"- `{a}` (`{current_resources[a]['type']}`)")
    return "\n".join(lines)


@mcp.tool(
    name="tf_summary",
    annotations={
        "title": "State Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tf_summary(params: SummaryInput, ctx=None) -> str:
    """Get a high-level summary of the current Terraform state.

    Returns resource counts by type, module breakdown, provider distribution,
    and state metadata (serial, version, Terraform version).

    Returns:
        str: Summary in the requested format.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load()
    resources = _extract_resources(state)

    by_type: Dict[str, int] = {}
    by_module: Dict[str, int] = {}
    by_provider: Dict[str, int] = {}
    for r in resources:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        by_module[r["module"]] = by_module.get(r["module"], 0) + 1
        by_provider[r["provider"]] = by_provider.get(r["provider"], 0) + 1

    summary = {
        "terraform_version": state.get("terraform_version", "unknown"),
        "serial": state.get("serial", 0),
        "lineage": state.get("lineage", "unknown"),
        "total_resources": len(resources),
        "total_outputs": len(state.get("outputs", {})),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "by_module": dict(sorted(by_module.items(), key=lambda x: -x[1])),
        "by_provider": dict(sorted(by_provider.items(), key=lambda x: -x[1])),
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(summary, indent=2)

    lines = [
        "## Terraform State Summary\n",
        f"- **Terraform Version**: `{summary['terraform_version']}`",
        f"- **State Serial**: `{summary['serial']}`",
        f"- **Lineage**: `{summary['lineage']}`",
        f"- **Total Resources**: {summary['total_resources']}",
        f"- **Total Outputs**: {summary['total_outputs']}",
        "",
        "### Resources by Type\n",
    ]
    for t, count in summary["by_type"].items():
        lines.append(f"- `{t}`: {count}")

    lines.append("\n### Resources by Module\n")
    for m, count in summary["by_module"].items():
        lines.append(f"- `{m}`: {count}")

    lines.append("\n### Resources by Provider\n")
    for p, count in summary["by_provider"].items():
        lines.append(f"- `{p}`: {count}")

    return "\n".join(lines)


@mcp.tool(
    name="tf_refresh_cache",
    annotations={
        "title": "Refresh State Cache",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def tf_refresh_cache(params: RefreshInput, ctx=None) -> str:
    """Force-refresh the cached Terraform state from the backend.

    Call this after running terraform apply to ensure the MCP server
    is working with the latest state.

    Returns:
        str: Confirmation message with state metadata.
    """
    loader: StateLoader = ctx.request_context.lifespan_state["loader"]
    state = await loader.load(force_refresh=True)
    resource_count = len(_extract_resources(state))
    return (
        f"State cache refreshed successfully.\n"
        f"- Serial: {state.get('serial', '?')}\n"
        f"- Resources: {resource_count}\n"
        f"- Terraform version: {state.get('terraform_version', '?')}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
