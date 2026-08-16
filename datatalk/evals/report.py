"""Rendering a run: the JSON record and the human summary.

Two audiences, one source. The JSON is what a CI job diffs between commits and
what a later analysis reads, so it carries every per-case field including the
failure reason -- an accuracy number with no way to ask "which ones, and why"
is a number nobody acts on. The Markdown is what a person reads afterwards.

Both always print the denominators. "83%" with the case count hidden is the
shape of every eval result that turned out to be twelve cases and a rounding
error.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from datatalk.evals.runner import ArmResult, RunResult


def to_dict(run: RunResult) -> dict[str, Any]:
    return {
        "suite": run.suite,
        "task": run.task,
        "model": run.model,
        "started_at": run.started_at,
        "duration_s": round(run.duration_s, 1),
        "fixture_fingerprint": run.fixture_fingerprint,
        "arms": [
            {
                "name": arm.name,
                "with_context_model": arm.with_context_model,
                "attempts": arm.attempts,
                "cases": len({r.case_id for r in arm.results}),
                "execution_accuracy": round(arm.execution_accuracy, 4),
                "routing_accuracy": round(arm.routing_accuracy, 4),
                "answer_fidelity": (
                    round(arm.answer_fidelity, 4)
                    if arm.answer_fidelity is not None
                    else None
                ),
                "answer_scored": arm.answer_scored,
                "consistency": (
                    round(arm.consistency, 4) if arm.consistency is not None else None
                ),
                "error_rate": round(arm.error_rate, 4),
                "rejected_queries": arm.rejected_queries,
                "mean_steps": round(arm.mean_steps, 2),
                "mean_queries": round(arm.mean_queries, 2),
                "mean_failed_queries": round(arm.mean_failed_queries, 2),
                "mean_tokens": round(arm.mean_tokens, 0),
                "total_tokens": arm.total_tokens,
                "latency_p50_s": round(arm.latency(0.5), 1),
                "latency_p95_s": round(arm.latency(0.95), 1),
                "by_tag": {k: list(v) for k, v in arm.by_tag().items()},
                "results": [asdict(r) for r in arm.results],
            }
            for arm in run.arms
        ],
    }


def to_json(run: RunResult, *, indent: int = 2) -> str:
    return json.dumps(to_dict(run), indent=indent, default=str)


def _pct(value: float | None) -> str:
    """``None`` renders as an explicit "not applicable", never as 0%.

    A metric that did not apply and a metric that scored zero are different
    findings, and collapsing them is how a suite ends up reporting a permanent
    0% that everyone learns to ignore.
    """
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _headline_table(run: RunResult) -> list[str]:
    rows = [
        "| Metric | " + " | ".join(a.name for a in run.arms) + " |",
        "|---|" + "---|" * len(run.arms),
    ]

    def row(label: str, fn) -> str:
        return f"| {label} | " + " | ".join(fn(a) for a in run.arms) + " |"

    rows.append(row("**Execution accuracy**", lambda a: _pct(a.execution_accuracy)))
    rows.append(
        row(
            "&nbsp;&nbsp;passed / attempted",
            lambda a: f"{sum(1 for r in a.results if r.passed)} / {a.attempts}",
        )
    )
    rows.append(row("Source routing", lambda a: _pct(a.routing_accuracy)))
    rows.append(
        row(
            "Answer fidelity",
            lambda a: _pct(a.answer_fidelity)
            + (f" ({a.answer_scored})" if a.answer_fidelity is not None else ""),
        )
    )
    rows.append(row("Consistency", lambda a: _pct(a.consistency)))
    rows.append(row("Run errors", lambda a: _pct(a.error_rate)))
    rows.append(row("Rejected (unsafe) SQL", lambda a: str(a.rejected_queries)))
    rows.append(row("Mean steps", lambda a: f"{a.mean_steps:.1f}"))
    rows.append(row("Mean queries", lambda a: f"{a.mean_queries:.1f}"))
    rows.append(row("Mean failed queries", lambda a: f"{a.mean_failed_queries:.1f}"))
    rows.append(row("Mean tokens / case", lambda a: f"{a.mean_tokens:,.0f}"))
    rows.append(row("Latency p50 / p95", lambda a: f"{a.latency(0.5):.0f}s / {a.latency(0.95):.0f}s"))
    return rows


def _tag_table(run: RunResult) -> list[str]:
    tags = sorted({t for arm in run.arms for t in arm.by_tag()})
    if not tags:
        return []
    lines = [
        "| Tag | " + " | ".join(a.name for a in run.arms) + " |",
        "|---|" + "---|" * len(run.arms),
    ]
    for tag in tags:
        cells = []
        for arm in run.arms:
            passed, total = arm.by_tag().get(tag, (0, 0))
            cells.append(f"{passed}/{total}" if total else "—")
        lines.append(f"| `{tag}` | " + " | ".join(cells) + " |")
    return lines


def _failure_lines(arm: ArmResult, limit: int = 20) -> list[str]:
    failures = [r for r in arm.results if not r.passed]
    if not failures:
        return [f"No failures in **{arm.name}**."]
    lines = [f"**{arm.name}** — {len(failures)} failing attempt(s):", ""]
    for r in failures[:limit]:
        detail = r.error or r.reason or "no reason recorded"
        routing = "" if r.routed_ok else (
            f" [routed to {', '.join(r.sources_queried) or 'nothing'}, "
            f"expected {', '.join(r.sources_expected)}]"
        )
        lines.append(f"- `{r.case_id}`{routing} — {detail}")
    if len(failures) > limit:
        lines.append(f"- …and {len(failures) - limit} more (see the JSON).")
    return lines


def to_markdown(run: RunResult) -> str:
    lines = [
        f"# DataTalk eval — `{run.suite}` / `{run.task}`",
        "",
        f"- Model: `{run.model}`",
        f"- Started: {run.started_at}  ·  Wall clock: {run.duration_s:.0f}s",
        f"- Fixture: `{run.fixture_fingerprint[:12]}…`",
        "",
        "## Results",
        "",
        *_headline_table(run),
        "",
    ]

    with_ctx = next((a for a in run.arms if a.with_context_model), None)
    without_ctx = next((a for a in run.arms if not a.with_context_model), None)
    if with_ctx and without_ctx and with_ctx.attempts and without_ctx.attempts:
        delta = with_ctx.execution_accuracy - without_ctx.execution_accuracy
        lines += [
            f"**Context model: {_pct(without_ctx.execution_accuracy)} → "
            f"{_pct(with_ctx.execution_accuracy)}** "
            f"({delta * 100:+.1f} points) on identical data, prompts and model.",
            "",
        ]

    tag_table = _tag_table(run)
    if tag_table:
        lines += ["## By question type", "", *tag_table, ""]

    lines += ["## Failures", ""]
    for arm in run.arms:
        lines += _failure_lines(arm)
        lines.append("")

    rejections = [
        (arm.name, r)
        for arm in run.arms
        for r in arm.results
        if r.rejected_queries
    ]
    if rejections:
        lines += [
            "## Statements the guardrails refused",
            "",
            "Not a scoring problem — the executor is doing its job. Worth "
            "reading anyway: each one is a statement the agent believed was a "
            "reasonable way to answer the question.",
            "",
        ]
        for arm_name, r in rejections:
            for message in r.query_errors:
                if message.startswith("Rejected SQL"):
                    lines.append(f"- `{r.case_id}` [{arm_name}] — {message}")
        lines.append("")

    lines += [
        "## How to read this",
        "",
        "- **Execution accuracy** — a dataset the agent captured contains the "
        "reference answer. Column names and ordering are forgiven; row counts "
        "and values are not.",
        "- **Source routing** — the agent queried exactly the sources the "
        "question needs. Querying a source that cannot contribute is a failure, "
        "not a harmless extra.",
        "- **Answer fidelity** — among the runs that fetched the right rows, "
        "the prose carried the right numbers. `n/a` for the `analyst` task, "
        "which is told to stop without restating anything.",
        "- **Mean queries** — read alongside accuracy. An agent that captures "
        "everything will eventually capture the answer.",
    ]
    return "\n".join(lines)


def summary_line(run: RunResult) -> str:
    """One line for a CI log or a commit comment."""
    parts = [f"{run.suite}/{run.task} on {run.model}"]
    for arm in run.arms:
        passed = sum(1 for r in arm.results if r.passed)
        parts.append(f"{arm.name} {_pct(arm.execution_accuracy)} ({passed}/{arm.attempts})")
    return " · ".join(parts)
