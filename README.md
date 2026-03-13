# Claude Code ROI Measurement Guide

A comprehensive guide to measuring the return on investment for Claude Code implementation in your development organization.

## Overview

This repository contains a complete walkthrough for setting up telemetry, measuring costs, tracking productivity, and calculating ROI for Claude Code usage. Whether you're an individual developer or managing a large engineering team, this guide provides the tools and metrics needed to make data-driven decisions about AI coding assistance.

## What's Included

- **Telemetry Setup**: Complete Prometheus and OpenTelemetry configuration
- **Cost Analysis**: Real usage patterns and pricing breakdowns across different plans
- **Productivity Metrics**: Key indicators for measuring developer efficiency
- **ROI Calculations**: Framework for calculating return on investment
- **Automated Reporting**: Integration with Linear for comprehensive productivity reports

## Key Metrics Tracked

- **Cost Metrics**: Total spend, cost per session, cost by model
- **Token Usage**: Input/output tokens, cache efficiency
- **Productivity**: PR count, commit frequency, session duration
- **Team Analytics**: Usage by developer, adoption rates

## Contents

- [`claude_code_roi_full.md`](claude_code_roi_full.md) - Complete implementation guide
- [`docker-compose.yml`](docker-compose.yml), [`prometheus.yml`](prometheus.yml), [`otel-collector-config.yaml`](otel-collector-config.yaml) - Docker Compose and metrics collection setup
- [`proxy.py`](proxy.py) - Logging proxy for capturing Claude Code API traffic
- [`tempo.yaml`](tempo.yaml) - Grafana Tempo configuration for distributed tracing
- [`sample-report-output.md`](sample-report-output.md) - Example automated reports
- [`report-generation-prompt.md`](report-generation-prompt.md) - Prompt template for generating productivity reports

## Getting Started

Read the complete guide in [`claude_code_roi_full.md`](claude_code_roi_full.md) for detailed setup instructions, real-world examples, and actionable insights for your organization.

---

## Logging Proxy

The observability stack includes a local HTTP proxy that sits between the Claude Code CLI and the Anthropic API. It captures every request and response and emits structured telemetry to the OpenTelemetry collector.

### What it does

- **Intercepts** all Claude Code API traffic by acting as a forwarding proxy to `api.anthropic.com`
- **Emits traces** (spans per request with system prompt, messages, token counts) → Grafana Tempo
- **Emits logs** (system prompt, each message role, response) → Loki, viewable in Grafana
- **Emits metrics** (request count, input/output tokens, latency) → Prometheus, viewable in Grafana

### Architecture

```
Claude Code CLI
      │
      │  ANTHROPIC_BASE_URL=http://localhost:8888
      ▼
  proxy.py :8888
      │
      ├── OTLP → OTel Collector :4318
      │               ├── traces  → Grafana Tempo
      │               ├── logs    → Loki
      │               └── metrics → Prometheus
      │
      └── forward → api.anthropic.com
```

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [uv](https://github.com/astral-sh/uv)

### Running the stack

**1. Start the observability stack:**

```bash
docker compose up -d
```

This starts the OTel collector, Prometheus, Loki, Grafana Tempo, and Grafana.

**2. Install proxy dependencies:**

```bash
uv sync
```

**3. Start the proxy:**

```bash
uv run proxy.py
```

**4. Run Claude Code through the proxy:**

```bash
ANTHROPIC_BASE_URL=http://localhost:8888 claude
```

Add shell functions to your `~/.zshrc` to toggle the proxy on/off without remembering the env var:

```bash
proxy-on()     { export ANTHROPIC_BASE_URL=http://localhost:8888; echo "Proxy enabled"; }
proxy-off()    { unset ANTHROPIC_BASE_URL; echo "Proxy disabled"; }
proxy-status() { [ -n "$ANTHROPIC_BASE_URL" ] && echo "Proxy ON → $ANTHROPIC_BASE_URL" || echo "Proxy OFF"; }
```

### Viewing telemetry in Grafana

Open [http://localhost:3000](http://localhost:3000) (user: `admin`, password: `admin`).

| What | Where |
|------|-------|
| Traces (per-request spans) | Explore → Tempo |
| Logs (prompts, responses) | Explore → Loki |
| Metrics (tokens, latency) | Explore → Prometheus, or build a dashboard |

Tempo is wired to Loki — clicking a span in Tempo lets you jump directly to the correlated log lines for that request.

## Contributing

This guide is based on real-world implementation experience. If you have additional insights or improvements, please feel free to create an issue / PR.

This guide was written by [Kashyap Coimbatore Murali](https://www.linkedin.com/in/kashyap-murali/)
