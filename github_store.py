"""
github_store
============
Optional persistence, via the GitHub Contents API, for two things that
otherwise wouldn't survive Streamlit Community Cloud redeploys/restarts
or be shared across every user's own session:

  1. The shared base template (A).
  2. The usage log (one row per file actually downloaded) behind the
     "使用統計" tab's counters.

This is the only way to get free, durable shared storage without an
Azure subscription - both just live as files in the same GitHub repo.

Configure via Streamlit secrets (.streamlit/secrets.toml, or the
"Secrets" panel in Streamlit Community Cloud's app settings):

    [github]
    token = "ghp_..."          # a fine-grained PAT with Contents:
                                # read/write on this ONE repo only
    repo = "your-org/your-repo"
    path = "templates/current_template.dxf"
    usage_log_path = "usage_log.csv"   # optional, defaults shown here
    branch = "main"

If this section is absent, the app just falls back to local (ephemeral)
storage for the template, and to an in-session-only counter for usage
stats - both still work, but "persist across redeploys / shared across
everyone" won't hold. See README.md for the usage-log caveats (mainly:
no locking, so two downloads at the exact same instant can in theory
race and one write gets lost - acceptable for how lightly this internal
tool is used, but worth knowing about).
"""
from __future__ import annotations

import base64

import requests
import streamlit as st

API_ROOT = "https://api.github.com"


def _cfg():
    try:
        gh = st.secrets["github"]
        return gh["token"], gh["repo"], gh.get("path", "templates/current_template.dxf"), gh.get("branch", "main")
    except Exception:
        return None, None, None, None


def _usage_cfg():
    try:
        gh = st.secrets["github"]
        return gh["token"], gh["repo"], gh.get("usage_log_path", "usage_log.csv"), gh.get("branch", "main")
    except Exception:
        return None, None, None, None


def is_configured() -> bool:
    token, repo, path, branch = _cfg()
    return bool(token and repo)


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _download_file(token, repo, path, branch) -> tuple[bytes | None, str | None]:
    """Returns (content, sha). content is None if the file doesn't exist
    yet (sha is then also None - there's nothing to overwrite)."""
    url = f"{API_ROOT}/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=_headers(token), params={"ref": branch}, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    body = r.json()
    return base64.b64decode(body["content"]), body["sha"]


def _upload_file(token, repo, path, branch, data: bytes, message: str, sha: str | None) -> None:
    url = f"{API_ROOT}/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(data).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_headers(token), json=payload, timeout=30)
    r.raise_for_status()


def download_template() -> bytes | None:
    token, repo, path, branch = _cfg()
    if not token:
        return None
    content, _sha = _download_file(token, repo, path, branch)
    return content


def upload_template(data: bytes, original_filename: str) -> None:
    token, repo, path, branch = _cfg()
    if not token:
        raise RuntimeError("GitHub sync not configured")
    _content, sha = _download_file(token, repo, path, branch)
    _upload_file(token, repo, path, branch, data, f"Update base template ({original_filename})", sha)


# ---------------------------------------------------------------------
# Usage log: one CSV row per file actually downloaded (timestamp,
# event, filename). Append-only, read back in full to compute the
# "使用統計" tab's monthly/yearly counters and for "另存數據" export.
# ---------------------------------------------------------------------

USAGE_LOG_HEADER = b"timestamp,event,filename\n"


def is_usage_log_configured() -> bool:
    token, repo, _path, _branch = _usage_cfg()
    return bool(token and repo)


def download_usage_log() -> bytes | None:
    token, repo, path, branch = _usage_cfg()
    if not token:
        return None
    content, _sha = _download_file(token, repo, path, branch)
    return content


def append_usage_event(timestamp_iso: str, event: str, filename: str, max_retries: int = 3) -> None:
    """Append one row to the shared usage log. Retries a couple of
    times on a 409 (someone else's write landed in between - re-read
    the new sha and try again) - this is a best-effort log for
    internal usage stats, not a transactional ledger, so a lost row
    under truly simultaneous clicks is an accepted, rare edge case
    rather than something worth building real locking for."""
    token, repo, path, branch = _usage_cfg()
    if not token:
        raise RuntimeError("GitHub sync not configured for the usage log")
    row = f'{timestamp_iso},{event},"{filename}"\n'.encode("utf-8")
    last_error = None
    for _attempt in range(max_retries):
        content, sha = _download_file(token, repo, path, branch)
        new_content = (content if content else USAGE_LOG_HEADER) + row
        try:
            _upload_file(token, repo, path, branch, new_content, f"Log usage: {event}", sha)
            return
        except requests.HTTPError as e:
            last_error = e
            if e.response is not None and e.response.status_code == 409:
                continue  # someone else wrote in between - retry with a fresh sha
            raise
    if last_error:
        raise last_error
