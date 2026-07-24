"""Block-document model tests: round-trip, materialize, flatten."""

from datatalk.agent.blocks import (
    Chart,
    Document,
    Heading,
    Paragraph,
    Row,
    Stat,
    Table,
    document_to_text,
    materialize,
    parse_json_object,
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
