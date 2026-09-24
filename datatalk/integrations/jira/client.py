"""A thin Jira Cloud REST client: only the calls the sync needs.

Two things are load-bearing:

* **The host is allowlisted** (:func:`normalize_site`). The server makes
  authenticated requests to whatever host an admin types, so an unchecked host
  is an SSRF primitive into the deployment's own network. Redirects are not
  followed for the same reason -- an allowed host must not be able to bounce
  the request somewhere that is not.
* **Rate limits are honoured, not fought.** Jira answers 429 with
  ``Retry-After``; we sleep that long (capped) and retry a bounded number of
  times, then fail the sync with a message rather than hammer.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_TIMEOUT_S = 30.0
_MAX_ATTEMPTS = 5
_MAX_RETRY_SLEEP_S = 60.0
# /search/jql caps maxResults at 100 when fields are requested.
_PAGE_SIZE = 100


class JiraError(RuntimeError):
    """A Jira call failed. The message is safe to show an admin."""


class JiraHostNotAllowedError(ValueError):
    """The site is not under an allowed suffix (``DATATALK_JIRA_ALLOWED_HOST_SUFFIXES``)."""


def normalize_site(raw: str, allowed_suffixes: list[str]) -> str:
    """``https://Acme.atlassian.net/jira/`` → ``acme.atlassian.net``, or raise.

    Accepts what people paste (scheme, trailing path) and reduces it to a bare
    hostname; rejects ports, userinfo, IP literals and anything off-allowlist.
    """
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    if not s or "@" in s or ":" in s or not _HOST_RE.match(s):
        raise JiraHostNotAllowedError(raw)
    if not any(s.endswith(suf) and len(s) > len(suf) for suf in allowed_suffixes):
        raise JiraHostNotAllowedError(raw)
    return s


class JiraClient:
    """Basic auth (account email + API token) against one Jira Cloud site."""

    def __init__(
        self,
        site: str,
        email: str,
        api_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.site = site
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=f"https://{site}",
            auth=(email, api_token),
            timeout=_TIMEOUT_S,
            follow_redirects=False,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- transport ------------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._http.request(method, path, **kw)
            except httpx.HTTPError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise JiraError(f"Could not reach {self.site}: {exc}") from exc
                self._sleep(min(2.0**attempt, _MAX_RETRY_SLEEP_S))
                continue

            if resp.status_code in (429, 503) and attempt < _MAX_ATTEMPTS:
                self._sleep(_retry_after(resp, attempt))
                continue
            if resp.status_code == 401:
                raise JiraError("Jira rejected the email / API token (401).")
            if resp.status_code == 403:
                raise JiraError("This Jira account is not allowed to do that (403).")
            if 300 <= resp.status_code < 400:
                # Not followed on purpose; see the module docstring.
                raise JiraError(
                    f"Jira redirected ({resp.status_code}); check the site name."
                )
            if resp.status_code >= 400:
                raise JiraError(_error_text(resp))
            return resp.json() if resp.content else None
        raise JiraError(f"Jira kept rate-limiting after {_MAX_ATTEMPTS} attempts.")

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, json=body)

    # --- endpoints ------------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        return self.get("/rest/api/3/myself")

    def fields(self) -> list[dict[str, Any]]:
        return self.get("/rest/api/3/field")

    def statuses(self) -> list[dict[str, Any]]:
        return self.get("/rest/api/3/status")

    def approximate_count(self, jql: str) -> int:
        out = self.post("/rest/api/3/search/approximate-count", {"jql": jql})
        return int((out or {}).get("count", 0))

    def search(
        self, jql: str, *, fields: list[str], expand: str = "changelog"
    ) -> Iterator[list[dict[str, Any]]]:
        """Pages of issues via the token-paginated ``/search/jql``.

        (The offset-paginated ``/search`` is deprecated on Jira Cloud.)
        """
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "fields": ",".join(fields),
                "expand": expand,
                "maxResults": _PAGE_SIZE,
            }
            if token:
                params["nextPageToken"] = token
            page = self.get("/rest/api/3/search/jql", **params) or {}
            yield list(page.get("issues") or [])
            token = page.get("nextPageToken")
            if page.get("isLast", not token) or not token:
                return

    def changelog(self, issue_id: str) -> list[dict[str, Any]]:
        """Every changelog history of one issue, when the inline one was cut short."""
        return list(
            self._offset_pages(
                f"/rest/api/3/issue/{issue_id}/changelog", key="values"
            )
        )

    def worklogs(self, issue_id: str) -> list[dict[str, Any]]:
        return list(
            self._offset_pages(f"/rest/api/3/issue/{issue_id}/worklog", key="worklogs")
        )

    # --- Jira Software (agile) --------------------------------------------------
    # Only sites with Jira Software have these, and only accounts with board
    # access see them; the sync treats a failure here as "no boards".

    def boards(self) -> list[dict[str, Any]]:
        return list(self._offset_pages("/rest/agile/1.0/board", key="values"))

    def board_sprints(self, board_id: int) -> list[dict[str, Any]]:
        return list(
            self._offset_pages(f"/rest/agile/1.0/board/{int(board_id)}/sprint", key="values")
        )

    def _offset_pages(self, path: str, *, key: str) -> Iterator[dict[str, Any]]:
        start = 0
        while True:
            page = self.get(path, startAt=start, maxResults=_PAGE_SIZE) or {}
            items = list(page.get(key) or [])
            yield from items
            start += len(items)
            total = page.get("total")
            if (
                not items
                or page.get("isLast") is True
                or (total is not None and start >= int(total))
            ):
                return


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), _MAX_RETRY_SLEEP_S)
    except ValueError:
        return min(2.0**attempt, _MAX_RETRY_SLEEP_S)


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msgs = list(body.get("errorMessages") or []) + [
            f"{k}: {v}" for k, v in (body.get("errors") or {}).items()
        ]
        if msgs:
            return f"Jira error ({resp.status_code}): " + "; ".join(msgs)
    except Exception:  # noqa: BLE001
        pass
    return f"Jira error ({resp.status_code})."
