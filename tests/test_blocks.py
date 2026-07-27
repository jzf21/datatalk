"""Block-document model tests: round-trip, materialize, flatten."""

from datatalk.agent.blocks import (
    Chart,
    Document,
    Heading,
    Paragraph,
    Row,
    Stat,
    Table,
    count_data_blocks,
    document_to_text,
    materialize,
    parse_json_object,
    validate_references,
)
from datatalk.agent.executor import QueryResult


def _dataset():
    return QueryResult(
        columns=["month", "issues", "resolved"],
        rows=[["2026-01", 10, 8], ["2026-02", 20, 15]],
        row_count=2,
        truncated=False,
        sql="SELECT ...",
    )


def test_round_trip_to_from_dict():
    doc = Document(
        blocks=[
            Heading(text="Overview", level=1),
            Paragraph(text="Some **bold** text."),
            Table(dataset_id="q1", columns=["month", "issues"]),
            Chart(
                chart_type="bar",
                title="Issues by month",
                dataset_id="q1",
                x_col="month",
                series_cols=["issues"],
            ),
        ]
    )
    restored = Document.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()
    assert isinstance(restored.blocks[0], Heading)
    assert isinstance(restored.blocks[2], Table)
    assert restored.blocks[2].dataset_id == "q1"


def test_materialize_table_and_chart():
    doc = Document(
        blocks=[
            Table(dataset_id="q1", columns=["month", "issues"]),
            Chart(
                chart_type="line",
                title="Trend",
                dataset_id="q1",
                x_col="month",
                series_cols=["issues", "resolved"],
            ),
        ]
    )
    out = materialize(doc, {"q1": _dataset()})

    table = out.blocks[0]
    assert isinstance(table, Table)
    # Materialized values equal the captured dataset values (no fabrication).
    assert table.columns == ["month", "issues"]
    assert table.rows == [["2026-01", 10], ["2026-02", 20]]

    chart = out.blocks[1]
    assert isinstance(chart, Chart)
    assert chart.x == {"label": "month", "values": ["2026-01", "2026-02"]}
    assert chart.series == [
        {"name": "issues", "values": [10, 20]},
        {"name": "resolved", "values": [8, 15]},
    ]


def test_materialize_preserves_dataset_id_for_citation():
    """The UI cites the query behind every number, so the id must survive."""
    doc = Document(
        blocks=[
            Table(dataset_id="q1", columns=["month"]),
            Chart(chart_type="bar", title="T", dataset_id="q1",
                  x_col="month", series_cols=["issues"]),
            Stat(dataset_id="q1", label="Issues", value_col="issues"),
        ]
    )
    out = materialize(doc, {"q1": _dataset()})

    assert [b.dataset_id for b in out.blocks] == ["q1", "q1", "q1"]
    assert all(b["dataset_id"] == "q1" for b in out.to_dict()["blocks"])


def test_materialized_blocks_are_not_re_materialized():
    """dataset_id is not the authoring discriminator -- rows/x/value are."""
    doc = Document(
        blocks=[
            Table(dataset_id="q1"),
            Chart(chart_type="bar", title="T", dataset_id="q1",
                  x_col="month", series_cols=["issues"]),
            Stat(dataset_id="q1", label="Issues", value_col="issues"),
        ]
    )
    once = materialize(doc, {"q1": _dataset()})
    # Re-running against no datasets would degrade to notes if the blocks still
    # looked like authoring blocks.
    twice = materialize(once, {})

    assert [type(b) for b in twice.blocks] == [Table, Chart, Stat]
    assert twice.blocks[0].rows == once.blocks[0].rows
    assert twice.blocks[1].series == once.blocks[1].series
    assert twice.blocks[2].value == once.blocks[2].value


def test_materialize_all_columns_when_omitted():
    doc = Document(blocks=[Table(dataset_id="q1")])
    out = materialize(doc, {"q1": _dataset()})
    assert out.blocks[0].columns == ["month", "issues", "resolved"]
    assert out.blocks[0].rows == [["2026-01", 10, 8], ["2026-02", 20, 15]]


def test_materialize_missing_dataset_degrades_to_paragraph():
    doc = Document(blocks=[Table(dataset_id="nope", columns=["x"])])
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)
    assert "unavailable" in out.blocks[0].text


def test_materialize_bad_column_degrades_to_paragraph():
    doc = Document(
        blocks=[
            Chart(
                chart_type="bar",
                dataset_id="q1",
                x_col="month",
                series_cols=["not_a_column"],
            )
        ]
    )
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)
    assert "not_a_column" in out.blocks[0].text


def test_materialize_bad_chart_type_degrades():
    doc = Document(
        blocks=[Chart(chart_type="donut", dataset_id="q1", x_col="month", series_cols=["issues"])]
    )
    out = materialize(doc, {"q1": _dataset()})
    assert isinstance(out.blocks[0], Paragraph)


def test_document_to_text_flattens_materialized_doc():
    doc = materialize(
        Document(
            blocks=[
                Heading(text="Report", level=1),
                Paragraph(text="Summary line."),
                Table(dataset_id="q1", columns=["month", "issues"]),
                Chart(
                    chart_type="bar",
                    title="Issues",
                    dataset_id="q1",
                    x_col="month",
                    series_cols=["issues"],
                ),
            ]
        ),
        {"q1": _dataset()},
    )
    text = document_to_text(doc)
    assert "# Report" in text
    assert "Summary line." in text
    assert "month | issues" in text
    assert "2026-01 | 10" in text
    assert "Issues (bar)" in text


def test_parse_json_object_handles_code_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('prefix {"b": 2} suffix') == {"b": 2}
    assert parse_json_object("not json") == {}


def _delta_dataset():
    from datatalk.agent.executor import QueryResult
    return QueryResult(
        columns=["metric", "current", "prior"],
        rows=[["revenue", 120, 100]],
        row_count=1,
        truncated=False,
        sql="SELECT ...",
    )


def test_materialize_stat_value_and_delta():
    doc = Document(
        blocks=[
            Stat(dataset_id="q1", value_col="current", label="Revenue",
                 delta_col="prior", unit="$"),
        ]
    )
    out = materialize(doc, {"q1": _delta_dataset()})
    stat = out.blocks[0]
    assert isinstance(stat, Stat)
    assert stat.value == 120
    assert stat.delta == 20.0
    assert stat.delta_pct == 20.0
    assert stat.label == "Revenue"
    assert stat.unit == "$"


def test_materialize_stat_defaults_to_last_row():
    from datatalk.agent.executor import QueryResult
    ds = QueryResult(columns=["m", "v"], rows=[["jan", 1], ["feb", 9]],
                     row_count=2, truncated=False, sql="x")
    out = materialize(Document(blocks=[Stat(dataset_id="q1", value_col="v", label="V")]),
                      {"q1": ds})
    assert out.blocks[0].value == 9  # last row by default


def test_materialize_stat_bad_reference_degrades():
    out = materialize(Document(blocks=[Stat(dataset_id="q1", value_col="nope", label="X")]),
                      {"q1": _delta_dataset()})
    assert isinstance(out.blocks[0], Paragraph)
    assert "unavailable" in out.blocks[0].text

    out2 = materialize(Document(blocks=[Stat(dataset_id="nope", value_col="v", label="X")]),
                       {"q1": _delta_dataset()})
    assert isinstance(out2.blocks[0], Paragraph)


def test_materialize_row_children_and_one_bad_child_still_renders():
    doc = Document(
        blocks=[
            Row(children=[
                Stat(dataset_id="q1", value_col="current", label="Revenue", width=6),
                Stat(dataset_id="q1", value_col="nope", label="Broken", width=6),
                Table(dataset_id="q1", columns=["metric", "current"], width=99),
            ])
        ]
    )
    out = materialize(doc, {"q1": _delta_dataset()})
    row = out.blocks[0]
    assert isinstance(row, Row)
    assert isinstance(row.children[0], Stat) and row.children[0].value == 120
    assert row.children[0].width == 6                    # width preserved
    assert isinstance(row.children[1], Paragraph)        # bad child -> note
    assert isinstance(row.children[2], Table)            # good child still renders
    assert row.children[2].width == 12                   # clamped 99 -> 12


def test_row_round_trips_through_dict():
    doc = Document(blocks=[Row(children=[
        Stat(dataset_id="q1", value_col="current", label="Rev", width=4),
        Heading(text="Hi", level=2, width=8),
    ])])
    restored = Document.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()
    assert isinstance(restored.blocks[0], Row)
    assert isinstance(restored.blocks[0].children[0], Stat)
    assert restored.blocks[0].children[0].width == 4


def test_document_to_text_flattens_stat_and_row():
    doc = materialize(
        Document(blocks=[Row(children=[
            Stat(dataset_id="q1", value_col="current", label="Revenue",
                 delta_col="prior", unit="$"),
        ])]),
        {"q1": _delta_dataset()},
    )
    text = document_to_text(doc)
    assert "Revenue: 120" in text
    assert "Δ 20.0" in text


def test_materialize_does_not_mutate_original_document():
    original_child = Heading(text="Hi", level=2, width=99)
    doc = Document(blocks=[Row(children=[original_child])])
    out = materialize(doc, {"q1": _dataset()})

    # The original child object must be untouched (materialize() is pure).
    assert original_child.width == 99
    # The returned document's corresponding child has the clamped width.
    assert out.blocks[0].children[0].width == 12


def test_materialize_clamps_top_level_stat_width():
    ds = _delta_dataset()
    out = materialize(
        Document(blocks=[Stat(dataset_id="q1", value_col="current", label="V", width=99)]),
        {"q1": ds},
    )
    stat = out.blocks[0]
    assert isinstance(stat, Stat)
    assert stat.width == 12


def test_materialize_keeps_top_level_chart_and_table_width():
    """A top-level chart/table must keep its grid width.

    ``materialize`` only re-applies width to a ``Row``'s children, so a block
    laid out at the top level of the grid depended on its own materializer
    carrying the width across -- and chart and table silently dropped it,
    rendering full-bleed no matter what the author asked for.
    """
    ds = _dataset()
    out = materialize(
        Document(blocks=[
            Chart(dataset_id="q1", chart_type="line", x_col="month",
                  series_cols=["issues"], width=8),
            Table(dataset_id="q1", columns=["month"], width=4),
        ]),
        {"q1": ds},
    )
    assert out.blocks[0].width == 8
    assert out.blocks[1].width == 4


def test_materialize_clamps_top_level_chart_and_table_width():
    ds = _dataset()
    out = materialize(
        Document(blocks=[
            Chart(dataset_id="q1", chart_type="line", x_col="month",
                  series_cols=["issues"], width=99),
            Table(dataset_id="q1", width=0),
        ]),
        {"q1": ds},
    )
    assert out.blocks[0].width == 12
    assert out.blocks[1].width == 1


# --- reference validation ----------------------------------------------------

def _mixed_document():
    """One document covering every way a reference can be good or bad."""
    return Document(blocks=[
        Heading(text="Title"),
        Paragraph(text="prose"),
        Row(children=[
            Stat(dataset_id="q1", value_col="issues", label="ok"),
            Stat(dataset_id="q1", value_col="ghost", label="bad column"),
            Stat(dataset_id="nope", value_col="issues", label="bad dataset"),
            Stat(dataset_id="q1", value_col="issues", delta_col="ghost", label="bad delta"),
            Stat(dataset_id="q1", value_col="issues", row_index=99, label="bad row"),
        ]),
        Chart(dataset_id="q1", chart_type="line", x_col="month", series_cols=["issues"]),
        Chart(dataset_id="q1", chart_type="sunburst", x_col="month", series_cols=["issues"]),
        Chart(dataset_id="q1", chart_type="bar", x_col="month", series_cols=["ghost"]),
        Chart(dataset_id="q1", chart_type="bar", x_col="", series_cols=[]),
        Table(dataset_id="q1", columns=["month", "issues"]),
        Table(dataset_id="q1", columns=["ghost"]),
        Table(dataset_id="nope"),
    ])


def test_validate_references_agrees_with_materialize():
    """Validation and materialization must never disagree.

    They share the ``_check_*`` predicates precisely so a repair turn is offered
    for exactly the blocks that would otherwise degrade to a note -- no more
    (the author gets sent back over nothing) and no fewer (a note ships).
    """
    ds = {"q1": _dataset()}
    doc = _mixed_document()

    errors = validate_references(doc, ds)
    out = materialize(doc, ds)

    flagged = {e.path for e in errors}

    def degraded(path, block):
        return {path} if isinstance(block, Paragraph) and block.text.startswith("_") else set()

    actual = set()
    for i, b in enumerate(out.blocks):
        if isinstance(b, Row):
            for j, c in enumerate(b.children):
                actual |= degraded(f"blocks[{i}].children[{j}]", c)
        else:
            actual |= degraded(f"blocks[{i}]", b)

    assert flagged == actual
    assert len(errors) == 9  # 4 bad stats + 3 bad charts + 2 bad tables


def test_validate_references_is_clean_for_a_good_document():
    ds = {"q1": _dataset()}
    doc = Document(blocks=[
        Heading(text="T"),
        Row(children=[Stat(dataset_id="q1", value_col="issues", label="ok")]),
        Chart(dataset_id="q1", chart_type="bar", x_col="month", series_cols=["issues"]),
        Table(dataset_id="q1"),
    ])
    assert validate_references(doc, ds) == []


def test_validate_references_ignores_materialized_blocks():
    """Only authoring blocks reference anything; materialized ones are done."""
    ds = {"q1": _dataset()}
    out = materialize(Document(blocks=[
        Table(dataset_id="q1", columns=["month"])]), ds)
    assert validate_references(out, {}) == []


def test_count_data_blocks_ignores_prose_and_notes():
    """Zero data blocks is what 'the dashboard is empty' actually means.

    A document can be non-empty and still show nothing: a page of degradation
    notes has blocks but no data, and used to be saved as a success.
    """
    ds = {"q1": _dataset()}
    notes_only = materialize(Document(blocks=[
        Heading(text="T"),
        Paragraph(text="prose"),
        Table(dataset_id="ghost"),
    ]), ds)
    assert count_data_blocks(notes_only) == 0

    with_data = materialize(Document(blocks=[
        Heading(text="T"),
        Row(children=[
            Stat(dataset_id="q1", value_col="issues", label="ok"),
            Stat(dataset_id="ghost", value_col="issues", label="bad"),
        ]),
        Table(dataset_id="q1"),
    ]), ds)
    assert count_data_blocks(with_data) == 2


# --- presentation hints (unit / stacked / direction / horizontal_bar) ---------

def test_presentation_hints_round_trip():
    doc = Document(blocks=[
        Chart(chart_type="horizontal_bar", dataset_id="q1", x_col="month",
              series_cols=["issues"], unit="ratio", stacked=True),
        Stat(dataset_id="q1", value_col="issues", label="Churn",
             direction="down_is_good"),
    ])
    restored = Document.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()
    assert restored.blocks[0].unit == "ratio"
    assert restored.blocks[0].stacked is True
    assert restored.blocks[1].direction == "down_is_good"


def test_old_documents_serialize_byte_identically():
    """A document authored before the hint fields existed must not grow keys."""
    old = {
        "blocks": [
            {"type": "chart", "chart_type": "bar", "title": "T",
             "dataset_id": "q1", "x_col": "month", "series_cols": ["issues"]},
            {"type": "stat", "label": "V", "dataset_id": "q1",
             "value_col": "issues"},
        ]
    }
    assert Document.from_dict(old).to_dict() == old


def test_materialize_carries_valid_hints_through():
    doc = Document(blocks=[
        Chart(chart_type="horizontal_bar", dataset_id="q1", x_col="month",
              series_cols=["issues"], unit="percent", stacked=True),
        Stat(dataset_id="q1", value_col="issues", label="Churn",
             direction="down_is_good"),
    ])
    out = materialize(doc, {"q1": _dataset()})
    chart, stat = out.blocks
    assert isinstance(chart, Chart)
    assert chart.chart_type == "horizontal_bar"  # a real type, not a note
    assert chart.unit == "percent"
    assert chart.stacked is True
    assert isinstance(stat, Stat)
    assert stat.direction == "down_is_good"


def test_materialize_normalizes_junk_hints_silently():
    """A bad hint costs the hint, never the block."""
    doc = Document(blocks=[
        Chart(chart_type="bar", dataset_id="q1", x_col="month",
              series_cols=["issues"], unit="furlongs", stacked=0),
        Stat(dataset_id="q1", value_col="issues", label="V",
             direction="sideways"),
    ])
    out = materialize(doc, {"q1": _dataset()})
    chart, stat = out.blocks
    assert isinstance(chart, Chart)
    assert chart.unit is None
    assert chart.stacked is None
    assert isinstance(stat, Stat)
    assert stat.direction is None


def test_validate_references_ignores_bad_hints():
    """The repair budget is for lost content, not cosmetics."""
    doc = Document(blocks=[
        Chart(chart_type="bar", dataset_id="q1", x_col="month",
              series_cols=["issues"], unit="furlongs"),
        Stat(dataset_id="q1", value_col="issues", label="V",
             direction="sideways"),
    ])
    assert validate_references(doc, {"q1": _dataset()}) == []
