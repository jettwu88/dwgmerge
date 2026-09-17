"""
github_store
============
Optional persistence for the shared base template (A): store it as a
file in a GitHub repo via the Contents API, so it survives Streamlit
Community Cloud redeploys/restarts and every user gets the same
baseline. This is the only way to get free, durable shared storage
without an Azure subscription.

Configure via Streamlit secrets (.streamlit/secrets.toml, or the
"Secrets" panel in Streamlit Community Cloud's app settings):

    [github]
    token = "ghp_..."          # a fine-grained PAT with Contents:
                                # read/write on this ONE repo only
    repo = "your-org/your-repo"
    path = "templates/current_template.dxf"
    branch = "main"

If this section is absent, the app just falls back to local (ephemeral)
storage - it still works, but "persist across redeploys" won't hold.
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


def is_configured() -> bool:
    token, repo, path, branch = _cfg()
    return bool(token and repo)


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def download_template() -> bytes | None:
    token, repo, path, branch = _cfg()
    if not token:
        return None
    url = f"{API_ROOT}/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=_headers(token), params={"ref": branch}, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    content = r.json()["content"]
    return base64.b64decode(content)


def upload_template(data: bytes, original_filename: str) -> None:
    token, repo, path, branch = _cfg()
    if not token:
        raise RuntimeError("GitHub sync not configured")
    url = f"{API_ROOT}/repos/{repo}/contents/{path}"

    # need the current file's sha to update it, if it already exists
    sha = None
    r = requests.get(url, headers=_headers(token), params={"ref": branch}, timeout=20)
    if r.status_code == 200:
        sha = r.json()["sha"]

    payload = {
        "message": f"Update base template ({original_filename})",
        "content": base64.b64encode(data).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=_headers(token), json=payload, timeout=30)
    r.raise_for_status()
