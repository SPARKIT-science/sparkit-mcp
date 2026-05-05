"""Tests for the sparkit-mcp server.

We exercise the decorated tool functions directly. The transport layer
(stdio + JSON-RPC framing) is covered by the upstream `mcp` package.

httpx is mocked with ``respx`` so no network call leaves the test
process. Each test sets a fake SPARKIT_API_KEY so the client constructor
succeeds.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
import respx

from sparkit_mcp import server
from sparkit_mcp.client import DEFAULT_BASE_URL


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SPARKIT_API_KEY", "sk_test_fake")
    # Tighten the polling cadence so tests don't sleep for seconds at a
    # time. 0.01s gives respx enough room to register requests in order.
    monkeypatch.setattr(server, "_POLL_INTERVAL_SECONDS", 0.01)
    # Drop the minimum-wait clamp too so timeout tests don't burn 30s
    # of wall clock when their max_wait_seconds is below the real-world
    # clamp.
    monkeypatch.setattr(server, "_MIN_MAX_WAIT_SECONDS", 0.05)


def _job(status: str, *, with_result: bool = False, job_id: str = "job_abc") -> dict:
    body = {
        "job_id": job_id,
        "status": status,
        "created_at": "2026-05-04T00:00:00Z",
        "completed_at": "2026-05-04T00:01:30Z" if with_result else None,
        "result": (
            {
                "answer_text": "## Answer\n\nKinase Foo is upregulated [1].",
                "sources": [
                    {
                        "id": 1,
                        "title": "A paper about Foo",
                        "url": "https://example.com/foo",
                        "year": 2024,
                    }
                ],
                "process_stats": {
                    "elapsed_seconds": 90.0,
                    "iterations": 4,
                    "papers_read": 12,
                    "sources_cited": 1,
                },
            }
            if with_result
            else None
        ),
    }
    return body


# ---------------------------------------------------------------------------
# research happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_research_polls_until_complete_and_returns_markdown() -> None:
    submit = respx.post(f"{DEFAULT_BASE_URL}/v1/research").mock(
        return_value=httpx.Response(202, json=_job("queued"))
    )
    poll = respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_abc").mock(
        side_effect=[
            httpx.Response(200, json=_job("running")),
            httpx.Response(200, json=_job("completed", with_result=True)),
        ]
    )

    out = await server.research("Which kinases are upregulated in PDAC?")

    assert submit.called
    assert poll.call_count == 2
    # Stats line, the answer, and the sources list are all rendered.
    assert "Elapsed 90s" in out
    assert "Kinase Foo is upregulated" in out
    assert "**Sources**" in out
    assert "A paper about Foo" in out
    assert "https://example.com/foo" in out


@respx.mock
async def test_research_returns_job_id_on_timeout() -> None:
    """If the job is still running when max_wait_seconds elapses, we
    surface the job_id so the caller can resume with get_job_status."""
    respx.post(f"{DEFAULT_BASE_URL}/v1/research").mock(
        return_value=httpx.Response(202, json=_job("running", job_id="job_slow"))
    )
    respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_slow").mock(
        return_value=httpx.Response(200, json=_job("running", job_id="job_slow"))
    )

    out = await server.research(
        "Long question",
        # Fixture lowers _MIN_MAX_WAIT_SECONDS to 0.05s for the test.
        # In production the server clamps the minimum to 30s.
        max_wait_seconds=0,
    )

    assert "job_slow" in out
    assert "still running" in out


@respx.mock
async def test_research_returns_failure_message_on_failed_status() -> None:
    respx.post(f"{DEFAULT_BASE_URL}/v1/research").mock(
        return_value=httpx.Response(202, json=_job("queued"))
    )
    respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_abc").mock(
        return_value=httpx.Response(200, json=_job("failed"))
    )

    out = await server.research("Question")
    assert "ended with status `failed`" in out


# ---------------------------------------------------------------------------
# research validation + auth
# ---------------------------------------------------------------------------


async def test_research_rejects_blank_question() -> None:
    out = await server.research("   ")
    assert "Error" in out
    assert "question" in out.lower()


async def test_research_rejects_invalid_response_format() -> None:
    out = await server.research("Q", response_format="brief-er")
    assert "Error" in out


@respx.mock
async def test_research_surfaces_401_with_actionable_message() -> None:
    respx.post(f"{DEFAULT_BASE_URL}/v1/research").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid API key.",
                }
            },
        )
    )
    out = await server.research("Q")
    assert "Authentication failed" in out
    assert "SPARKIT_API_KEY" in out


@respx.mock
async def test_research_surfaces_quota_exhausted() -> None:
    respx.post(f"{DEFAULT_BASE_URL}/v1/research").mock(
        return_value=httpx.Response(
            402,
            json={
                "error": {
                    "code": "quota_exhausted",
                    "message": "Out of credits and no active subscription.",
                }
            },
        )
    )
    out = await server.research("Q")
    assert "Quota exhausted" in out
    assert "billing" in out.lower()


def test_missing_api_key_surfaces_via_client(monkeypatch) -> None:
    monkeypatch.delenv("SPARKIT_API_KEY", raising=False)
    # Construction-time validation; surfaces inside the tool when run.
    out = asyncio.run(server.research("Q"))
    assert "Authentication failed" in out or "SPARKIT_API_KEY" in out


# ---------------------------------------------------------------------------
# get_job_status
# ---------------------------------------------------------------------------


async def test_get_job_status_rejects_blank() -> None:
    out = await server.get_job_status("")
    assert "Error" in out


@respx.mock
async def test_get_job_status_returns_completed_report() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_xyz").mock(
        return_value=httpx.Response(
            200, json=_job("completed", with_result=True, job_id="job_xyz")
        )
    )
    out = await server.get_job_status("job_xyz")
    assert "Kinase Foo is upregulated" in out


@respx.mock
async def test_get_job_status_returns_status_when_running() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_xyz").mock(
        return_value=httpx.Response(200, json=_job("running", job_id="job_xyz"))
    )
    out = await server.get_job_status("job_xyz")
    assert "currently running" in out


@respx.mock
async def test_get_job_status_404() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/v1/research/job_missing").mock(
        return_value=httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "Job not found."}},
        )
    )
    out = await server.get_job_status("job_missing")
    assert "Not found" in out
