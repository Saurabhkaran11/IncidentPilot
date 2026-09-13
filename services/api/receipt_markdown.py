"""Render a recovery receipt as Markdown.

The receipt is the product's central artifact, so its wording is part of
the product, not an afterthought: it must state what was verified, what was
not, and which requests are still outstanding -- without ever implying more
recovery than the checks support.
"""

from __future__ import annotations

from packages.domain.enums import ReceiptOutcome
from packages.domain.models import Receipt

_OUTCOME_HEADLINE = {
    ReceiptOutcome.RECOVERED: "Recovered",
    ReceiptOutcome.PARTIAL: "Service restored, some requests still unresolved",
    ReceiptOutcome.NEEDS_ATTENTION: "Needs attention -- recovery not confirmed",
}


def render_markdown(receipt: Receipt) -> str:
    lines: list[str] = [
        f"# Recovery receipt {receipt.receipt_id}",
        "",
        f"**Outcome:** {_OUTCOME_HEADLINE[receipt.outcome]}",
        f"**Incident:** {receipt.incident_id}",
        f"**Mode:** {receipt.mode.value}"
        + ("  _(fixture data -- not a live AWS run)_" if receipt.mode.value == "fixture" else ""),
        f"**Created:** {receipt.created_at.isoformat()}",
        "",
        "## Approved action",
        "",
        f"- Plan `{receipt.plan_id}` approved as `{receipt.approval_id}`",
        f"- Executed as operation `{receipt.execution_id}`",
        f"- Applied version: **{receipt.applied_version or 'not applied'}**",
        "",
        "## Verification",
        "",
    ]
    if receipt.verification:
        lines += ["| Check | Passed | Detail |", "|---|---|---|"]
        for check in receipt.verification:
            mark = "yes" if check.passed else "NO"
            lines.append(f"| `{check.check}` | {mark} | {check.details or ''} |")
    else:
        lines.append("_No verification checks were recorded._")

    lines += ["", "## Customer requests", ""]
    if receipt.requests:
        lines += ["| Request | Outcome | Result |", "|---|---|---|"]
        for req in receipt.requests:
            lines.append(f"| `{req.request_id}` | {req.outcome.value} | {req.result_id or '—'} |")
    else:
        lines.append("_No customer requests were associated with this incident._")

    if receipt.unresolved_request_ids:
        lines += [
            "",
            "## Still unresolved",
            "",
            "These requests were **not** confirmed recovered and need manual reconciliation:",
            "",
        ]
        lines += [f"- `{rid}`" for rid in receipt.unresolved_request_ids]

    lines += ["", "## Timing", ""]
    lines += [f"- {name}: {value} ms" for name, value in sorted(receipt.timing_ms.items())] or ["_Not measured._"]

    lines += [
        "",
        "## Model usage",
        "",
        f"- Model: `{receipt.usage.model_id}`",
        f"- Input tokens: {receipt.usage.input_tokens if receipt.usage.input_tokens is not None else 'not measured'}",
        f"- Output tokens: {receipt.usage.output_tokens if receipt.usage.output_tokens is not None else 'not measured'}",
        f"- Estimated cost: {f'${receipt.usage.estimated_cost_usd:.4f}' if receipt.usage.estimated_cost_usd is not None else 'not measured'}",
        "",
    ]
    return "\n".join(lines)
