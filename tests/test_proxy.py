"""
Behavioral tests for proxy.py.

Each test verifies one observable behavior of the proxy. The proxy runs as a real
HTTP server in a background thread. Upstream API calls are intercepted at the
urllib boundary so tests control what the "Anthropic API" returns without making
real network calls.

The test client uses http.client directly (not urllib) so that patching
urllib.request.urlopen only intercepts the proxy's outbound calls, not the
test's own inbound calls.
"""

import http.client
import json
import socket
import threading
import urllib.error
import urllib.parse
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import proxy


# ── Helpers ────────────────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def make_upstream_response(body: dict, status: int = 200) -> MagicMock:
    """Build a mock that looks like the object returned by urllib.request.urlopen()."""
    raw = json.dumps(body).encode()
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    resp.read.return_value = raw
    resp.headers.items.return_value = [("content-type", "application/json")]
    return resp


def make_upstream_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=None, code=code, msg="", hdrs=MagicMock(), fp=BytesIO(body)
    )


def call_proxy(server_url: str, body: dict) -> tuple[int, bytes]:
    """POST to the proxy and return (status_code, response_body)."""
    parsed = urllib.parse.urlparse(server_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    data = json.dumps(body).encode()
    conn.request(
        "POST", "/v1/messages", body=data,
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    return resp.status, resp.read()


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def span_exporter():
    """
    Wire an in-memory span exporter into the proxy's already-initialised
    tracer provider. SimpleSpanProcessor exports synchronously so spans are
    available immediately after the HTTP response is received.
    """
    exporter = InMemorySpanExporter()
    proxy.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture(scope="module")
def server_url(span_exporter):
    """Start the proxy HTTP server once for all tests in this module."""
    from http.server import HTTPServer

    port = free_port()
    server = HTTPServer(("127.0.0.1", port), proxy.LoggingProxy)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ── Forwarding behavior ────────────────────────────────────────────────────────


class TestHttpForwarding:
    def test_upstream_response_body_is_returned_unchanged(self, server_url, span_exporter):
        upstream = {"id": "msg_1", "content": "hello", "usage": {"input_tokens": 5, "output_tokens": 3}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            _, body = call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert json.loads(body) == upstream

    def test_upstream_success_status_code_is_forwarded(self, server_url, span_exporter):
        with patch("urllib.request.urlopen", return_value=make_upstream_response({}, 200)):
            status, _ = call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert status == 200

    def test_upstream_error_status_code_is_forwarded(self, server_url, span_exporter):
        with patch("urllib.request.urlopen", side_effect=make_upstream_error(429)):
            status, _ = call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert status == 429

    def test_upstream_error_body_is_forwarded(self, server_url, span_exporter):
        error_body = b'{"error": "rate_limited"}'
        with patch("urllib.request.urlopen", side_effect=make_upstream_error(429, error_body)):
            _, body = call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert body == error_body

    def test_non_json_request_body_does_not_crash_the_proxy(self, server_url, span_exporter):
        """The proxy must remain stable when it cannot parse the request body as JSON."""
        upstream = {"id": "msg_1", "usage": {"input_tokens": 1, "output_tokens": 1}}
        parsed = urllib.parse.urlparse(server_url)
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            conn.request("POST", "/v1/messages", body=b"not valid json at all",
                         headers={"Content-Type": "text/plain"})
            resp = conn.getresponse()

        assert resp.status == 200


# ── Trace behavior ─────────────────────────────────────────────────────────────


class TestTracing:
    def test_one_span_is_produced_per_request(self, server_url, span_exporter):
        span_exporter.clear()
        upstream = {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 5}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert len(span_exporter.get_finished_spans()) == 1

    def test_span_records_the_model_name(self, server_url, span_exporter):
        span_exporter.clear()
        upstream = {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 5}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            call_proxy(server_url, {"model": "claude-opus-4-6", "messages": []})

        span = span_exporter.get_finished_spans()[0]
        assert span.attributes["llm.model"] == "claude-opus-4-6"

    def test_span_records_token_counts_from_the_response(self, server_url, span_exporter):
        span_exporter.clear()
        upstream = {"id": "msg_1", "usage": {"input_tokens": 42, "output_tokens": 17}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        span = span_exporter.get_finished_spans()[0]
        assert span.attributes["llm.input_tokens"] == 42
        assert span.attributes["llm.output_tokens"] == 17

    def test_span_records_the_system_prompt(self, server_url, span_exporter):
        span_exporter.clear()
        upstream = {"id": "msg_1", "usage": {"input_tokens": 20, "output_tokens": 10}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            call_proxy(server_url, {
                "model": "claude-sonnet-4-5",
                "system": "You are a concise assistant.",
                "messages": [],
            })

        span = span_exporter.get_finished_spans()[0]
        assert "You are a concise assistant." in span.attributes.get("llm.system_prompt", "")

    def test_span_is_produced_even_when_upstream_returns_an_error(self, server_url, span_exporter):
        span_exporter.clear()
        with patch("urllib.request.urlopen", side_effect=make_upstream_error(500)):
            call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert len(span_exporter.get_finished_spans()) == 1


# ── Metric behavior ────────────────────────────────────────────────────────────


class TestMetrics:
    def test_request_counter_increments_on_success(self, server_url, span_exporter):
        upstream = {"id": "msg_1", "usage": {"input_tokens": 5, "output_tokens": 3}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            with patch.object(proxy.request_counter, "add") as mock_add:
                call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        mock_add.assert_called_once()
        assert mock_add.call_args[0][0] == 1

    def test_request_counter_increments_on_upstream_error(self, server_url, span_exporter):
        with patch("urllib.request.urlopen", side_effect=make_upstream_error(503)):
            with patch.object(proxy.request_counter, "add") as mock_add:
                call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        mock_add.assert_called_once()
        assert mock_add.call_args[0][0] == 1

    def test_token_counter_records_input_and_output_tokens_separately(self, server_url, span_exporter):
        upstream = {"id": "msg_1", "usage": {"input_tokens": 100, "output_tokens": 50}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            with patch.object(proxy.token_counter, "add") as mock_add:
                call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        assert mock_add.call_count == 2
        recorded_values = {c[0][0] for c in mock_add.call_args_list}
        assert 100 in recorded_values
        assert 50 in recorded_values

    def test_latency_is_recorded_for_every_request(self, server_url, span_exporter):
        upstream = {"id": "msg_1", "usage": {"input_tokens": 5, "output_tokens": 3}}
        with patch("urllib.request.urlopen", return_value=make_upstream_response(upstream)):
            with patch.object(proxy.latency_histogram, "record") as mock_record:
                call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        mock_record.assert_called_once()
        latency_ms = mock_record.call_args[0][0]
        assert latency_ms >= 0

    def test_latency_is_recorded_even_when_upstream_errors(self, server_url, span_exporter):
        with patch("urllib.request.urlopen", side_effect=make_upstream_error(500)):
            with patch.object(proxy.latency_histogram, "record") as mock_record:
                call_proxy(server_url, {"model": "claude-sonnet-4-5", "messages": []})

        mock_record.assert_called_once()
