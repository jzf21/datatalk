"""``datatalk-eval`` — seed the fixture, validate the suite, run the agents.

Three subcommands, in the order you use them:

    datatalk-eval seed              # load the fixture into the eval warehouses
    datatalk-eval validate          # execute every reference query, no inference
    datatalk-eval run               # the real thing, with/without context model

``validate`` exists because ``run`` costs money. A reference query that errors
or returns four hundred rows is a broken case, and the cheap way to find that
out is before the paid part, not during it.

This is a CLI and not a pytest module on purpose: the suite makes live model
calls against whatever endpoint ``.env`` names, and a test suite that silently
spends money -- or fails because someone's key is missing -- is a test suite
people learn to skip. What pytest covers is the scorer and the wiring, against
the same fakes every other agent test uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from datatalk.evals import dataset, report as reporting, seed as seeding
from datatalk.evals.cases import SuiteError, list_suites, load_suite
from datatalk.evals.runner import run_suite, validate_suite
from datatalk.evals.targets import (
    UnsafeTargetError,
    build_eval_context,
    build_targets,
)


def _log(message: str) -> None:
    print(message, flush=True)


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--events-engine",
        choices=("postgres", "clickhouse"),
        default="postgres",
        help=(
            "Engine hosting the events source. 'clickhouse' makes the suite "
            "genuinely cross-engine -- the agent must switch dialect per source "
            "within one question -- and needs the clickhouse compose profile up."
        ),
    )


def cmd_seed(args: argparse.Namespace) -> int:
    targets = build_targets(events_engine=args.events_engine)
    _log(f"fixture: {dataset.describe()}")
    try:
        counts = seeding.seed_all(
            targets, events_engine=args.events_engine, log=_log
        )
    except UnsafeTargetError as exc:
        _log(f"\nrefused: {exc}")
        return 2
    except dataset.FixtureDriftError as exc:
        _log(f"\n{exc}")
        return 2
    total = sum(sum(t.values()) for t in counts.values())
    _log(f"\nseeded {total} rows across {len(counts)} sources.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    targets = build_targets(events_engine=args.events_engine)
    ctx = build_eval_context(targets, with_context_model=True)
    _log(f"validating {len(suite)} cases in suite '{suite.name}'…")
    outcomes = validate_suite(suite, ctx, log=_log)
    broken = [(c, p) for c, _e, p in outcomes if p]
    if broken:
        _log(f"\n{len(broken)} case(s) need attention:")
        for case, problem in broken:
            _log(f"  {case.id}: {problem}")
        return 1
    _log(f"\nall {len(outcomes)} reference queries look sound.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite).filtered(ids=args.case or (), tags=args.tag or ())
    if not len(suite):
        _log("no cases selected.")
        return 1

    targets = build_targets(events_engine=args.events_engine)
    arms = []
    if args.arm in ("both", "with-context"):
        arms.append(
            ("with-context", True, build_eval_context(targets, with_context_model=True))
        )
    if args.arm in ("both", "no-context"):
        arms.append(
            ("no-context", False, build_eval_context(targets, with_context_model=False))
        )

    _log(
        f"suite '{suite.name}': {len(suite)} cases x {args.repeat} attempt(s) "
        f"x {len(arms)} arm(s) = {len(suite) * args.repeat * len(arms)} agent runs"
    )
    _log(f"task={args.task} model={arms[0][2].model} max_steps={args.max_steps}\n")

    run = run_suite(
        suite,
        contexts=arms,
        task=args.task,
        max_steps=args.max_steps,
        repeat=args.repeat,
        concurrency=args.concurrency,
        log=_log,
    )

    markdown = reporting.to_markdown(run)
    _log("\n" + markdown)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(reporting.to_json(run))
        md_path = out.with_suffix(".md")
        md_path.write_text(markdown)
        _log(f"\nwrote {out} and {md_path}")

    # A threshold makes this usable as a merge gate. Without one the command
    # always succeeds, which is the wrong default for something you would put in
    # CI but the right one for something you run while iterating.
    if args.min_accuracy is not None:
        best = max(a.execution_accuracy for a in run.arms)
        if best < args.min_accuracy:
            _log(
                f"\nFAIL: best execution accuracy {best:.1%} is below the "
                f"--min-accuracy threshold of {args.min_accuracy:.1%}"
            )
            return 1
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    _log(f"suite '{suite.name}' — {len(suite)} cases")
    for case in suite:
        tags = " ".join(f"[{t}]" for t in case.tags)
        sources = ",".join(case.expect.sources)
        _log(f"\n  {case.id}  {tags}")
        _log(f"    sources: {sources}  match: {case.expect.match}")
        _log(f"    Q: {case.question}")
        if case.notes:
            _log(f"    note: {case.notes}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datatalk-eval",
        description="Measure how often DataTalk's agents get the right answer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="load the fixture into the eval warehouses")
    _add_target_args(p_seed)
    p_seed.set_defaults(func=cmd_seed)

    p_val = sub.add_parser(
        "validate", help="execute every reference query; makes no model calls"
    )
    p_val.add_argument("--suite", default="retail")
    _add_target_args(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_cases = sub.add_parser("cases", help="list the cases in a suite")
    p_cases.add_argument("--suite", default="retail")
    p_cases.set_defaults(func=cmd_cases)

    p_run = sub.add_parser("run", help="run the suite through the agents")
    p_run.add_argument("--suite", default="retail")
    p_run.add_argument(
        "--task",
        choices=("analyst", "report"),
        default="analyst",
        help=(
            "'analyst' isolates SQL generation behind a one-section plan; "
            "'report' runs the whole Planner/Analyst/Reporter pipeline."
        ),
    )
    p_run.add_argument(
        "--arm",
        choices=("both", "with-context", "no-context"),
        default="both",
        help="which context-model configuration(s) to run",
    )
    p_run.add_argument("--case", action="append", help="run only this case id (repeatable)")
    p_run.add_argument("--tag", action="append", help="run only cases with this tag (repeatable)")
    p_run.add_argument("--repeat", type=int, default=1, help="attempts per case")
    p_run.add_argument("--max-steps", type=int, default=8)
    p_run.add_argument("--concurrency", type=int, default=4)
    p_run.add_argument("--out", help="write results JSON here (and .md alongside)")
    p_run.add_argument(
        "--min-accuracy",
        type=float,
        help="exit non-zero if the best arm scores below this (0-1). For CI.",
    )
    _add_target_args(p_run)
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SuiteError as exc:
        _log(f"suite error: {exc}")
        _log(f"available suites: {', '.join(list_suites())}")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        _log("\ninterrupted.")
        return 130
    finally:
        # The Postgres adapter keeps a connection pool per source, and its
        # worker threads outlive an interpreter that just exits -- psycopg then
        # prints a wall of "couldn't stop thread" after the results. A long run
        # ending in warnings reads as a failed run.
        from datatalk import clients

        clients.close_all()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
