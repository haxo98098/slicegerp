# Publishing checklist

Steps that require the project owner's accounts (cannot be automated from a
collaborator machine). Everything else — building, validating, tagging,
GitHub release — is automated.

## 1. PyPI (one-time setup, ~5 minutes)

1. Create/log into your account at https://pypi.org
2. Since the `slicegrep` project doesn't exist yet, use a **pending
   publisher**: https://pypi.org/manage/account/publishing/ → "Add a new
   pending publisher" with:
   - PyPI project name: `slicegrep`
   - Owner: `haxo98098`
   - Repository: `slicegerp`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. In the GitHub repo: Settings → Environments → New environment → name it
   `pypi` (no other config needed).
4. Push any `v*` tag (the v0.2.0 tag already exists — re-releasing means
   bumping the version and tagging again). The release workflow builds,
   validates, and publishes without any token.

After the first publish, `pip install slicegrep` works and monthly download
stats start counting (a Claude-for-OSS eligibility metric).

## 2. MCP registry (official)

The official registry (https://registry.modelcontextprotocol.io) requires
the repo owner to authenticate. After PyPI publish:

1. Install the publisher CLI: see
   https://github.com/modelcontextprotocol/registry/blob/main/docs/guides/publishing/publish-server.md
2. `mcp-publisher login github` (as haxo98098 — namespace `io.github.haxo98098`)
3. From the repo root (contains `server.json`): `mcp-publisher publish`

## 3. Community MCP directories (free listings, form submissions)

- https://mcpservers.org — "Submit" form
- https://mcp.so — "Submit" form
- https://glama.ai/mcp/servers — indexes GitHub automatically; check listing
- awesome-mcp-servers lists (e.g. github.com/punkpeye/awesome-mcp-servers)
  accept PRs adding one line

## 4. Claude for Open Source application

https://claude.com/contact-sales/claude-for-oss — apply under the exception
clause ("apply anyway and tell us about it") citing: benchmark-driven
development (three published result sets, a found-and-fixed ranking bug),
MCP server for the agent ecosystem, and PyPI download trajectory.
