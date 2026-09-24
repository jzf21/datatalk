/**
 * The layout editor's model: pure, so vitest can cover it (see filters.ts).
 *
 * Two documents are edited in lockstep. The *authoring* document is what gets
 * saved -- blocks that reference a dataset by id and column, never values. The
 * *preview* document is the materialized one already on screen, so edits show
 * real numbers without a round trip. They are paired by position, which holds
 * because `materialize()` maps block for block, row for row; every operation
 * here moves a pair, never one side.
 *
 * Saving sends only `toAuthoring(model)`. The server re-validates every
 * reference and strips any value, so nothing this module does can put a number
 * on screen that no query produced.
 */

import type { ChartType } from "@/lib/api/types";

/** A block in either form. Authoring and materialized fields differ. */
export type LooseBlock = {
  type: string;
  width?: number;
  children?: LooseBlock[];
  [key: string]: unknown;
};
export type LooseDoc = { blocks: LooseBlock[] };

export interface Tile {
  key: string;
  authoring: LooseBlock;
  /** The materialized counterpart; null if the preview had nothing there. */
  preview: LooseBlock | null;
}

export type LayoutItem =
  | { key: string; kind: "row"; tiles: Tile[] }
  | {
      key: string;
      kind: "text";
      authoring: LooseBlock;
      preview: LooseBlock | null;
    };

export interface LayoutModel {
  items: LayoutItem[];
  /** Next key suffix; keys are stable for the life of one editing session. */
  seq: number;
}

export const WIDTH_CHOICES = [3, 4, 6, 8, 12] as const;

const TEXT_TYPES = new Set(["heading", "paragraph"]);

/**
 * Build the model. A data block sitting at the top level (not in a row) is
 * wrapped in a one-tile row, decided by the *authoring* side: a block that
 * failed to materialize is a paragraph in the preview but still a chart here.
 */
export function buildModel(
  authoring: LooseDoc,
  preview: LooseDoc | null,
): LayoutModel {
  let seq = 0;
  const next = (p: string) => `${p}${seq++}`;
  const items: LayoutItem[] = [];
  const blocks = authoring.blocks ?? [];
  const shown = preview?.blocks ?? [];

  blocks.forEach((block, i) => {
    const counterpart = shown[i] ?? null;
    if (block.type === "row") {
      const children = block.children ?? [];
      const previewChildren =
        counterpart?.type === "row" ? (counterpart.children ?? []) : [];
      items.push({
        key: next("r"),
        kind: "row",
        tiles: children.map((child, j) => ({
          key: next("t"),
          authoring: child,
          preview: previewChildren[j] ?? null,
        })),
      });
    } else if (TEXT_TYPES.has(block.type)) {
      items.push({
        key: next("x"),
        kind: "text",
        authoring: block,
        preview: counterpart,
      });
    } else {
      items.push({
        key: next("r"),
        kind: "row",
        tiles: [{ key: next("t"), authoring: block, preview: counterpart }],
      });
    }
  });
  return { items, seq };
}

export function toAuthoring(model: LayoutModel): LooseDoc {
  return {
    blocks: model.items.flatMap((item): LooseBlock[] => {
      if (item.kind === "text") return [item.authoring];
      if (!item.tiles.length) return [];
      return [{ type: "row", children: item.tiles.map((t) => t.authoring) }];
    }),
  };
}

/** The preview document, in the model's current arrangement. */
export function toPreview(model: LayoutModel): LooseDoc {
  return {
    blocks: model.items.flatMap((item): LooseBlock[] => {
      if (item.kind === "text") return item.preview ? [item.preview] : [];
      if (!item.tiles.length) return [];
      return [
        {
          type: "row",
          children: item.tiles.map(
            (t) =>
              t.preview ?? { type: "paragraph", text: "_Not refreshed yet._" },
          ),
        },
      ];
    }),
  };
}

// --- finding things ----------------------------------------------------------

export function findTile(
  model: LayoutModel,
  tileKey: string,
): { rowIndex: number; index: number; tile: Tile } | null {
  for (let r = 0; r < model.items.length; r++) {
    const item = model.items[r];
    if (item.kind !== "row") continue;
    const index = item.tiles.findIndex((t) => t.key === tileKey);
    if (index >= 0) return { rowIndex: r, index, tile: item.tiles[index] };
  }
  return null;
}

function withRows(model: LayoutModel, items: LayoutItem[]): LayoutModel {
  // An emptied row disappears, as it would on the server.
  return {
    ...model,
    items: items.filter((i) => i.kind !== "row" || i.tiles.length),
  };
}

// --- operations --------------------------------------------------------------

/** Move a tile into row `toRowKey` at `toIndex` (clamped); same row or another. */
export function moveTile(
  model: LayoutModel,
  tileKey: string,
  toRowKey: string,
  toIndex: number,
): LayoutModel {
  const from = findTile(model, tileKey);
  if (!from) return model;
  const items = model.items.map((item) =>
    item.kind === "row" ? { ...item, tiles: [...item.tiles] } : item,
  );
  const source = items[from.rowIndex] as Extract<LayoutItem, { kind: "row" }>;
  const [tile] = source.tiles.splice(from.index, 1);
  const target = items.find((i) => i.key === toRowKey);
  if (!target || target.kind !== "row") return model;
  const at = Math.max(0, Math.min(toIndex, target.tiles.length));
  target.tiles.splice(at, 0, tile);
  return withRows(model, items);
}

/** Pull a tile out into a row of its own, directly after row `afterKey`. */
export function moveTileToNewRow(
  model: LayoutModel,
  tileKey: string,
  afterKey: string,
): LayoutModel {
  const from = findTile(model, tileKey);
  if (!from) return model;
  const key = `r${model.seq}`;
  const detached = removeTile(model, tileKey);
  const at = detached.items.findIndex((i) => i.key === afterKey);
  const items = [...detached.items];
  items.splice(at < 0 ? items.length : at + 1, 0, {
    key,
    kind: "row",
    tiles: [from.tile],
  });
  return { items, seq: model.seq + 1 };
}

export function removeTile(model: LayoutModel, tileKey: string): LayoutModel {
  return withRows(
    model,
    model.items.map((item) =>
      item.kind === "row"
        ? { ...item, tiles: item.tiles.filter((t) => t.key !== tileKey) }
        : item,
    ),
  );
}

/** Move a whole row (or heading) up or down among the items. */
export function moveItem(
  model: LayoutModel,
  key: string,
  delta: -1 | 1,
): LayoutModel {
  const i = model.items.findIndex((item) => item.key === key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= model.items.length) return model;
  const items = [...model.items];
  [items[i], items[j]] = [items[j], items[i]];
  return { ...model, items };
}

export function removeItem(model: LayoutModel, key: string): LayoutModel {
  return { ...model, items: model.items.filter((i) => i.key !== key) };
}

/** Presentation fields a tile may change; applied to both sides of the pair. */
export type TilePatch = Partial<{
  width: number;
  chart_type: ChartType;
  stacked: boolean | undefined;
  title: string;
  label: string;
}>;

export function updateTile(
  model: LayoutModel,
  tileKey: string,
  patch: TilePatch,
): LayoutModel {
  const apply = (block: LooseBlock): LooseBlock => {
    const next: LooseBlock = { ...block, ...patch };
    // `stacked` is only ever true or absent on the wire (None is dropped).
    if (!next.stacked) delete next.stacked;
    return next;
  };
  return {
    ...model,
    items: model.items.map((item) =>
      item.kind !== "row"
        ? item
        : {
            ...item,
            tiles: item.tiles.map((t) =>
              t.key !== tileKey
                ? t
                : {
                    ...t,
                    authoring: apply(t.authoring),
                    // A degraded preview (a note) takes only the width.
                    preview: t.preview
                      ? t.preview.type === t.authoring.type
                        ? apply(t.preview)
                        : {
                            ...t.preview,
                            width: patch.width ?? t.preview.width,
                          }
                      : null,
                  },
            ),
          },
    ),
  };
}

export function renameText(
  model: LayoutModel,
  key: string,
  text: string,
): LayoutModel {
  return {
    ...model,
    items: model.items.map((item) =>
      item.kind === "text" && item.key === key
        ? {
            ...item,
            authoring: { ...item.authoring, text },
            preview: item.preview ? { ...item.preview, text } : null,
          }
        : item,
    ),
  };
}

export function addHeading(
  model: LayoutModel,
  text: string,
  beforeKey?: string,
): LayoutModel {
  const block: LooseBlock = { type: "heading", text, level: 2 };
  const item: LayoutItem = {
    key: `x${model.seq}`,
    kind: "text",
    authoring: block,
    preview: block,
  };
  const items = [...model.items];
  const at = beforeKey ? items.findIndex((i) => i.key === beforeKey) : -1;
  items.splice(at < 0 ? items.length : at, 0, item);
  return { items, seq: model.seq + 1 };
}

// --- what a tile can become --------------------------------------------------

/**
 * Chart types that can draw this chart's data. A pie is one series of parts,
 * so a multi-series chart cannot become one; everything else can swap freely.
 */
export function compatibleChartTypes(block: LooseBlock): ChartType[] {
  if (block.type !== "chart") return [];
  const series = Array.isArray(block.series_cols)
    ? block.series_cols.length
    : 1;
  const all: ChartType[] = ["bar", "horizontal_bar", "line", "area", "pie"];
  return series > 1 ? all.filter((t) => t !== "pie") : all;
}

/** Whether "stacked" means anything for this chart. */
export function canStack(block: LooseBlock): boolean {
  const series = Array.isArray(block.series_cols)
    ? block.series_cols.length
    : 0;
  return (
    block.type === "chart" &&
    series > 1 &&
    ["bar", "horizontal_bar", "area"].includes(String(block.chart_type))
  );
}

export function tileTitle(block: LooseBlock): string {
  if (block.type === "chart") return String(block.title ?? "") || "Chart";
  if (block.type === "stat") return String(block.label ?? "") || "Stat";
  if (block.type === "table") return "Table";
  return block.type;
}

/** Datasets referenced anywhere in the model -- one filter panel per dataset. */
export function datasetOf(block: LooseBlock): string | null {
  return typeof block.dataset_id === "string" ? block.dataset_id : null;
}
