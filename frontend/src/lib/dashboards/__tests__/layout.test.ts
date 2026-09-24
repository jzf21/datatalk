import { describe, expect, it } from "vitest";

import {
  addHeading,
  buildModel,
  canStack,
  compatibleChartTypes,
  moveItem,
  moveTile,
  moveTileToNewRow,
  removeTile,
  toAuthoring,
  toPreview,
  updateTile,
  type LooseDoc,
} from "../layout";

const stat = (col: string) => ({
  type: "stat",
  label: col,
  dataset_id: "q1",
  value_col: col,
  width: 3,
});
const chart = {
  type: "chart",
  chart_type: "bar",
  title: "Velocity",
  dataset_id: "q2",
  x_col: "sprint",
  series_cols: ["committed", "completed"],
};

const AUTHORING: LooseDoc = {
  blocks: [
    { type: "row", children: [stat("a"), stat("b")] },
    { type: "heading", text: "Trend", level: 2 },
    chart, // a data block at the top level: wrapped in a row of its own
  ],
};

// What materialize() returns for it: same shape, values filled -- except the
// chart, which failed and degraded to a note.
const PREVIEW: LooseDoc = {
  blocks: [
    {
      type: "row",
      children: [
        { ...stat("a"), value: 1 },
        { ...stat("b"), value: 2 },
      ],
    },
    { type: "heading", text: "Trend", level: 2 },
    { type: "paragraph", text: "_chart unavailable: q2 failed_" },
  ],
};

describe("layout model", () => {
  it("round-trips the authoring document, wrapping loose data blocks in rows", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    expect(toAuthoring(model)).toEqual({
      blocks: [
        { type: "row", children: [stat("a"), stat("b")] },
        { type: "heading", text: "Trend", level: 2 },
        { type: "row", children: [chart] },
      ],
    });
  });

  it("pairs a degraded preview with its authoring block, by position", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    const row = model.items[2];
    expect(row.kind === "row" && row.tiles[0].preview?.type).toBe("paragraph");
    expect(row.kind === "row" && row.tiles[0].authoring.type).toBe("chart");
  });

  it("moves a tile across rows, on both sides, and drops the emptied row", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    const firstRow = model.items[0];
    const chartRow = model.items[2];
    if (chartRow.kind !== "row" || firstRow.kind !== "row")
      throw new Error("shape");
    const moved = moveTile(model, chartRow.tiles[0].key, firstRow.key, 1);

    expect(toAuthoring(moved).blocks).toEqual([
      { type: "row", children: [stat("a"), chart, stat("b")] },
      { type: "heading", text: "Trend", level: 2 },
    ]);
    const previewRow = toPreview(moved).blocks[0];
    expect(previewRow.children?.map((c) => c.type)).toEqual([
      "stat",
      "paragraph",
      "stat",
    ]);
  });

  it("reorders within a row and splits a tile into its own row", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    const row = model.items[0];
    if (row.kind !== "row") throw new Error("shape");
    const swapped = moveTile(model, row.tiles[1].key, row.key, 0);
    expect(
      toAuthoring(swapped).blocks[0].children?.map((c) => c.value_col),
    ).toEqual(["b", "a"]);

    const split = moveTileToNewRow(model, row.tiles[0].key, row.key);
    expect(toAuthoring(split).blocks.slice(0, 2)).toEqual([
      { type: "row", children: [stat("b")] },
      { type: "row", children: [stat("a")] },
    ]);
  });

  it("removes a tile and moves whole sections", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    const row = model.items[0];
    if (row.kind !== "row") throw new Error("shape");
    const removed = removeTile(
      removeTile(model, row.tiles[0].key),
      row.tiles[1].key,
    );
    expect(toAuthoring(removed).blocks.map((b) => b.type)).toEqual([
      "heading",
      "row",
    ]);

    const up = moveItem(model, model.items[1].key, -1);
    expect(toAuthoring(up).blocks.map((b) => b.type)).toEqual([
      "heading",
      "row",
      "row",
    ]);
    expect(moveItem(model, model.items[0].key, -1)).toBe(model); // already first
  });

  it("changes presentation on both sides, never data references", () => {
    const model = buildModel(AUTHORING, {
      blocks: [
        PREVIEW.blocks[0],
        PREVIEW.blocks[1],
        { ...chart, x: { label: "sprint", values: [] }, series: [] },
      ],
    });
    const row = model.items[2];
    if (row.kind !== "row") throw new Error("shape");
    const edited = updateTile(model, row.tiles[0].key, {
      chart_type: "area",
      stacked: true,
      width: 8,
      title: "Points",
    });
    const [tile] = (
      edited.items[2] as {
        tiles: {
          authoring: Record<string, unknown>;
          preview: Record<string, unknown> | null;
        }[];
      }
    ).tiles;
    expect(tile.authoring).toMatchObject({
      chart_type: "area",
      stacked: true,
      width: 8,
      title: "Points",
      series_cols: ["committed", "completed"],
    });
    expect(tile.preview).toMatchObject({
      chart_type: "area",
      stacked: true,
      width: 8,
    });

    const unstacked = updateTile(edited, row.tiles[0].key, { stacked: false });
    expect(
      "stacked" in (toAuthoring(unstacked).blocks[2].children?.[0] ?? {}),
    ).toBe(false);
  });

  it("offers only chart types that can draw the data", () => {
    expect(compatibleChartTypes(chart)).not.toContain("pie");
    expect(compatibleChartTypes({ ...chart, series_cols: ["x"] })).toContain(
      "pie",
    );
    expect(compatibleChartTypes(stat("a"))).toEqual([]);
    expect(canStack(chart)).toBe(true);
    expect(canStack({ ...chart, chart_type: "line" })).toBe(false);
  });

  it("adds a heading before a given item", () => {
    const model = buildModel(AUTHORING, PREVIEW);
    const withHeading = addHeading(model, "KPIs", model.items[0].key);
    expect(toAuthoring(withHeading).blocks[0]).toEqual({
      type: "heading",
      text: "KPIs",
      level: 2,
    });
  });
});
