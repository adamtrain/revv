"""A small async GitHub API client (GraphQL + REST) built on httpx."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Any

import httpx


class GitHubError(Exception):
    """Something went wrong talking to GitHub; the message is fit for the user."""


class AuthError(GitHubError):
    pass


def api_urls(host: str) -> tuple[str, str]:
    """(REST base, GraphQL endpoint) for github.com or a GitHub Enterprise host."""
    if host in ("github.com", "api.github.com"):
        return "https://api.github.com", "https://api.github.com/graphql"
    return f"https://{host}/api/v3", f"https://{host}/api/graphql"


def find_token(host: str = "github.com") -> str:
    """Find a token: GH_TOKEN/GITHUB_TOKEN (or the enterprise variants), then `gh auth token`."""
    names = (
        ("GH_TOKEN", "GITHUB_TOKEN")
        if host == "github.com"
        else ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    )
    for name in names:
        if token := os.environ.get(name, "").strip():
            return token
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run(
                [gh, "auth", "token", "--hostname", host],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    raise AuthError(
        f"No GitHub token found for {host}. Run `gh auth login` (GitHub CLI) "
        "or set GH_TOKEN / GITHUB_TOKEN."
    )


def _error_message(errors: list[dict[str, Any]]) -> str:
    messages = []
    for error in errors:
        message = error.get("message") or str(error)
        if message not in messages:
            messages.append(message)
    return "; ".join(messages)


class GitHubClient:
    def __init__(self, token: str, host: str = "github.com") -> None:
        self.host = host
        self.rest_base, self.graphql_url = api_urls(host)
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "revv",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as error:
                last_error = error
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            if response.status_code in (502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return response
        raise GitHubError(f"Network error talking to {self.host}: {last_error}")

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AuthError("GitHub rejected the token (401). Try `gh auth refresh`.")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("GitHub API rate limit exceeded; try again in a little while.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise GitHubError(f"GitHub API error {response.status_code}: {detail}")

    async def graphql(self, document: str, /, **variables: Any) -> dict[str, Any]:
        response = await self._send(
            "POST", self.graphql_url, json={"query": document, "variables": variables}
        )
        self._check(response)
        payload = response.json()
        errors = payload.get("errors")
        data = payload.get("data")
        if errors:
            is_mutation = document.lstrip().startswith("mutation")
            if (
                data is None
                or is_mutation
                or any(e.get("type") in ("NOT_FOUND", "FORBIDDEN") for e in errors)
            ):
                raise GitHubError(_error_message(errors))
        if data is None:
            raise GitHubError("GitHub returned no data")
        return data

    async def rest(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        accept: str | None = None,
    ) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        response = await self._send(
            method, f"{self.rest_base}{path}", params=params, json=json, headers=headers
        )
        self._check(response)
        return response
