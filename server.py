"""pages-mcp — host static sites/artifacts in-cluster, with an MCP upload tool.

One FastMCP streamable-HTTP app on :8080, two planes dispatched by Host header:

  * Control plane (bearer-gated):  https://pages-mcp.internal.white.fm/mcp
      MCP tools: deploy_site, list_sites, get_site, delete_site.
  * Serving plane (open on tailnet): https://<site>.pages.internal.white.fm/...
      Each site gets its own subdomain under the SERVE_HOST zone; files come from
      SITES_DIR/<site>/. A <base href="/"> is injected into served HTML (when none
      is present) so relative-path multi-file sites resolve from the site root.
      The apex (https://pages.internal.white.fm/) serves a directory index.

Auth (Claude -> control plane): static bearer MCP_TOKEN. /healthz is open.
Sites persist on a volume at SITES_DIR (default /data/sites).
"""

import base64
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
SITES_DIR = Path(os.environ.get("SITES_DIR", "/data/sites"))
# Serve zone: each site is hosted at https://<name>.<SERVE_HOST>/. The apex
# (Host == SERVE_HOST) serves the directory index.
SERVE_HOST = os.environ.get("SERVE_HOST", "pages.internal.white.fm").lower()
MCP_HOST = os.environ.get("MCP_HOST", "pages-mcp.internal.white.fm").lower()
PUBLIC_SCHEME = os.environ.get("PUBLIC_SCHEME", "https")
# Per-site total upload cap (bytes); protects the backing volume.
MAX_TOTAL_BYTES = int(os.environ.get("MAX_TOTAL_BYTES", str(64 * 1024 * 1024)))
GIT_REMOTE = os.environ.get("GIT_REMOTE", "").strip()
GIT_SSH_KEY = os.environ.get("GIT_SSH_KEY", "").strip()
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "pages-mcp")
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "pages-mcp@white.fm")

# DNS-label-ish: 1-63 chars, lowercase alnum + '-', not leading/trailing '-'.
SITE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RESERVED_NAMES = {"mcp", "healthz", "favicon.ico", "robots.txt"}

for _ext, _type in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".svg", "image/svg+xml"),
    (".json", "application/json"),
    (".wasm", "application/wasm"),
    (".woff2", "font/woff2"),
    (".webmanifest", "application/manifest+json"),
):
    mimetypes.add_type(_type, _ext)

mcp = FastMCP(
    "pages",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# --- helpers --------------------------------------------------------------

def _site_url(name: str) -> str:
    return f"{PUBLIC_SCHEME}://{name}.{SERVE_HOST}/"


def _req_host(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].lower()


def _is_serve_host(host: str) -> bool:
    """True for the content plane: the apex zone or any <label>.<SERVE_HOST>."""
    return host == SERVE_HOST or host.endswith("." + SERVE_HOST)


def _site_from_host(host: str) -> str | None:
    """Site name from a serve Host, or None for the apex / non-content hosts.

    <name>.<SERVE_HOST> -> "<name>" (single label, must pass SITE_NAME_RE and not
    be reserved). The apex (Host == SERVE_HOST) and anything else -> None.
    """
    suffix = "." + SERVE_HOST
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if "." in label or not SITE_NAME_RE.match(label) or label in RESERVED_NAMES:
        return None
    return label


def _site_dir(name: str) -> Path:
    if not isinstance(name, str) or not SITE_NAME_RE.match(name) or name in RESERVED_NAMES:
        raise ValueError(
            f"invalid site name {name!r}: use 1-63 lowercase letters/digits/'-' "
            "(not starting or ending with '-'), and not a reserved name"
        )
    return SITES_DIR / name


def _safe_member(root: Path, rel: str) -> Path:
    """Resolve rel within root, rejecting traversal/absolute/empty paths."""
    if not isinstance(rel, str) or not rel.strip("/") or "\x00" in rel:
        raise ValueError(f"invalid file path {rel!r}")
    rel = rel.lstrip("/")
    if rel.endswith("/"):
        raise ValueError(f"invalid file path {rel!r}")
    root_r = root.resolve()
    target = (root / rel).resolve()
    if target != root_r and root_r not in target.parents:
        raise ValueError(f"path escapes site root: {rel!r}")
    return target


def _site_stats(name: str) -> dict:
    d = _site_dir(name)
    files = total = 0
    latest = 0.0
    for p in d.rglob("*"):
        if p.is_file():
            st = p.stat()
            files += 1
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return {
        "name": name,
        "url": _site_url(name),
        "files": files,
        "bytes": total,
        "modified": datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else None,
    }


# --- git mirror (best-effort backup to GIT_REMOTE; never breaks the tools) ---

log = logging.getLogger("pages.git")
_git_lock = threading.Lock()
_git_ready = False
_git_ssh_command: "str | None" = None


def _git(*args, check=True):
    env = dict(os.environ)
    env["HOME"] = "/tmp"
    if _git_ssh_command:
        env["GIT_SSH_COMMAND"] = _git_ssh_command
    cmd = ["git", "-C", str(SITES_DIR), "-c", "safe.directory=*", *args]
    return subprocess.run(cmd, env=env, check=check, capture_output=True, text=True, timeout=120)


def _has_origin_main() -> bool:
    return _git("rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode == 0


def _count_sites() -> int:
    if not SITES_DIR.exists():
        return 0
    return sum(1 for c in SITES_DIR.iterdir() if c.is_dir() and SITE_NAME_RE.match(c.name))


def _git_sync(message: str) -> None:
    """Commit the current SITES_DIR state and push to GIT_REMOTE. Never raises."""
    if not _git_ready:
        return
    with _git_lock:
        try:
            _git("add", "-A")
            if not _git("status", "--porcelain").stdout.strip():
                return
            _git("commit", "-q", "-m", message)
            _git("push", "-q", "origin", "HEAD:main")
            log.info("git: %s", message)
        except Exception as exc:
            log.warning("git sync failed (%s): %s", message, exc)


def git_setup() -> None:
    """Bind SITES_DIR to GIT_REMOTE: restore from git if the volume is empty,
    else adopt history (volume is authoritative). Best-effort; never raises."""
    global _git_ready, _git_ssh_command
    if not GIT_REMOTE:
        log.info("GIT_REMOTE unset; git mirror disabled")
        return
    try:
        SITES_DIR.mkdir(parents=True, exist_ok=True)
        if GIT_SSH_KEY and Path(GIT_SSH_KEY).is_file():
            keydst = "/tmp/pages_git_key"
            shutil.copyfile(GIT_SSH_KEY, keydst)
            os.chmod(keydst, 0o600)
            _git_ssh_command = (
                f"ssh -i {keydst} -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/known_hosts"
            )
        if not (SITES_DIR / ".git").is_dir():
            _git("init", "-q")
            _git("remote", "add", "origin", GIT_REMOTE, check=False)
        _git("remote", "set-url", "origin", GIT_REMOTE, check=False)
        _git("config", "user.name", GIT_AUTHOR_NAME)
        _git("config", "user.email", GIT_AUTHOR_EMAIL)
        _git("fetch", "--quiet", "origin", "main", check=False)
        if _has_origin_main():
            if _count_sites() == 0:
                _git("reset", "--hard", "origin/main")   # restore: empty volume
                log.info("restored sites from %s", GIT_REMOTE)
            else:
                _git("reset", "--mixed", "origin/main")   # adopt; volume authoritative
            _git("checkout", "--", "README.md", check=False)  # keep repo docs in tree
        _git_ready = True
        log.info("git mirror ready (%s)", GIT_REMOTE)
        _git_sync("sync on startup")
    except Exception as exc:
        _git_ready = False
        log.warning("git setup failed; mirror disabled this run: %s", exc)


# --- MCP tools ------------------------------------------------------------

@mcp.tool()
def deploy_site(name: str, files: list[dict], replace: bool = True) -> dict:
    """Deploy a static site/artifact, served at https://<name>.pages.internal.white.fm/.

    name: lowercase DNS-label slug (a-z, 0-9, '-'), 1-63 chars. Becomes the site's
        subdomain, so it must also be a valid DNS label.
    files: list of {"path": "index.html", "content": "<...>", "encoding": "text"}.
        - path: relative path within the site (e.g. "index.html", "assets/app.js").
          Subdirectories are created automatically; "../" and absolute paths are rejected.
        - content: the file body.
        - encoding: "text" (default, utf-8) or "base64" for binary assets (images, fonts).
    replace: True (default) replaces the site with exactly these files; False merges
        the given files into an existing site (leaving other files in place).

    Put an index.html at the site root or the URL will 404. Returns {url, files, bytes}.
    """
    d = _site_dir(name)
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty list of {path, content[, encoding]}")

    # Validate + decode everything before touching disk.
    staged: list[tuple[str, bytes]] = []
    total = 0
    for i, f in enumerate(files):
        if not isinstance(f, dict) or "path" not in f or "content" not in f:
            raise ValueError(f"files[{i}] must be an object with 'path' and 'content'")
        rel = f["path"]
        enc = (f.get("encoding") or "text").lower()
        if enc == "base64":
            try:
                data = base64.b64decode(f["content"], validate=True)
            except Exception as exc:
                raise ValueError(f"files[{i}] ({rel!r}): invalid base64: {exc}")
        elif enc == "text":
            if not isinstance(f["content"], str):
                raise ValueError(f"files[{i}] ({rel!r}): text content must be a string")
            data = f["content"].encode("utf-8")
        else:
            raise ValueError(f"files[{i}] ({rel!r}): encoding must be 'text' or 'base64'")
        _safe_member(SITES_DIR / name, rel)  # path-safety check (rejects traversal)
        total += len(data)
        staged.append((rel, data))

    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"site too large: {total} bytes exceeds limit of {MAX_TOTAL_BYTES}")

    SITES_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=SITES_DIR, prefix=f".{name}.tmp."))
    try:
        for rel, data in staged:
            target = _safe_member(staging, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if replace:
            if d.exists():
                shutil.rmtree(d)
            staging.replace(d)
            staging = None  # consumed
        else:
            d.mkdir(parents=True, exist_ok=True)
            for rel, _ in staged:
                src = _safe_member(staging, rel)
                dst = _safe_member(d, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    stats = _site_stats(name)
    if not (d / "index.html").is_file():
        stats["warning"] = "no index.html at the site root; the base URL will 404 until one exists"
    _git_sync(f"deploy {name}")
    return stats


@mcp.tool()
def list_sites() -> list[dict]:
    """List all hosted sites with their URL, file count, total size, and last-modified time."""
    if not SITES_DIR.exists():
        return []
    return [
        _site_stats(c.name)
        for c in sorted(SITES_DIR.iterdir())
        if c.is_dir() and SITE_NAME_RE.match(c.name)
    ]


@mcp.tool()
def get_site(name: str) -> dict:
    """Get a site's URL, stats, and the list of file paths it contains."""
    d = _site_dir(name)
    if not d.is_dir():
        raise ValueError(f"site {name!r} does not exist")
    stats = _site_stats(name)
    stats["paths"] = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    return stats


@mcp.tool()
def delete_site(name: str) -> dict:
    """Delete a hosted site and all of its files. Returns {name, deleted}."""
    d = _site_dir(name)
    existed = d.is_dir()
    if existed:
        shutil.rmtree(d)
        _git_sync(f"delete {name}")
    return {"name": name, "deleted": existed}


# --- static serving plane -------------------------------------------------

_BASE_HEAD_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)
_BASE_HTML_RE = re.compile(rb"<html[^>]*>", re.IGNORECASE)


def _inject_base(html: bytes, prefix: str) -> bytes:
    """Inject <base href="prefix"> so relative asset paths resolve under the site path."""
    if b"<base" in html[:4096].lower():
        return html
    tag = f'<base href="{prefix}">'.encode("utf-8")
    for pat in (_BASE_HEAD_RE, _BASE_HTML_RE):
        m = pat.search(html)
        if m:
            return html[: m.end()] + tag + html[m.end():]
    return tag + html


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pages</title>
<style>
  :root{
    --bg:#0a0c10; --panel:#141821; --panel2:#1a1f2b;
    --text:#e8edf4; --muted:#8b95a7; --ring:#283142;
    --accent:#6ea8fe; --accent2:#a78bfa; --ok:#3ddc84;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    min-height:100vh; color:var(--text);
    background:
      radial-gradient(1100px 520px at 50% -8%, #1b2438 0%, rgba(27,36,56,0) 60%),
      var(--bg);
    font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:62rem; margin:0 auto; padding:5.5rem 1.25rem 4rem}
  .brand{display:flex; align-items:center; gap:.7rem; font-size:1.7rem; font-weight:750; letter-spacing:-.03em}
  .glyph{width:1.5rem; height:1.5rem; border-radius:.45rem;
    background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 6px 18px rgba(110,168,254,.35)}
  .sub{color:var(--muted); margin-top:.5rem; font-size:.95rem}
  .count{color:var(--text); font-weight:600}
  .grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(15.5rem,1fr)); gap:1rem; margin-top:2.4rem}
  .card{display:flex; flex-direction:column; gap:.45rem; padding:1.15rem 1.25rem;
    background:linear-gradient(180deg,var(--panel2),var(--panel));
    border:1px solid var(--ring); border-radius:1rem; text-decoration:none; color:inherit;
    transition:transform .16s ease, border-color .16s ease, box-shadow .16s ease}
  .card:hover{transform:translateY(-3px); border-color:var(--accent); box-shadow:0 12px 34px rgba(0,0,0,.4)}
  .name{display:flex; align-items:center; gap:.55rem; font-weight:650; font-size:1.06rem; letter-spacing:-.01em}
  .dot{width:.5rem; height:.5rem; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px rgba(61,220,132,.16)}
  .host{color:var(--muted); font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; word-break:break-all}
  .empty{color:var(--muted); padding:2.5rem; border:1px dashed var(--ring); border-radius:1rem; text-align:center}
  code{background:var(--panel2); border:1px solid var(--ring); border-radius:.4rem; padding:.1rem .35rem; font-size:.85em}
  footer{margin-top:3.5rem; padding-top:1.25rem; border-top:1px solid var(--ring); color:var(--muted); font-size:.82rem;
    display:flex; justify-content:space-between; flex-wrap:wrap; gap:.6rem}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand"><span class="glyph"></span>pages</div>
      <div class="sub">self-hosted static sites &middot; <span class="count">__COUNT__</span> live</div>
    </header>
    <main class="grid">__GRID__</main>
    <footer><span>pages.internal.white.fm</span><span>white.fm</span></footer>
  </div>
</body>
</html>"""


def _listing() -> HTMLResponse:
    cards = []
    if SITES_DIR.exists():
        for c in sorted(SITES_DIR.iterdir()):
            if c.is_dir() and SITE_NAME_RE.match(c.name):
                url = _site_url(c.name)
                host = url.split("://", 1)[-1].rstrip("/")
                cards.append(
                    '<a class="card" href="' + url + '">'
                    '<span class="name"><span class="dot"></span>' + c.name + "</span>"
                    '<span class="host">' + host + "</span></a>"
                )
    grid = "".join(cards) or (
        '<p class="empty">No sites yet — deploy one with the <code>pages</code> MCP tool.</p>'
    )
    body = _INDEX_HTML.replace("__GRID__", grid).replace("__COUNT__", str(len(cards)))
    return HTMLResponse(body)


async def _serve(request: Request) -> Response:
    host = _req_host(request)
    raw = request.url.path
    site = _site_from_host(host)
    if site is None:
        # Apex zone root -> directory index. Anything else (other hosts, or a path
        # on the apex) is 404: sites live on subdomains, not under a path prefix.
        if host == SERVE_HOST and not raw.strip("/"):
            return _listing()
        return PlainTextResponse("not found", status_code=404)

    site_root = SITES_DIR / site
    if not site_root.is_dir():
        return PlainTextResponse("not found", status_code=404)

    rel = raw.strip("/")
    if not rel:
        target = site_root / "index.html"
    else:
        try:
            target = _safe_member(site_root, rel)
        except ValueError:
            return PlainTextResponse("bad request", status_code=400)
        if target.is_dir():
            if not raw.endswith("/"):
                return RedirectResponse(raw + "/", status_code=308)
            target = target / "index.html"

    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)

    data = target.read_bytes()
    ctype, _ = mimetypes.guess_type(target.name)
    if target.suffix.lower() in (".html", ".htm"):
        data = _inject_base(data, "/")
        ctype = "text/html; charset=utf-8"
    return Response(
        content=data,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class Gate(BaseHTTPMiddleware):
    """Host-based plane split + bearer auth for the control plane."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)
        host = _req_host(request)
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        if is_mcp:
            if _is_serve_host(host):  # never expose the control plane on a content host
                return PlainTextResponse("not found", status_code=404)
            if MCP_TOKEN and request.headers.get("authorization", "") != f"Bearer {MCP_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)
        if host == MCP_HOST:  # keep the control-plane host free of served content
            return PlainTextResponse("not found", status_code=404)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(Gate)
app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
app.router.routes.append(Route("/{path:path}", _serve, methods=["GET", "HEAD"]))


if __name__ == "__main__":
    import uvicorn

    SITES_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    git_setup()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
