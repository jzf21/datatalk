import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/lib/api/types";

import { applyEvent, EMPTY } from "../use-ndjson-run";

function play(events: RunEvent[], from = { ...EMPTY, phase: "streaming" as const }) {
  return events.reduce(applyEvent, from);
}

const sql = (query_id: string, text: string): RunEvent => ({
  kind: "sql",
  data: { sql: text, query_id, source: "events" },
});

const result = (query_id: string, dataset_id: string): RunEvent => ({
  kind: "result",
  data: { query_id, dataset_id, row_count: 3, columns: ["a"], source: "events" },
});

describe("applyEvent", () => {
  it("resolves out-of-order results by query_id (batched turns run concurrently)", () => {
    const state = play([
      sql("c1", "SELECT 1"),
      sql("c2", "SELECT 2"),
      result("c2", "q2"),
      result("c1", "q1"),
    ]);
    expect(state.steps.map((s) => s.datasetId)).toEqual(["q1", "q2"]);
    expect(state.steps.every((s) => s.state === "done")).toBe(true);
  });

  it("falls back to the most recent running step when query_id is absent", () => {
    const state = play([
      { kind: "sql", data: { sql: "SELECT 1" } },
      { kind: "sql", data: { sql: "SELECT 2" } },
      { kind: "result", data: { dataset_id: "q1", row_count: 1, columns: ["a"] } },
    ]);
    expect(state.steps[1].datasetId).toBe("q1");
    expect(state.steps[0].state).toBe("running");
  });

  it("attaches an error to the step it belongs to, not the last one", () => {
    const state = play([
      sql("c1", "SELECT 1"),
      sql("c2", "SELECT 2"),
      { kind: "error", data: { message: "relation does not exist", query_id: "c1" } },
      result("c2", "q1"),
    ]);
    expect(state.steps[0].retries).toEqual(["relation does not exist"]);
    expect(state.steps[1].retries).toEqual([]);
  });

  it("keeps the source from the sql event on each step", () => {
    const state = play([sql("c1", "SELECT 1")]);
    expect(state.steps[0].source).toBe("events");
  });

  it("ignores ping heartbeats", () => {
    const before = play([sql("c1", "SELECT 1")]);
    const after = applyEvent(before, { kind: "ping", data: {} });
    expect(after).toEqual(before);
  });
});
