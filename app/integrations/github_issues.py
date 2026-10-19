"""GitHub Issues adapter: create an issue, find one by incident marker, read its state. Nothing else.

* No arbitrary GitHub API access: three fixed operations against ONE configured repository.
* The token is only ever placed in the Authorization header. It is never logged, repr()'d, or put in an error.
  Response bodies are never copied into errors either (only a fixed ``kind``).
* ``create_issue`` is NOT retried automatically (a retry could create a duplicate). The caller recovers from an
  unconfirmed create by calling ``find_issue_by_marker`` first. Read operations get a small bounded retry.
* Tests use ``httpx.MockTransport``; this module is never exercised against the real GitHub by the default suite.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

PROVIDER = "github_issues"
DEFAULT_API_URL = "https://api.github.com"
LABEL = "dataops-incident"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MARKER_PREFIX = "<!-- dataops-incident:"


def incident_marker(incident_id: str) -> str:
    """The stable, machine-readable duplicate-detection marker stored in the issue body."""
    return f"{_MARKER_PREFIX}{incident_id} -->"


class TicketError(Exception):
    """A ticket-provider failure. ``kind`` is one of the fixed values below; the message is generic by design."""

    KINDS = ("not_configured", "auth_failed", "permission_denied", "not_found", "rate_limited", "timeout",
             "unavailable", "service_error", "bad_response")

    def __init__(self, kind: str, *, retry_after: int | None = None, unconfirmed: bool = False) -> None:
        assert kind in self.KINDS
        self.kind, self.retry_after, self.unconfirmed = kind, retry_after, unconfirmed
        super().__init__(kind)


@dataclass(frozen=True)
class Issue:
    external_id: str
    number: int
    url: str
    state: str  # open | closed
    title: str = ""


@dataclass(frozen=True)
class GitHubConfig:
    token: str = field(repr=False)
    owner: str
    repo: str
    api_url: str = DEFAULT_API_URL

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "GitHubConfig | None":
        """None when ticketing is not configured (token, owner or repo missing). Invalid-but-present values raise
        ValueError with a message that never contains the token."""
        token = (environ.get("GITHUB_TOKEN") or "").strip()
        owner = (environ.get("GITHUB_OWNER") or "").strip()
        repo = (environ.get("GITHUB_REPO") or "").strip()
        if not (token and owner and repo):
            return None
        api_url = (environ.get("GITHUB_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
        if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
            raise ValueError("GITHUB_OWNER / GITHUB_REPO contain unsupported characters")
        if not api_url.startswith("https://"):
            raise ValueError("GITHUB_API_URL must be an https:// URL")
        if len(token) > 500 or any(ch.isspace() for ch in token):
            raise ValueError("GITHUB_TOKEN is malformed")
        return cls(token=token, owner=owner, repo=repo, api_url=api_url)


def _retry_after(resp: httpx.Response) -> int | None:
    try:
        if "retry-after" in resp.headers:
            return max(1, min(int(float(resp.headers["retry-after"])), 3600))
        if resp.headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in resp.headers:
            return max(1, min(int(resp.headers["x-ratelimit-reset"]) - int(time.time()), 3600))
    except ValueError:
        return None
    return None


class GitHubIssuesAdapter:
    provider = PROVIDER

    def __init__(self, config: GitHubConfig, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0,
                 max_read_retries: int = 2, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.max_read_retries, self._sleep = max_read_retries, sleep
        self._client = httpx.Client(
            base_url=config.api_url, transport=transport, timeout=timeout, follow_redirects=False,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "ai-dataops-agent", "Authorization": f"Bearer {config.token}"})

    @property
    def repository(self) -> str:
        return self.config.repository

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:  # never shows the token
        return f"GitHubIssuesAdapter(repository={self.repository!r})"

    # ------------------------------------------------------------------------------------ plumbing
    def _issues_path(self, suffix: str = "") -> str:
        return f"/repos/{self.config.owner}/{self.config.repo}/issues{suffix}"

    @staticmethod
    def _classify(resp: httpx.Response) -> TicketError | None:
        code = resp.status_code
        if 200 <= code < 300:
            return None
        if code == 401:
            return TicketError("auth_failed")
        if code == 429:
            return TicketError("rate_limited", retry_after=_retry_after(resp))
        if code == 403:
            limited = resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
            return TicketError("rate_limited", retry_after=_retry_after(resp)) if limited else TicketError("permission_denied")
        if code in (404, 410):
            return TicketError("not_found")
        if code >= 500:
            return TicketError("service_error")
        return TicketError("bad_response")

    def _send(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> httpx.Response:
        attempts = (self.max_read_retries + 1) if retry else 1
        last: TicketError | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                last = TicketError("timeout", unconfirmed=(method == "POST"))
            except httpx.HTTPError:
                last = TicketError("unavailable", unconfirmed=(method == "POST"))
            else:
                err = self._classify(resp)
                if err is None:
                    return resp
                if err.kind != "service_error":  # auth / permission / rate limit / not found: retrying will not help
                    raise err
                last = err
            if attempt + 1 < attempts:
                self._sleep(min(2 ** attempt * 0.5, 4.0))
        assert last is not None
        raise last

    @staticmethod
    def _issue(data: Any) -> Issue:
        try:
            return Issue(external_id=str(data["id"]), number=int(data["number"]), url=str(data["html_url"]),
                         state="closed" if data.get("state") == "closed" else "open", title=str(data.get("title", ""))[:200])
        except (KeyError, TypeError, ValueError) as exc:
            raise TicketError("bad_response") from exc

    def _json(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise TicketError("bad_response") from exc

    # ------------------------------------------------------------------------------------ operations
    def create_issue(self, *, title: str, body: str, labels: list[str] | None = None) -> Issue:
        resp = self._send("POST", self._issues_path(), retry=False,
                          json={"title": title[:256], "body": body[:60000], "labels": labels if labels is not None else [LABEL]})
        return self._issue(self._json(resp))

    def find_issue_by_marker(self, marker: str, *, max_pages: int = 5) -> Issue | None:
        """Look for an existing issue carrying ``marker`` (so an unconfirmed create is never repeated)."""
        for page in range(1, max_pages + 1):
            resp = self._send("GET", self._issues_path(), retry=True,
                              params={"labels": LABEL, "state": "all", "per_page": 100, "page": page})
            items = self._json(resp)
            if not isinstance(items, list):
                raise TicketError("bad_response")
            for item in items:
                if isinstance(item, dict) and "pull_request" not in item and marker in str(item.get("body") or ""):
                    return self._issue(item)
            if len(items) < 100:
                return None
        return None

    def get_issue(self, number: int) -> Issue:
        resp = self._send("GET", self._issues_path(f"/{int(number)}"), retry=True)
        return self._issue(self._json(resp))
