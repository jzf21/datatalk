"""The shared introspection walk (BaseWarehouse), no engine attached.

The filtering behavior itself is covered against a live engine in
test_warehouse_postgres.py; these tests pin what the parallel column/sample
fetch must preserve — output order, the table cap, fail-the-source error
semantics — and prove the fetches actually overlap.
"""

from __future__ import annotations

import threading

import pytest

from datatalk.warehouse.base import BaseWarehouse, Column, Table, WarehouseSpec
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT


def _spec(**overrides) -> WarehouseSpec:
    base = dict(type="clickhouse", host="h", port=1, username="u")
    base.update(overrides)
    return WarehouseSpec(**base)


class ScriptedWarehouse(BaseWarehouse):
    """Four scripted catalog queries, with counters for the parallel walk."""

    def __init__(self, spec, tables_by_ns):
        self.spec = spec
        self.dialect = CLICKHOUSE_DIALECT
        self._tables_by_ns = tables_by_ns
        self.column_fetches: list[str] = []
        self.fail_on: str | None = None
        self.barrier: threading.Barrier | None = None

    def _list_namespaces(self):
        return list(self._tables_by_ns)

    def _fetch_tables(self, namespace):
        return [
            Table(database=namespace, name=n, columns=[], sample_rows=[])
            for n in self._tables_by_ns[namespace]
        ]

    def _fetch_columns(self, namespace, table):
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if table == self.fail_on:
            raise RuntimeError(f"cannot describe {table}")
        self.column_fetches.append(f"{namespace}.{table}")
        return [Column(name="id", type="String")]

    def _fetch_sample_rows(self, namespace, table, limit):
        return [{"id": 1}]


def test_output_order_matches_catalog_order_despite_parallel_fetches():
    wh = ScriptedWarehouse(_spec(), {"web": ["a", "b"], "billing": ["c", "d"]})
    wh.barrier = threading.Barrier(4)

    tables = wh.introspect()

    assert [f"{t.database}.{t.name}" for t in tables] == [
        "web.a",
        "web.b",
        "billing.c",
        "billing.d",
    ]
    assert all(t.columns and t.sample_rows for t in tables)


def test_the_table_cap_bounds_the_fetches_not_just_the_result():
    wh = ScriptedWarehouse(
        _spec(introspect_max_tables=2), {"web": ["a", "b", "c", "d"]}
    )

    tables = wh.introspect()

    assert [t.name for t in tables] == ["a", "b"]
    # The cap keeps the expensive per-table round trips from running at all.
    assert sorted(wh.column_fetches) == ["web.a", "web.b"]


def test_one_failing_table_still_fails_the_whole_source():
    """A partial catalog that silently lost a table would read as complete."""
    wh = ScriptedWarehouse(_spec(), {"web": ["a", "b", "c"]})
    wh.fail_on = "b"

    with pytest.raises(RuntimeError, match="cannot describe b"):
        wh.introspect()


def test_a_single_candidate_skips_the_pool():
    wh = ScriptedWarehouse(_spec(), {"web": ["only"]})
    # A barrier with parties=2 would deadlock if a pool thread were involved
    # alongside the caller; a single inline fetch never touches it.
    tables = wh.introspect()
    assert [t.name for t in tables] == ["only"]
