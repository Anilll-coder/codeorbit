"""The GitHub REST API, only as much of it as reviewing a pull request needs.

This is the second place in CodeOrbit that talks to the network, and the first
that can *write* anywhere. Both facts shape what is here:

  * Nothing in this module posts unless something above it explicitly asks. The
    read path and the write path are separate functions, and the write path is
    reached only from an opt-in flag.
  * The token never appears in a URL, a log line, an error message or a repr.
    Errors from here are raised with the API's message and the status, never
    with the request that produced them.

Everything returned by this module is written by whoever opened the pull
request, which is not necessarily someone you trust. It is data. See the
`untrusted` helpers in pr.py before any of it reaches a model.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

API = "https://api.github.com"
TIMEOUT = 30


class GitHubError(RuntimeError):
    pass


@dataclass
class Repo:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    state: str
    draft: bool
    author: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repo: str | None          # None when the fork was deleted
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    comments: list = field(default_factory=list)


# A remote can be spelled several ways and only some of them are GitHub.
_REMOTE = re.compile(
    r"^(?:https?://|git://|ssh://git@|git@)"
    r"(?:[^@/]+@)?"
    r"(?P<host>[^:/]+)"
    r"[:/](?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?/?$"
)


def repo_from_remote(root: Path, remote: str = "origin") -> Repo:
    """Work out which GitHub repository this checkout belongs to."""
    try:
        p = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", remote],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as e:
        raise GitHubError("git is not installed or not on PATH.") from e
    if p.returncode != 0:
        raise GitHubError(
            f"No git remote named {remote!r} in {root}. "
            "Pass --repo owner/name to say which repository the PR is in.")

    url = p.stdout.strip()
    m = _REMOTE.match(url)
    if not m:
        raise GitHubError(f"Could not read a repository out of the remote URL {url!r}.")
    if "github" not in m.group("host"):
        raise GitHubError(
            f"The {remote!r} remote points at {m.group('host')}, not GitHub. "
            "Only GitHub pull requests are supported.")
    return Repo(m.group("owner"), m.group("name"))


def parse_slug(slug: str) -> Repo:
    if slug.count("/") != 1 or not all(slug.split("/")):
        raise GitHubError(f"--repo must look like owner/name, got {slug!r}")
    owner, name = slug.split("/")
    return Repo(owner, name)


def token() -> str | None:
    """A token, or None. GITHUB_TOKEN first: it is what CI sets."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    # gh stores one too, and someone with gh authenticated should not have to
    # set a second variable. Read it through gh rather than parsing its config,
    # so gh stays responsible for its own storage format.
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=15)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return None


NO_TOKEN = (
    "No GitHub token found. Reviewing a pull request reads it from the API, "
    "which needs one.\n"
    "  Set GITHUB_TOKEN (or GH_TOKEN) to a token with 'repo' read access, or\n"
    "  run `gh auth login` if you use the GitHub CLI.\n"
    "A fine-grained token needs 'Pull requests: read'. Posting also needs write."
)


def _headers(tok: str) -> dict:
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "codeorbit",
    }


def _request(method: str, url: str, tok: str, **kw):
    try:
        r = requests.request(method, url, headers=_headers(tok),
                             timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        # Deliberately not including the request: it carries the token header.
        raise GitHubError(f"Could not reach GitHub: {e}") from None

    if r.status_code == 401:
        raise GitHubError("GitHub rejected the token (401). It may be expired "
                          "or missing the scopes this needs.")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise GitHubError("GitHub rate limit reached. Try again later.")
    if r.status_code == 403:
        raise GitHubError("GitHub refused (403). The token is probably missing "
                          "access to this repository.")
    if r.status_code == 404:
        raise GitHubError("Not found (404). Either it does not exist or the "
                          "token cannot see it.")
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("message", "")
        except ValueError:
            detail = r.text[:200]
        raise GitHubError(f"GitHub returned {r.status_code}. {detail}")
    return r


def get_pull(repo: Repo, number: int, tok: str,
             with_comments: bool = True) -> PullRequest:
    r = _request("GET", f"{API}/repos/{repo.slug}/pulls/{number}", tok)
    d = r.json()

    pr = PullRequest(
        number=d["number"],
        title=d.get("title") or "",
        body=d.get("body") or "",
        state=d.get("state") or "",
        draft=bool(d.get("draft")),
        author=(d.get("user") or {}).get("login") or "unknown",
        base_ref=d["base"]["ref"],
        base_sha=d["base"]["sha"],
        head_ref=d["head"]["ref"],
        head_sha=d["head"]["sha"],
        head_repo=((d["head"].get("repo") or {}).get("full_name")),
        additions=d.get("additions") or 0,
        deletions=d.get("deletions") or 0,
        changed_files=d.get("changed_files") or 0,
    )
    if with_comments:
        pr.comments = get_review_comments(repo, number, tok)
    return pr


def get_review_comments(repo: Repo, number: int, tok: str,
                        limit: int = 40) -> list[dict]:
    """Existing inline review comments, so a review can avoid repeating them."""
    out: list[dict] = []
    url = f"{API}/repos/{repo.slug}/pulls/{number}/comments?per_page=100"
    while url and len(out) < limit:
        r = _request("GET", url, tok)
        for c in r.json():
            out.append({
                "author": (c.get("user") or {}).get("login") or "unknown",
                "path": c.get("path") or "",
                "line": c.get("line") or c.get("original_line"),
                "body": c.get("body") or "",
            })
        url = r.links.get("next", {}).get("url")
    return out[:limit]


def get_diff(repo: Repo, number: int, tok: str) -> str:
    """The PR's diff, as GitHub computes it (three-dot, against the base)."""
    headers = _headers(tok) | {"Accept": "application/vnd.github.v3.diff"}
    try:
        r = requests.get(f"{API}/repos/{repo.slug}/pulls/{number}",
                         headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GitHubError(f"Could not reach GitHub: {e}") from None
    if r.status_code >= 400:
        raise GitHubError(f"Could not fetch the diff for #{number} "
                          f"({r.status_code}).")
    return r.text


def post_review(repo: Repo, number: int, tok: str, body: str,
                commit_sha: str | None = None) -> str:
    """Leave one review comment on the PR. Returns its URL.

    Always event=COMMENT. CodeOrbit does not approve or request changes: a
    local 3.8B model's opinion is not a merge gate, and an automated APPROVE
    can dismiss a human reviewer's pending request on some branch protection
    settings.
    """
    payload = {"body": body, "event": "COMMENT"}
    if commit_sha:
        payload["commit_id"] = commit_sha
    r = _request("POST", f"{API}/repos/{repo.slug}/pulls/{number}/reviews",
                 tok, json=payload)
    return r.json().get("html_url") or f"https://github.com/{repo.slug}/pull/{number}"
