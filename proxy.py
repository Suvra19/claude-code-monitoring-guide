#!/usr/bin/env python3
"""
Logging proxy for Claude Code.
Forwards requests to api.anthropic.com and emits telemetry to an OTel collector.

Usage:
    uv sync
    uv run proxy.py

Then in another terminal:
    ANTHROPIC_BASE_URL=http://localhost:8888 claude
"""

import json
import logging
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

UPSTREAM = "https://api.anthropic.com"
OTEL_ENDPOINT = "http://localhost:4318"

resource = Resource.create({"service.name": "claude-code-proxy"})

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("claude-code-proxy")

# Metrics
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics")
        )
    ],
)
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("claude-code-proxy")

request_counter = meter.create_counter(
    "llm.requests", description="Total LLM requests"
)
token_counter = meter.create_counter(
    "llm.tokens", description="Token usage by type"
)
latency_histogram = meter.create_histogram(
    "llm.request.duration_ms", description="Request latency in milliseconds"
)

# Logs
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{OTEL_ENDPOINT}/v1/logs"))
)
set_logger_provider(logger_provider)
logging.getLogger().addHandler(
    LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
)
logging.getLogger().setLevel(logging.DEBUG)
logger = logging.getLogger("claude-code-proxy")


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


class LoggingProxy(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}

        model = parsed.get("model", "unknown")
        attrs = {"llm.model": model}

        with tracer.start_as_current_span("llm.request") as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.path", self.path)

            if "system" in parsed:
                system_text = extract_text(parsed["system"])
                span.set_attribute("llm.system_prompt", system_text[:1000])
                logger.info(
                    "llm.system_prompt",
                    extra={"llm.model": model, "llm.system": system_text[:4000]},
                )

            for msg in parsed.get("messages", []):
                role = msg.get("role", "unknown")
                text = extract_text(msg.get("content", ""))
                span.set_attribute(f"llm.{role}", text[:1000])
                logger.info(
                    f"llm.message.{role}",
                    extra={"llm.role": role, "llm.content": text[:4000], "llm.model": model},
                )

            # Forward to Anthropic
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length")
            }
            req = urllib.request.Request(
                UPSTREAM + self.path, data=body, headers=headers, method="POST"
            )

            start = time.time()
            try:
                with urllib.request.urlopen(req) as resp:
                    resp_body = resp.read()
                    latency_ms = (time.time() - start) * 1000

                    try:
                        usage = json.loads(resp_body).get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        token_counter.add(input_tokens, {**attrs, "llm.token_type": "input"})
                        token_counter.add(output_tokens, {**attrs, "llm.token_type": "output"})
                        span.set_attribute("llm.input_tokens", input_tokens)
                        span.set_attribute("llm.output_tokens", output_tokens)
                        logger.info(
                            "llm.response",
                            extra={
                                "llm.model": model,
                                "llm.input_tokens": input_tokens,
                                "llm.output_tokens": output_tokens,
                            },
                        )
                    except Exception:
                        pass

                    latency_histogram.record(latency_ms, attrs)
                    request_counter.add(1, {**attrs, "status": "success"})

                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp_body)

            except urllib.error.HTTPError as e:
                latency_ms = (time.time() - start) * 1000
                latency_histogram.record(latency_ms, attrs)
                request_counter.add(1, {**attrs, "status": str(e.code)})
                self.send_response(e.code)
                self.end_headers()
                self.wfile.write(e.read())

    def log_message(self, *args):
        pass  # suppress default access log


if __name__ == "__main__":
    server = HTTPServer(("localhost", 8888), LoggingProxy)
    print(f"Proxy listening on http://localhost:8888")
    print(f"Sending telemetry to {OTEL_ENDPOINT}")
    server.serve_forever()
