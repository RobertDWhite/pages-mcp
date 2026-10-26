# pages-mcp

A tiny static-site host with an MCP upload tool. Claude (or any MCP client) uploads
static sites/artifacts; they're served over HTTPS at an internal hostname.

- **Serve:** `https://pages.internal.white.fm/<site>/` (open on the tailnet)
- **Upload (MCP):** `https://pages-mcp.internal.white.fm/mcp` — `Authorization: Bearer <MCP_TOKEN>`
- **Storage:** files persist under `SITES_DIR` (default `/data/sites`), one dir per site.

A `<base href="/<site>/">` is injected into served HTML (when absent) so relative-path
multi-file sites work under the path prefix, not just self-contained single files.

## MCP tools

| Tool | Purpose |
|------|---------|
| `deploy_site(name, files, replace=True)` | Upload a site. `files` = `[{path, content, encoding}]` (`encoding`: `text`\|`base64`). `replace` replaces vs. merges. Returns `{url, files, bytes}`. |
| `list_sites()` | All sites with URL, file count, size, last-modified. |
| `get_site(name)` | One site's stats + the list of file paths. |
| `delete_site(name)` | Remove a site and all its files. |

## Writable data files

A site can let its own pages save JSON back to the server, e.g. a gallery that
keeps user-curated items. Opt in by deploying a `pages-writable.json` at the site
root:

```json
{"files": {"collages/saved.json": {"max_bytes": 262144}}}
```

Then the page can `PUT` that path with `Content-Type: application/json`:

- Only on `WRITE_HOSTS` (default: the canonical `SERVE_HOST`, i.e. the internal
  zone), so a site also exposed on a public zone stays read-only there.
- Only listed `.json` paths; the body must parse as JSON and fit `max_bytes`
  (capped by `MAX_WRITABLE_BYTES`). Cross-origin `Origin` headers are refused.
- `GET` returns an `ETag`; send it back as `If-Match` and a write that raced
  another one gets `412` with the current `ETag`, so the page can re-read, merge
  and retry instead of silently dropping the other write.
- Each write is committed to the git mirror (`update <site>/<path>`).
- The live file outlives redeploys: `deploy_site` seeds a writable file the first
  time but never overwrites or deletes it afterwards (returned as `preserved`).

`/healthz` is open (used by k8s probes). Site names are lowercase DNS labels
(`a-z`, `0-9`, `-`), 1–63 chars.

## Env

| Var | Default | Notes |
|-----|---------|-------|
| `MCP_TOKEN` | _(empty)_ | Bearer token for the control plane. Empty = auth disabled (don't run that way). |
| `PORT` | `8080` | |
| `SITES_DIR` | `/data/sites` | Persistent storage root. |
| `SERVE_HOST` | `pages.internal.white.fm` | Content host (Host header → serving plane). |
| `MCP_HOST` | `pages-mcp.internal.white.fm` | Control host (Host header → `/mcp`). |
| `PUBLIC_BASE_URL` | `https://$SERVE_HOST` | Base used in returned URLs. |
| `MAX_TOTAL_BYTES` | `67108864` (64 MiB) | Per-site upload cap. |
| `WRITE_HOSTS` | first `SERVE_HOST` | Serve zones that accept writable-file `PUT`s (comma-separated). |
| `MAX_WRITABLE_BYTES` | `1048576` (1 MiB) | Upper bound for any writable file. |

## Build

GitHub Actions (`.github/workflows/build.yml`) builds `ghcr.io/robertdwhite/pages-mcp`
(linux/amd64) on push to `main` / `v*` tags. Pin the resulting digest in the cluster
repo at `apps/ai/pages/kustomization.yaml`.

## Register with Claude Code

```sh
# Token from the k8s secret:
sops -d apps/ai/pages/11-secret.sops.yaml | grep MCP_TOKEN

claude mcp add --transport http pages \
  https://pages-mcp.internal.white.fm/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```
