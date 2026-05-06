"""
Central tool registry.

Tools are registered at import time and can be enumerated at runtime.
This is the foundation for exposing tools to an external LLM agent
(e.g., Claude tool-use loop) without hard-coding imports everywhere.
"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    input_keys: list[str]    # required keys the caller must supply
    output_keys: list[str]   # guaranteed keys in ToolResult.data on success


_registry: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    _registry[tool.name] = tool


def get(name: str) -> Tool | None:
    return _registry.get(name)


def list_tools() -> list[str]:
    return list(_registry.keys())


def _register_defaults() -> None:
    """Register all built-in agent tools. Called once at module load."""
    from agent import booking_handler, email_outreach, enrichment_pipeline

    register(Tool(
        name="enrich_company",
        description=(
            "Run all four signal sources (Crunchbase, job posts, Layoffs.fyi, PDL) "
            "and return a classified CompanyProfile."
        ),
        fn=enrichment_pipeline.enrich,
        input_keys=["email"],
        output_keys=["company_name", "segment", "funding_stage", "ai_maturity_score"],
    ))

    register(Tool(
        name="compose_and_send_email",
        description="Compose a segment-aware outreach email and send it via Resend.",
        fn=email_outreach.compose_and_send,
        input_keys=["profile", "trace_id"],
        output_keys=["id"],
    ))

    register(Tool(
        name="book_discovery_call",
        description="Book a Cal.com discovery call for a qualified prospect.",
        fn=booking_handler.book,
        input_keys=["email", "name", "trace_id"],
        output_keys=["booking_url", "slot"],
    ))


_register_defaults()
