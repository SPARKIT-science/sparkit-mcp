"""Thin async client around the SPARKIT REST API.

Covers the two endpoints the MCP tools need:
- POST /v1/research        — submit a question, returns a Job (queued/running)
- GET  /v1/research/{id}   — fetch current status / final result

Errors are raised as ``SparkitAPIError`` with the HTTP status and the
parsed message body (when available), so callers can surface a useful
explanation rather than a raw httpx exception.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://jlsteenwyk--sparkit-api-web.modal.run"
ENV_API_KEY = "SPARKIT_API_KEY"
ENV_API_BASE = "SPARKIT_API_BASE"
ENV_TIMEOUT = "SPARKIT_API_TIMEOUT_SECONDS"

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0


class SparkitAPIError(Exception):
    """API-side error (4xx/5xx). status_code is the HTTP code; message is
    the API's `error.message` when present, else httpx's reason phrase."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code
        self.message = message
        self.code = code


@dataclass
class Source:
    id: int
    title: str | None = None
    url: str | None = None
    doi: str | None = None
    year: int | None = None
    citation_count: int | None = None


@dataclass
class ProcessStats:
    elapsed_seconds: float = 0.0
    iterations: int = 0
    searches: int = 0
    papers_read: int = 0
    calculations: int = 0
    sources_cited: int = 0


@dataclass
class ResearchResult:
    answer_text: str
    sources: list[Source]
    process_stats: ProcessStats | None = None


@dataclass
class Job:
    job_id: str
    status: str  # queued | running | completed | failed | cancelled
    created_at: str
    completed_at: str | None = None
    result: ResearchResult | None = None


def _result_from_dict(d: dict[str, Any]) -> ResearchResult:
    sources = [
        Source(
            id=s.get("id", 0),
            title=s.get("title"),
            url=s.get("url"),
            doi=s.get("doi"),
            year=s.get("year"),
            citation_count=s.get("citation_count"),
        )
        for s in (d.get("sources") or [])
    ]
    stats_raw = d.get("process_stats")
    stats = (
        ProcessStats(
            elapsed_seconds=float(stats_raw.get("elapsed_seconds", 0) or 0),
            iterations=int(stats_raw.get("iterations", 0) or 0),
            searches=int(stats_raw.get("searches", 0) or 0),
            papers_read=int(stats_raw.get("papers_read", 0) or 0),
            calculations=int(stats_raw.get("calculations", 0) or 0),
            sources_cited=int(stats_raw.get("sources_cited", 0) or 0),
        )
        if stats_raw
        else None
    )
    return ResearchResult(
        answer_text=d.get("answer_text", ""), sources=sources, process_stats=stats
    )


def _job_from_dict(d: dict[str, Any]) -> Job:
    result = _result_from_dict(d["result"]) if d.get("result") else None
    return Job(
        job_id=d["job_id"],
        status=d["status"],
        created_at=d["created_at"],
        completed_at=d.get("completed_at"),
        result=result,
    )


class SparkitClient:
    """Async client. Owns its own httpx.AsyncClient — caller is responsible
    for using ``async with`` so the connection pool is released."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get(ENV_API_KEY, "").strip()
        if not self.api_key:
            raise SparkitAPIError(
                401,
                f"{ENV_API_KEY} is not set. Get an API key at "
                "https://app.sparkit.science/keys and pass it via the "
                f"{ENV_API_KEY} environment variable.",
            )
        self.base_url = (base_url or os.environ.get(ENV_API_BASE) or DEFAULT_BASE_URL).rstrip("/")
        if timeout_seconds is None:
            env_timeout = os.environ.get(ENV_TIMEOUT)
            timeout_seconds = float(env_timeout) if env_timeout else DEFAULT_HTTP_TIMEOUT_SECONDS

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "sparkit-mcp/0.1",
            },
        )

    async def __aenter__(self) -> "SparkitClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit_research(
        self,
        question: str,
        *,
        response_format: str = "full",
        include_citations: bool = True,
        max_answer_tokens: int | None = None,
    ) -> Job:
        body: dict[str, Any] = {
            "question": question,
            "response_format": response_format,
            "include_citations": include_citations,
        }
        if max_answer_tokens is not None:
            body["max_answer_tokens"] = max_answer_tokens
        resp = await self._client.post("/v1/research", json=body)
        return _job_from_dict(_unwrap(resp))

    async def get_job(self, job_id: str) -> Job:
        resp = await self._client.get(f"/v1/research/{job_id}")
        return _job_from_dict(_unwrap(resp))


def _unwrap(resp: httpx.Response) -> dict[str, Any]:
    """Raise ``SparkitAPIError`` on non-2xx; otherwise return parsed JSON."""
    if 200 <= resp.status_code < 300:
        try:
            return resp.json()
        except ValueError as e:
            raise SparkitAPIError(
                resp.status_code, f"Malformed JSON response: {e}"
            ) from e

    # Error path — try to extract the API's structured error.
    code: str | None = None
    message = resp.reason_phrase or "Request failed."
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or message
    except ValueError:
        pass
    raise SparkitAPIError(resp.status_code, message, code=code)
