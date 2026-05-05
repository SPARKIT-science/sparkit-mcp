"""SPARKIT MCP server.

Exposes two tools to MCP clients (Claude Desktop, Cursor, Claude Code):
- ``research``        — submit a question, poll until done (or until a
                        timeout the caller sets), return the cited
                        Markdown report.
- ``get_job_status``  — fetch the current status (and result if done) of
                        a job by id. Useful when ``research`` returned
                        before the job finished, or to revisit a past
                        result.

Transport is stdio, the universally-supported MCP transport. The server
uses the official ``mcp`` SDK from Anthropic.

Configuration (env vars):
- SPARKIT_API_KEY              required. Bearer key from app.sparkit.science.
- SPARKIT_API_BASE             optional. Override the API base URL.
- SPARKIT_API_TIMEOUT_SECONDS  optional. Per-request HTTP timeout (default 30).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import (
    Job,
    ResearchResult,
    SparkitAPIError,
    SparkitClient,
)

logger = logging.getLogger("sparkit_mcp")

# Polling cadence for ``research``. 5s keeps the response feel snappy
# (reports usually finish in 60-180s) without flooding the API. A
# multiplicative backoff isn't worth the complexity at single-job scale.
_POLL_INTERVAL_SECONDS = 5.0

# Default sync wait for ``research``. 4 minutes covers the long tail of
# jobs without hitting Claude Desktop's ~5-minute tool timeout. Callers
# can pass a higher value, but most clients will reject anything beyond
# their own ceiling.
_DEFAULT_MAX_WAIT_SECONDS = 240
_MIN_MAX_WAIT_SECONDS = 30
_MAX_MAX_WAIT_SECONDS = 540  # 9 minutes; longer than any client allows

mcp = FastMCP("sparkit")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_completed(job: Job) -> str:
    """Render a completed Job as Markdown for the LLM client.

    Layout: title-line with elapsed/iterations stats, the answer
    Markdown (already Markdown from the API), then a numbered "Sources"
    section. Keeping the layout deterministic helps the LLM cite back to
    sources by index when it reasons over the result.
    """
    if job.result is None:
        return f"Job {job.job_id} completed but has no result."

    result: ResearchResult = job.result
    parts: list[str] = []

    stats = result.process_stats
    if stats is not None:
        parts.append(
            f"_Elapsed {stats.elapsed_seconds:.0f}s · "
            f"{stats.iterations} iterations · "
            f"{stats.papers_read} papers read · "
            f"{stats.sources_cited} sources cited_"
        )
        parts.append("")

    parts.append(result.answer_text.rstrip())

    if result.sources:
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("**Sources**")
        parts.append("")
        for s in result.sources:
            label = s.title or s.url or s.doi or f"Source {s.id}"
            extras: list[str] = []
            if s.url:
                extras.append(s.url)
            elif s.doi:
                extras.append(f"doi:{s.doi}")
            if s.year:
                extras.append(str(s.year))
            tail = f" ({' · '.join(extras)})" if extras else ""
            parts.append(f"{s.id}. {label}{tail}")

    return "\n".join(parts).rstrip() + "\n"


def _format_in_flight(job: Job, waited_seconds: float) -> str:
    return (
        f"Job `{job.job_id}` is still {job.status} after "
        f"{waited_seconds:.0f}s. Use `get_job_status` with this id to "
        f"check progress; SPARKIT jobs typically finish within 60-180s "
        f"but can take longer for deep questions."
    )


def _format_terminal_failure(job: Job) -> str:
    return (
        f"Job `{job.job_id}` ended with status `{job.status}`. "
        "Re-submit with a clearer question, or check "
        "https://app.sparkit.science/admin for details if you have "
        "operator access."
    )


# ---------------------------------------------------------------------------
# Tool: research
# ---------------------------------------------------------------------------


@mcp.tool()
async def research(
    question: str,
    response_format: str = "full",
    include_citations: bool = True,
    max_wait_seconds: int = _DEFAULT_MAX_WAIT_SECONDS,
) -> str:
    """Submit a scientific question to the SPARKIT research agent.

    SPARKIT searches the literature, reads relevant papers, and returns
    a cited Markdown report. Best for questions where a correct answer
    requires synthesizing across multiple primary sources.

    Args:
        question: Free-text scientific question. Be specific —
            "Which kinases are upregulated in pancreatic cancer with
            evidence from human tissue?" works better than "tell me
            about pancreatic cancer."
        response_format: ``"full"`` (default) for a multi-paragraph
            Markdown report, or ``"brief"`` for a tighter summary.
        include_citations: Keep ``True`` (default) so the report is
            usable for downstream work; only set ``False`` if you
            specifically want unsourced prose.
        max_wait_seconds: How long to block waiting for the job before
            returning the job_id with instructions to poll via
            ``get_job_status``. Default 240s (4 min). Range 30-540.

    Returns the cited Markdown report on success. If the job is still
    running at the wait limit, returns the job_id and status so the
    caller can resume with ``get_job_status``.
    """
    if not question or not question.strip():
        return "Error: `question` is required and cannot be empty."

    wait = max(_MIN_MAX_WAIT_SECONDS, min(_MAX_MAX_WAIT_SECONDS, max_wait_seconds))
    if response_format not in ("full", "brief"):
        return "Error: `response_format` must be 'full' or 'brief'."

    try:
        async with SparkitClient() as client:
            job = await client.submit_research(
                question.strip(),
                response_format=response_format,
                include_citations=include_citations,
            )
            logger.info("Submitted SPARKIT job %s", job.job_id)
            return await _await_completion(client, job, wait)
    except SparkitAPIError as e:
        return _format_api_error(e)


async def _await_completion(
    client: SparkitClient, job: Job, max_wait_seconds: int
) -> str:
    """Poll until the job is terminal or until ``max_wait_seconds`` elapses."""
    elapsed = 0.0
    current = job
    while current.status in {"queued", "running"} and elapsed < max_wait_seconds:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        try:
            current = await client.get_job(job.job_id)
        except SparkitAPIError as e:
            # Transient lookup error mid-poll; bail out with the job_id so
            # the caller can resume rather than swallowing progress.
            return (
                f"Submitted as `{job.job_id}` but couldn't fetch status: "
                f"{e.message}. Use `get_job_status` to check on it."
            )

    if current.status == "completed":
        return _format_completed(current)
    if current.status in {"failed", "cancelled"}:
        return _format_terminal_failure(current)
    # Still queued or running and we ran out the clock.
    return _format_in_flight(current, elapsed)


# ---------------------------------------------------------------------------
# Tool: get_job_status
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_job_status(job_id: str) -> str:
    """Fetch the current status (and result if done) of a SPARKIT job.

    Use this when ``research`` returned before the job finished, or to
    revisit a previous result by id.

    Args:
        job_id: The id returned by a prior ``research`` call.

    Returns the cited Markdown report if the job has completed, a status
    line if it's still running, or a failure message otherwise.
    """
    if not job_id or not job_id.strip():
        return "Error: `job_id` is required."

    try:
        async with SparkitClient() as client:
            job = await client.get_job(job_id.strip())
    except SparkitAPIError as e:
        return _format_api_error(e)

    if job.status == "completed":
        return _format_completed(job)
    if job.status in {"failed", "cancelled"}:
        return _format_terminal_failure(job)
    return f"Job `{job.job_id}` is currently {job.status}."


# ---------------------------------------------------------------------------
# Errors and entry point
# ---------------------------------------------------------------------------


def _format_api_error(e: SparkitAPIError) -> str:
    if e.status_code == 401:
        return (
            "Authentication failed. Check that SPARKIT_API_KEY is set "
            "correctly. Get a key at https://app.sparkit.science/keys."
        )
    if e.status_code == 402:
        return (
            f"Quota exhausted: {e.message} "
            "Visit https://app.sparkit.science/billing to add credits "
            "or upgrade your plan."
        )
    if e.status_code == 429:
        return f"Rate limited: {e.message} Try again in a moment."
    if e.status_code == 404:
        return f"Not found: {e.message}"
    return f"SPARKIT API error ({e.status_code}): {e.message}"


def main() -> None:
    """Console-script entry point. Runs the MCP server on stdio."""
    # Logs go to stderr so they don't interfere with the stdio MCP
    # protocol on stdout. Default level is INFO; override with
    # PYTHONLOGLEVEL or by calling `logging.basicConfig` before main().
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s sparkit-mcp %(levelname)s %(message)s",
    )
    mcp.run()


__all__ = ["main", "mcp", "research", "get_job_status"]
