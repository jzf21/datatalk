"""Run the suite through the production agents and score what comes back.

Two rules shape this module.

**The agents are not modified and the prompts are not replaced.** ``gather_data``
and ``generate_report`` are called exactly as ``web/app.py`` calls them, against
a ``TenantContext`` built by the same constructor a request uses. A harness that
supplies its own system prompt measures a prompt nobody ships, and its number
would move every time the real prompt changed without anyone noticing.

**Every arm of a run differs in exactly one variable.** The default experiment
is the context model on versus off, which is the claim this product is built
around: that curated meaning, not more schema, is what an agent is missing. Both
arms see the same fixture, the same catalog, the same model at temperature 0, so
the difference between their accuracies is attributable to the one thing that
changed.

What is measured, and why they are kept apart rather than averaged into a single
score:

- **execution accuracy** -- did a captured dataset contain the right answer.
  The headline.
- **routing accuracy** -- did it query exactly the sources the question needs.
  Only meaningful because the fixture is split across two.
- **answer fidelity** -- did the right numbers survive into the prose. A
  separate failure from fetching the wrong rows, and the one ``materialize()``
  exists to prevent.
- **cost**: steps, queries, failed queries, tokens, wall clock. Execution
  accuracy alone would reward an agent that captures forty datasets hoping one
  lands; reported alongside the query count, it cannot.
"""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from datatalk.agent.analyst import gather_data
from datatalk.agent.executor import run_sql
from datatalk.agent.planner import Section
from datatalk.context import TenantContext
from datatalk.evals import scoring
from datatalk.evals.cases import Case, Suite, combine_steps
from datatalk.evals.scoring import Relation
from datatalk.warehouse.catalog import build_catalog

Log = Callable[[str], None]


def _noop(message: str) -> None:  # pragma: no cover - default sink
    pass


# --- token accounting ---------------------------------------------------------


class CountingOpenAI:
    """A transparent proxy that tallies token usage.

    Installed through ``TenantContext.openai_override``, the injection point the
    agent tests already use, so no agent knows it exists. Counting at the client
    is the only place that sees *every* call -- planner, analyst, reporter and
    the finalize turn -- which is what makes a cost-per-question figure honest
    rather than an estimate of the parts someone remembered to instrument.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.chat = _CountingChat(self)
        self.embeddings = getattr(inner, "embeddings", None)

    def _record(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.calls += 1
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class _CountingChat:
    def __init__(self, owner: CountingOpenAI) -> None:
        self.completions = _CountingCompletions(owner)


class _CountingCompletions:
    def __init__(self, owner: CountingOpenAI) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        response = self._owner._inner.chat.completions.create(**kwargs)
        self._owner._record(response)
        return response


# --- results ------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    question: str
    tags: tuple[str, ...]
    attempt: int = 0
    passed: bool = False
    dataset_id: str = ""
    reason: str = ""
    routed_ok: bool = True
    sources_queried: tuple[str, ...] = ()
    sources_expected: tuple[str, ...] = ()
    answer_ok: bool | None = None
    answer_missing: tuple[str, ...] = ()
    steps: int = 0
    query_count: int = 0
    failed_queries: int = 0
    rejected_queries: int = 0
    query_errors: tuple[str, ...] = ()
    context_reads: int = 0
    duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


@dataclass
class ArmResult:
    """One configuration of the run -- e.g. "with context" or "without"."""

    name: str
    with_context_model: bool
    results: list[CaseResult] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.results)

    @property
    def execution_accuracy(self) -> float:
        return _rate(sum(1 for r in self.results if r.passed), self.attempts)

    @property
    def routing_accuracy(self) -> float:
        scored = [r for r in self.results if r.sources_expected]
        return _rate(sum(1 for r in scored if r.routed_ok), len(scored))

    @property
    def answer_scored(self) -> int:
        return sum(1 for r in self.results if r.passed and r.answer_ok is not None)

    @property
    def answer_fidelity(self) -> float | None:
        """Over the cases that *passed* execution match only.

        An agent that fetched the wrong rows and then narrated them faithfully
        has not demonstrated fidelity, and counting it here would let a collapse
        in accuracy read as an improvement in honesty.

        ``None`` when nothing was scored -- which is the normal state for the
        ``analyst`` task, whose prompt ends "reply with a single short line such
        as 'Data gathering complete.'". Reporting 0% there would be measuring
        the Analyst for obeying its instructions.
        """
        scored = [r for r in self.results if r.passed and r.answer_ok is not None]
        if not scored:
            return None
        return _rate(sum(1 for r in scored if r.answer_ok), len(scored))

    @property
    def error_rate(self) -> float:
        return _rate(sum(1 for r in self.results if r.error), self.attempts)

    @property
    def consistency(self) -> float | None:
        """Fraction of cases whose repeated attempts all agreed.

        ``None`` without ``--repeat``: a single attempt agrees with itself, and
        rendering that as 100% would advertise a stability this suite has not
        measured. Worth measuring because temperature 0 is not determinism --
        MoE routing, provider-side batching and tool-call ordering all still
        move -- and a suite whose own number wobbles by five points between runs
        cannot detect a five-point regression.
        """
        by_case: dict[str, list[bool]] = {}
        for r in self.results:
            by_case.setdefault(r.case_id, []).append(r.passed)
        if not by_case or max(len(v) for v in by_case.values()) < 2:
            return None
        agreed = sum(1 for outcomes in by_case.values() if len(set(outcomes)) == 1)
        return _rate(agreed, len(by_case))

    def _mean(self, attr: str) -> float:
        values = [getattr(r, attr) for r in self.results]
        return statistics.fmean(values) if values else 0.0

    @property
    def mean_steps(self) -> float:
        return self._mean("steps")

    @property
    def mean_queries(self) -> float:
        return self._mean("query_count")

    @property
    def mean_failed_queries(self) -> float:
        return self._mean("failed_queries")

    @property
    def rejected_queries(self) -> int:
        """Statements the read-only guardrails refused, across the whole arm.

        Expected to be zero. A non-zero count is not a scoring problem, it is a
        finding: the agent tried to write.
        """
        return sum(r.rejected_queries for r in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.results)

    @property
    def mean_tokens(self) -> float:
        return self._mean("total_tokens")

    def latency(self, quantile: float) -> float:
        values = sorted(r.duration_s for r in self.results)
        if not values:
            return 0.0
        index = min(len(values) - 1, int(round(quantile * (len(values) - 1))))
        return values[index]

    def by_tag(self) -> dict[str, tuple[int, int]]:
        """tag -> (passed, total). Where a suite-wide number hides the shape."""
        out: dict[str, list[int]] = {}
        for r in self.results:
            for tag in r.tags:
                bucket = out.setdefault(tag, [0, 0])
                bucket[0] += int(r.passed)
                bucket[1] += 1
        return {k: (v[0], v[1]) for k, v in sorted(out.items())}


@dataclass
class RunResult:
    suite: str
    task: str
    model: str
    started_at: str
    fixture_fingerprint: str
    arms: list[ArmResult] = field(default_factory=list)
    duration_s: float = 0.0

    def arm(self, name: str) -> ArmResult | None:
        return next((a for a in self.arms if a.name == name), None)


# --- expectations -------------------------------------------------------------


def compute_expected(case: Case, ctx: TenantContext) -> Relation:
    """Execute the case's reference queries and return the right answer.

    Run through ``executor.run_sql`` rather than a raw driver, so the reference
    is subject to the same read-only guardrails and row caps as the agent. A
    reference query that the product would refuse to run is not a fair standard
    to hold the product to.
    """
    step_rows: dict[str, tuple[list[str], list[list[Any]]]] = {}
    for step in case.expect.reference:
        engine = ctx.source(step.source).type
        result = run_sql(step.sql_for(engine), ctx=ctx, source=step.source)
        step_rows[step.as_] = (list(result.columns), [list(r) for r in result.rows])

    if not case.expect.combine:
        columns, rows = next(iter(step_rows.values()))
        return Relation(columns=columns, rows=rows)

    columns, rows = combine_steps(step_rows, case.expect.combine)
    return Relation(columns=columns, rows=rows)


# --- one agent run ------------------------------------------------------------


@dataclass
class AgentRun:
    datasets: dict[str, Relation]
    sources: tuple[str, ...]
    text: str
    steps: int
    query_count: int


class _EventCollector:
    """Counts what the run did, from the same events the UI streams.

    Keeps the failure *messages*, not just tallies. "2 rejected statements" with
    nothing attached is a number nobody can act on -- and a rejected statement
    is the one outcome here that might be a real defect rather than a score.
    """

    _MAX_KEPT = 5

    def __init__(self) -> None:
        self.failed = 0
        self.rejected = 0
        self.context_reads = 0
        self.errors: list[str] = []

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "error":
            message = str(data.get("message", ""))
            self.failed += 1
            if message.startswith("Rejected SQL"):
                self.rejected += 1
            if len(self.errors) < self._MAX_KEPT:
                self.errors.append(message)
        elif kind == "status" and str(data.get("message", "")).startswith(
            "Reading context"
        ):
            self.context_reads += 1


def _relations(datasets: dict[str, Any]) -> dict[str, Relation]:
    return {
        dataset_id: Relation(columns=list(r.columns), rows=[list(row) for row in r.rows])
        for dataset_id, r in datasets.items()
    }


def run_analyst_task(
    case: Case,
    ctx: TenantContext,
    schema_context: str,
    collector: _EventCollector,
    *,
    max_steps: int,
) -> AgentRun:
    """The question as a one-section plan, handed to the real Analyst.

    A single section is a legitimate plan -- the Planner emits exactly this
    shape for a narrow request -- and skipping the Planner is deliberate: it
    isolates SQL generation, so a regression in accuracy points at the Analyst
    or the catalog rather than at three agents at once. The ``report`` task
    below measures the whole pipeline for those who want the end-to-end number.
    """
    section = Section(
        id="s1",
        title="Answer the question",
        goal=case.question,
        data_questions=[case.question],
    )
    loop = gather_data(
        case.question,
        [section],
        ctx=ctx,
        schema_context=schema_context,
        on_event=collector,
        max_steps=max_steps,
    )
    return AgentRun(
        datasets=_relations(loop.datasets),
        sources=tuple(q.get("source", "") for q in loop.queries),
        text=loop.final_content,
        steps=loop.steps,
        query_count=len(loop.queries),
    )


def run_report_task(
    case: Case,
    ctx: TenantContext,
    schema_context: str,
    collector: _EventCollector,
    *,
    max_steps: int,
) -> AgentRun:
    """The full Planner -> Analyst -> Reporter pipeline.

    ``ReportResult`` carries the materialized document and the query *log*, not
    the rows -- so the datasets are recovered by re-executing the recorded SQL,
    which is precisely what ``dashboards/refresh.py`` does to refresh a live
    dashboard. Deterministic fixture, same statement, same rows; and reusing the
    product's own mechanism keeps the scorer from inventing a second way to get
    data out of a run.
    """
    from datatalk.agent.blocks import document_to_text
    from datatalk.agent.report import generate_report

    result = generate_report(
        case.question, ctx=ctx, on_event=collector, max_steps=max_steps
    )
    datasets: dict[str, Relation] = {}
    for entry in result.queries:
        dataset_id = entry.get("dataset_id") or f"q{len(datasets) + 1}"
        try:
            replayed = run_sql(entry["sql"], ctx=ctx, source=entry.get("source") or None)
        except Exception:  # noqa: BLE001 - a dead query drops out, like a refresh
            continue
        datasets[dataset_id] = Relation(
            columns=list(replayed.columns), rows=[list(r) for r in replayed.rows]
        )
    return AgentRun(
        datasets=datasets,
        sources=tuple(q.get("source", "") for q in result.queries),
        text=document_to_text(result.document),
        steps=result.steps,
        query_count=len(result.queries),
    )


TASKS: dict[str, Callable[..., AgentRun]] = {
    "analyst": run_analyst_task,
    "report": run_report_task,
}


# --- scoring one case ---------------------------------------------------------

# Above this many numbers in the expected answer, "did the prose repeat them
# all" stops being a fidelity check and becomes a formatting complaint: a
# twenty-row table is summarized in prose, not transcribed.
_MAX_ANSWER_VALUES = 8

# Tasks that produce a written deliverable, and so can be asked whether the
# numbers survived into it. The Analyst is not one of them: its prompt tells it
# to stop and say "Data gathering complete", and the whole authoring-block
# design exists so that no agent types a number at all -- values are filled in
# by `materialize()` from the captured datasets. Scoring the Analyst on prose
# fidelity would penalise it for following the rule the product is built on.
_TASKS_WITH_PROSE = {"report"}


def _expected_numbers(expected: Relation) -> list[str]:
    values: list[str] = []
    for row in expected.rows:
        for cell in row:
            normalized = scoring.normalize_value(cell)
            if normalized is not None and normalized[0] == "n":
                values.append(f"{normalized[1]:.2f}".rstrip("0").rstrip("."))
    return values


def score_case(
    case: Case,
    expected: Relation,
    run: AgentRun,
    collector: _EventCollector,
    *,
    attempt: int = 0,
    duration_s: float = 0.0,
    check_answer: bool = False,
) -> CaseResult:
    outcome = scoring.execution_match(
        expected,
        run.datasets,
        match=case.expect.match,
        tolerance=case.expect.tolerance,
        order_sensitive=case.expect.order_sensitive,
    )
    queried = tuple(s for s in run.sources if s)
    result = CaseResult(
        case_id=case.id,
        question=case.question,
        tags=case.tags,
        attempt=attempt,
        passed=outcome.matched,
        dataset_id=outcome.dataset_id,
        reason=outcome.reason,
        sources_queried=tuple(sorted(set(queried))),
        sources_expected=case.expect.sources,
        routed_ok=scoring.routing_score(queried, case.expect.sources),
        steps=run.steps,
        query_count=run.query_count,
        failed_queries=collector.failed,
        rejected_queries=collector.rejected,
        query_errors=tuple(collector.errors),
        context_reads=collector.context_reads,
        duration_s=duration_s,
    )

    wanted = list(case.expect.answer_contains) or _expected_numbers(expected)
    if check_answer and wanted and len(wanted) <= _MAX_ANSWER_VALUES:
        check = scoring.answer_contains(
            run.text, wanted, tolerance=case.expect.tolerance
        )
        result.answer_ok = check.passed
        result.answer_missing = tuple(check.missing)
    return result


# --- the run ------------------------------------------------------------------


def run_arm(
    suite: Suite,
    ctx: TenantContext,
    *,
    name: str,
    with_context_model: bool,
    task: str = "analyst",
    max_steps: int = 8,
    repeat: int = 1,
    concurrency: int = 4,
    expected_by_case: dict[str, Relation] | None = None,
    log: Log = _noop,
) -> ArmResult:
    """Run every case in one configuration.

    Cases run concurrently because they are almost entirely network wait, and a
    twenty-case suite serialized against a slow endpoint takes long enough that
    nobody runs it. They cannot interfere: each gets its own counting client and
    its own event collector, and the fixture is read-only for the duration.
    """
    task_fn = TASKS[task]
    arm = ArmResult(name=name, with_context_model=with_context_model)
    expected_by_case = expected_by_case or {}

    schema_context = build_catalog(ctx)

    def one(case: Case, attempt: int) -> CaseResult:
        # A per-case client so concurrent cases cannot pool their token counts.
        # `replace` on the frozen context is the same move `with_sources` makes,
        # and it keeps every other field -- sources, settings, context model --
        # identical to the arm's.
        counter = CountingOpenAI(ctx.openai)
        run_ctx = replace(ctx, openai_override=counter)
        collector = _EventCollector()
        started = time.monotonic()
        try:
            agent_run = task_fn(
                case, run_ctx, schema_context, collector, max_steps=max_steps
            )
        except Exception as exc:  # noqa: BLE001 - one bad case must not void the run
            log(f"  {case.id} [{name}] ERROR {exc}")
            return CaseResult(
                case_id=case.id,
                question=case.question,
                tags=case.tags,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - started,
                prompt_tokens=counter.prompt_tokens,
                completion_tokens=counter.completion_tokens,
            )
        duration = time.monotonic() - started
        result = score_case(
            case,
            expected_by_case[case.id],
            agent_run,
            collector,
            attempt=attempt,
            duration_s=duration,
            check_answer=task in _TASKS_WITH_PROSE,
        )
        result.prompt_tokens = counter.prompt_tokens
        result.completion_tokens = counter.completion_tokens
        log(
            f"  {case.id} [{name}] {'PASS' if result.passed else 'FAIL'} "
            f"({duration:.1f}s, {result.query_count}q, {result.total_tokens} tok)"
            + (f" — {result.reason[:120]}" if not result.passed else "")
        )
        return result

    jobs = [(case, attempt) for attempt in range(repeat) for case in suite]
    if concurrency > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(jobs)), thread_name_prefix="eval"
        ) as pool:
            arm.results = list(pool.map(lambda j: one(*j), jobs))
    else:
        arm.results = [one(*j) for j in jobs]
    return arm


def run_suite(
    suite: Suite,
    *,
    contexts: Sequence[tuple[str, bool, TenantContext]],
    task: str = "analyst",
    max_steps: int = 8,
    repeat: int = 1,
    concurrency: int = 4,
    log: Log = _noop,
) -> RunResult:
    """Run every arm, sharing one set of computed expectations.

    Expectations are computed once, before any inference: they depend only on
    the fixture, and recomputing them per arm would let a warehouse hiccup make
    two arms disagree about the right answer -- which would look exactly like
    the effect being measured.
    """
    from datatalk.evals.dataset import FINGERPRINT

    started = time.monotonic()
    reference_ctx = contexts[0][2]

    log(f"computing expectations for {len(suite)} cases…")
    expected_by_case = {
        case.id: compute_expected(case, reference_ctx) for case in suite
    }
    for case in suite:
        rel = expected_by_case[case.id]
        log(f"  {case.id}: {rel.shape[0]} rows x {rel.shape[1]} cols")

    run = RunResult(
        suite=suite.name,
        task=task,
        model=reference_ctx.model,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fixture_fingerprint=FINGERPRINT,
    )
    for name, with_context, ctx in contexts:
        log(f"\narm: {name}")
        run.arms.append(
            run_arm(
                suite,
                ctx,
                name=name,
                with_context_model=with_context,
                task=task,
                max_steps=max_steps,
                repeat=repeat,
                concurrency=concurrency,
                expected_by_case=expected_by_case,
                log=log,
            )
        )
    run.duration_s = time.monotonic() - started
    return run


def validate_suite(
    suite: Suite, ctx: TenantContext, *, log: Log = _noop
) -> list[tuple[Case, Relation | None, str]]:
    """Execute every reference query and report what it returns. No inference.

    The cheap check that has to pass before anyone spends money: a reference
    query that errors, or returns nothing, or returns four hundred rows, is a
    broken case -- and finding that out during a paid run costs the whole run.
    """
    out: list[tuple[Case, Relation | None, str]] = []
    for case in suite:
        try:
            expected = compute_expected(case, ctx)
        except Exception as exc:  # noqa: BLE001 - report every broken case, not the first
            log(f"  {case.id}: ERROR {exc}")
            out.append((case, None, f"{type(exc).__name__}: {exc}"))
            continue
        problem = ""
        if not expected.rows:
            problem = "reference returns no rows"
        elif len(expected.rows) > 100:
            problem = (
                f"reference returns {len(expected.rows)} rows; keep expectations "
                "small enough that the row cap cannot truncate a candidate"
            )
        elif case.expect.match == "scalar" and expected.shape != (1, 1):
            problem = f"scalar case, but the reference returns {expected.shape}"
        log(
            f"  {case.id}: {expected.shape[0]} rows x {expected.shape[1]} cols"
            + (f"  !! {problem}" if problem else "")
        )
        out.append((case, expected, problem))
    return out
