"""The documentation agent's five-pass wiring, on a scripted fake (no live DB/API).

Asserts the shape that matters: the plan drives the file list, entity files are
keyed by business entity rather than by table, and every call goes to the *docs*
model rather than the report loop's.
"""

import json

from datatalk.agent import datacontext as dc
from tests.conftest import FakeOpenAI, FakeWarehouse, fake_table, make_ctx, make_settings
from tests.conftest import fn_call as _fn_call
from tests.conftest import message as _message
from tests.conftest import response as _response


def _scripted():
    plan = json.dumps(
        {
            "entities": [
                {
                    "slug": "customers",
                    "title": "Customers",
                    "tables": [{"source": "main", "table": "db.accounts"}],
                }
            ],
            "playbooks": [{"slug": "churn", "title": "Churn", "why": "retention"}],
        }
    )
    ontology = json.dumps(
        {
            "path": "ontology/customers.md",
            "summary": "Customer identity and lifecycle",
            "body_md": "## What it is\n\nA buyer.",
            "covers": [{"source": "main", "table": "db.accounts"}],
        }
    )
    playbook = json.dumps(
        {
            "path": "playbooks/churn.md",
            "summary": "How churn is computed here",
            "body_md": "## The definition we use\n\n30-day window.",
        }
    )
    overview = json.dumps(
        {"path": "overview.md", "summary": "What this workspace measures",
         "body_md": "We sell things."}
    )
    return [
        _response(_message(content=plan)),                              # Pass B
        _response(_message(tool_calls=[_fn_call("c1", "SELECT 1")])),   # Pass C step 1
        _response(_message(content="Profiling complete.")),             # Pass C step 2
        _response(_message(content=ontology)),                          # Pass D
        _response(_message(content=playbook)),                          # Pass E
        _response(_message(content=overview)),                          # Pass E
    ]


def _ctx():
    warehouse = FakeWarehouse(
        tables=[fake_table("accounts", columns=("id", "email"))],
        columns=("n",),
        rows=((1,),),
    )
    return make_ctx(
        openai=FakeOpenAI(_scripted()),
        warehouses={"main": warehouse},
        settings=make_settings(OPENAI_MODEL="weak", OPENAI_DOCS_MODEL="strong"),
    )


def test_the_run_produces_an_entity_keyed_ontology_and_a_playbook():
    ctx = _ctx()
    events: list[tuple[str, dict]] = []

    result = dc.generate_data_context(
        ctx=ctx, on_event=lambda kind, data: events.append((kind, data))
    )

    assert [f.path for f in result.files] == [
        "ontology/customers.md",
        "playbooks/churn.md",
        "overview.md",
    ]

    ontology = result.files[0]
    # Keyed by the business entity, not the table it happens to live in.
    assert ontology.path == "ontology/customers.md"
    assert ontology.covers == [{"source": "main", "table": "db.accounts"}]
    # The profiling SQL is kept as provenance for the claims in the body.
    assert ontology.evidence and ontology.evidence[0]["sql"].startswith("SELECT 1")

    assert result.stats["entities"] == 1
    assert result.stats["playbooks"] == 1
    assert result.stats["queries"] == 1
    assert [p for k, p in events if k == "file"][0]["path"] == "ontology/customers.md"


def test_every_call_uses_the_docs_model_not_the_report_model():
    """The whole point of OPENAI_DOCS_MODEL. A silent fallback to the report
    loop's model would look identical from the outside."""
    ctx = _ctx()
    dc.generate_data_context(ctx=ctx)

    used = ctx.openai.chat.completions.models_used
    assert used, "no completions were made"
    assert set(used) == {"strong"}, used
    assert ctx.model == "weak"  # ...and the report loop is untouched


def test_a_dead_source_is_recorded_and_does_not_stop_the_run():
    ctx = make_ctx(
        openai=FakeOpenAI(_scripted()),
        warehouses={"main": FakeWarehouse(fail=RuntimeError("connection refused"))},
        settings=make_settings(OPENAI_DOCS_MODEL="strong"),
    )
    result = dc.generate_data_context(ctx=ctx)

    assert result.stats["failed_sources"] == ["main"]
    # Nothing to document, so nothing is invented.
    assert result.files == []


def test_one_run_per_org_at_a_time():
    ctx = _ctx()
    assert dc.try_acquire(ctx.org_id) is True
    assert dc.try_acquire(ctx.org_id) is False
    dc.release(ctx.org_id)
    assert dc.try_acquire(ctx.org_id) is True
    dc.release(ctx.org_id)
