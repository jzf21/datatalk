"use client";

import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  Heading2,
  Loader2,
  Plus,
  SplitSquareVertical,
  Trash2,
} from "lucide-react";
import { createContext, useContext, useMemo, useState } from "react";
import { toast } from "sonner";

import { BlockRenderer } from "@/components/doc/block-renderer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ApiError, apiErrorMessage } from "@/lib/api/client";
import {
  useAddDashboardWidget,
  useSaveDashboardLayout,
} from "@/lib/api/queries";
import type {
  Block,
  BlockDocument,
  ChartType,
  DashboardDetail,
} from "@/lib/api/types";
import { childWidths, widthStyle } from "@/lib/doc/widths";
import {
  WIDTH_CHOICES,
  addHeading,
  buildModel,
  canStack,
  compatibleChartTypes,
  datasetOf,
  findTile,
  moveItem,
  moveTile,
  moveTileToNewRow,
  removeItem,
  removeTile,
  renameText,
  tileTitle,
  toAuthoring,
  updateTile,
  type LayoutItem,
  type LayoutModel,
  type LooseBlock,
  type Tile,
} from "@/lib/dashboards/layout";
import { AskWidget } from "./ask-widget";
import { WidgetFilters } from "./widget-filters";

const CHART_LABELS: Record<ChartType, string> = {
  bar: "Bars",
  horizontal_bar: "Horizontal bars",
  line: "Line",
  area: "Area",
  pie: "Share",
};

const errorText = (err: unknown) =>
  err instanceof ApiError
    ? apiErrorMessage(err.message)
    : "Couldn't reach the API.";

/**
 * Edit mode for a dashboard: drag, resize, restyle, remove, add.
 *
 * Tiles show the numbers already on screen (the preview document), so editing
 * never waits on the warehouse. Saving sends the authoring document only; the
 * page then refreshes into the saved layout like any other view change.
 */
export function LayoutEditor({
  dashboard,
  preview,
  onClose,
  onReload,
}: {
  dashboard: DashboardDetail;
  preview: BlockDocument;
  onClose: () => void;
  /** Re-open the editor on the stored dashboard, after a server-side change. */
  onReload: () => void;
}) {
  const initial = useMemo(
    () => buildModel(dashboard.authoring_document as never, preview as never),
    // The editor is keyed by the page; it starts from whatever was on screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [model, setModel] = useState<LayoutModel>(initial);
  const [adding, setAdding] = useState(false);
  const save = useSaveDashboardLayout(dashboard.id);
  const add = useAddDashboardWidget(dashboard.id);

  const dirty =
    JSON.stringify(toAuthoring(model)) !== JSON.stringify(toAuthoring(initial));

  // How many tiles read each dataset: its filter wiring is shared by all of them.
  const readers = useMemo(() => {
    const out = new Map<string, number>();
    for (const item of model.items) {
      if (item.kind !== "row") continue;
      for (const t of item.tiles) {
        const ds = datasetOf(t.authoring);
        if (ds) out.set(ds, (out.get(ds) ?? 0) + 1);
      }
    }
    return out;
  }, [model]);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const overTile = findTile(model, String(over.id));
    if (overTile) {
      const row = model.items[overTile.rowIndex];
      setModel(moveTile(model, String(active.id), row.key, overTile.index));
      return;
    }
    // Dropped on a row's empty space: append to that row.
    const row = model.items.find((i) => i.key === String(over.id));
    if (row?.kind === "row") {
      setModel(moveTile(model, String(active.id), row.key, row.tiles.length));
    }
  };

  const doSave = () =>
    save.mutate(toAuthoring(model), {
      onSuccess: () => {
        toast.success("Layout saved");
        onClose();
      },
      onError: (err) => toast.error(errorText(err)),
    });

  const addWidget = (key: string) =>
    add.mutate(key, {
      onSuccess: () => {
        setAdding(false);
        onReload();
      },
      onError: (err) => toast.error(errorText(err)),
    });

  return (
    <div className="space-y-4">
      <div className="sticky top-14 z-10 -mx-6 flex flex-wrap items-center gap-2 border-b border-border bg-background/95 px-6 py-2 backdrop-blur">
        <span className="label-caps text-ink-tertiary">Editing layout</span>
        <span className="text-[12px] text-ink-tertiary">
          Drag tiles by their handle; drop on a tile to take its place.
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              setModel(addHeading(model, "New section", model.items[0]?.key))
            }
          >
            <Heading2 className="size-3.5" />
            Add heading
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setAdding(true)}
            disabled={dirty}
            title={
              dirty
                ? "Save your layout first"
                : "Add a widget to this dashboard"
            }
          >
            <Plus className="size-3.5" />
            Add widget
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={doSave}
            disabled={!dirty || save.isPending}
          >
            {save.isPending && <Loader2 className="size-3.5 animate-spin" />}
            Save layout
          </Button>
        </div>
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={onDragEnd}
      >
        <div className="doc space-y-6">
          {model.items.map((item, i) => (
            <ItemFrame
              key={item.key}
              item={item}
              first={i === 0}
              last={i === model.items.length - 1}
              onMove={(delta) => setModel(moveItem(model, item.key, delta))}
              onRemove={() => setModel(removeItem(model, item.key))}
            >
              {item.kind === "text" ? (
                <Input
                  aria-label="Heading text"
                  value={String(item.authoring.text ?? "")}
                  maxLength={200}
                  onChange={(e) =>
                    setModel(renameText(model, item.key, e.target.value))
                  }
                  className="h-9 text-[20px] font-semibold tracking-[-0.012em]"
                />
              ) : (
                <EditableRow
                  item={item}
                  renderTile={(tile) => (
                    <TileFrame
                      tile={tile}
                      model={model}
                      rowKey={item.key}
                      dashboard={dashboard}
                      readers={readers}
                      setModel={setModel}
                    />
                  )}
                />
              )}
            </ItemFrame>
          ))}
          {!model.items.length && (
            <p className="text-[13px] text-ink-secondary">
              Every widget has been removed. Save to keep an empty dashboard, or
              Cancel.
            </p>
          )}
        </div>
      </DndContext>

      <Sheet open={adding} onOpenChange={setAdding}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Add a widget</SheetTitle>
            <SheetDescription>
              {dashboard.widget_catalog.length > 0
                ? "From this report's catalog, with its filters, or describe one to generate."
                : "Describe the widget you want; the agents write its query."}
            </SheetDescription>
          </SheetHeader>
          <div className="flex-1 space-y-4 overflow-y-auto pb-4">
            {dashboard.widget_catalog.length > 0 && (
              <ul className="space-y-2 px-4">
                {dashboard.widget_catalog.map((w) => (
                  <li key={w.key}>
                    <button
                      type="button"
                      disabled={add.isPending}
                      onClick={() => addWidget(w.key)}
                      className="w-full rounded-[6px] border border-border px-3 py-2 text-left transition-colors duration-[120ms] hover:bg-primary-tint focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60"
                    >
                      <span className="block text-[13px] font-medium text-ink-primary">
                        {w.title}
                      </span>
                      {w.description && (
                        <span className="block text-[12px] text-ink-tertiary">
                          {w.description}
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <AskWidget
              dashboardId={dashboard.id}
              hasFilters={(dashboard.filters.filters ?? []).length > 0}
              onAdded={() => {
                setAdding(false);
                onReload();
              }}
            />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}

function ItemFrame({
  item,
  first,
  last,
  onMove,
  onRemove,
  children,
}: {
  item: LayoutItem;
  first: boolean;
  last: boolean;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
  children: React.ReactNode;
}) {
  const noun = item.kind === "text" ? "heading" : "row";
  return (
    <div className="group relative rounded-[6px] border border-dashed border-border p-2">
      <div className="mb-2 flex items-center gap-1">
        <span className="label-caps text-ink-tertiary">{noun}</span>
        <div className="ml-auto flex items-center">
          <Button
            variant="ghost"
            size="icon-xs"
            disabled={first}
            onClick={() => onMove(-1)}
            aria-label={`Move ${noun} up`}
          >
            <ArrowUp />
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            disabled={last}
            onClick={() => onMove(1)}
            aria-label={`Move ${noun} down`}
          >
            <ArrowDown />
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={onRemove}
            aria-label={`Remove ${noun}`}
          >
            <Trash2 />
          </Button>
        </div>
      </div>
      {children}
    </div>
  );
}

function EditableRow({
  item,
  renderTile,
}: {
  item: Extract<LayoutItem, { kind: "row" }>;
  renderTile: (tile: Tile) => React.ReactNode;
}) {
  // The row itself is a drop target, so a tile can join a row by its gaps.
  const { setNodeRef, isOver } = useDroppable({ id: item.key });
  const widths = childWidths(
    item.tiles.map((t) => t.authoring as unknown as Block),
  );
  return (
    <SortableContext
      items={item.tiles.map((t) => t.key)}
      strategy={rectSortingStrategy}
    >
      <div
        ref={setNodeRef}
        className={`doc-row min-h-16 rounded-[4px] ${isOver ? "bg-primary-tint" : ""}`}
      >
        {item.tiles.map((tile, i) => (
          <SortableCell key={tile.key} id={tile.key} width={widths[i]}>
            {renderTile(tile)}
          </SortableCell>
        ))}
      </div>
    </SortableContext>
  );
}

function SortableCell({
  id,
  width,
  children,
}: {
  id: string;
  width: number;
  children: React.ReactNode;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id });
  return (
    <div
      ref={setNodeRef}
      style={{
        ...widthStyle(width),
        transform: CSS.Translate.toString(transform),
        transition,
        zIndex: isDragging ? 20 : undefined,
      }}
      className={isDragging ? "opacity-70" : undefined}
    >
      <DragHandleContext.Provider
        value={{
          ref: setActivatorNodeRef,
          props: { ...attributes, ...listeners },
        }}
      >
        {children}
      </DragHandleContext.Provider>
    </div>
  );
}

const DragHandleContext = createContext<{
  ref: (el: HTMLElement | null) => void;
  props: Record<string, unknown>;
} | null>(null);

function DragHandle({ label }: { label: string }) {
  const handle = useContext(DragHandleContext);
  return (
    <Button
      ref={handle?.ref}
      variant="ghost"
      size="icon-xs"
      aria-label={`Drag ${label}`}
      className="cursor-grab active:cursor-grabbing"
      {...(handle?.props ?? {})}
    >
      <GripVertical />
    </Button>
  );
}

function TileFrame({
  tile,
  model,
  rowKey,
  dashboard,
  readers,
  setModel,
}: {
  tile: Tile;
  model: LayoutModel;
  rowKey: string;
  dashboard: DashboardDetail;
  readers: Map<string, number>;
  setModel: (m: LayoutModel) => void;
}) {
  const block = tile.authoring;
  const title = tileTitle(block);
  const chartTypes = compatibleChartTypes(block);
  const dataset = datasetOf(block);
  const width = typeof block.width === "number" ? block.width : null;
  const set = (patch: Parameters<typeof updateTile>[2]) =>
    setModel(updateTile(model, tile.key, patch));

  return (
    <div className="flex h-full flex-col rounded-[6px] border border-border bg-card">
      <div className="flex flex-wrap items-center gap-1 border-b border-border px-1.5 py-1">
        <DragHandle label={title} />
        {block.type === "chart" || block.type === "stat" ? (
          <input
            aria-label={`${title} title`}
            className="min-w-0 flex-1 rounded-[4px] bg-transparent px-1 text-[12px] text-ink-secondary focus-visible:outline-2 focus-visible:outline-ring"
            value={String(
              block.type === "chart"
                ? (block.title ?? "")
                : (block.label ?? ""),
            )}
            maxLength={200}
            onChange={(e) =>
              set(
                block.type === "chart"
                  ? { title: e.target.value }
                  : { label: e.target.value },
              )
            }
          />
        ) : (
          <span className="flex-1 truncate px-1 text-[12px] text-ink-secondary">
            {title}
          </span>
        )}
        <select
          aria-label={`${title} width`}
          className="h-6 rounded-[4px] border border-border bg-transparent px-1 text-[11px]"
          value={width ?? ""}
          onChange={(e) => set({ width: Number(e.target.value) })}
        >
          {width === null && <option value="">Auto</option>}
          {WIDTH_CHOICES.map((w) => (
            <option key={w} value={w}>
              {w}/12
            </option>
          ))}
        </select>
        {chartTypes.length > 0 && (
          <select
            aria-label={`${title} chart type`}
            className="h-6 rounded-[4px] border border-border bg-transparent px-1 text-[11px]"
            value={String(block.chart_type)}
            onChange={(e) => {
              const chart_type = e.target.value as ChartType;
              const next: LooseBlock = { ...block, chart_type };
              set({
                chart_type,
                stacked: canStack(next)
                  ? (block.stacked as boolean | undefined)
                  : undefined,
              });
            }}
          >
            {chartTypes.map((t) => (
              <option key={t} value={t}>
                {CHART_LABELS[t]}
              </option>
            ))}
          </select>
        )}
        {canStack(block) && (
          <label className="flex items-center gap-1 text-[11px] text-ink-secondary">
            <input
              type="checkbox"
              checked={!!block.stacked}
              onChange={(e) => set({ stacked: e.target.checked || undefined })}
            />
            Stacked
          </label>
        )}
        {dataset && (
          <WidgetFilters
            dashboardId={dashboard.id}
            datasetId={dataset}
            filters={dashboard.filters}
            sharedWith={(readers.get(dataset) ?? 1) - 1}
          />
        )}
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={`Move ${title} to its own row`}
          title="Own row"
          onClick={() => setModel(moveTileToNewRow(model, tile.key, rowKey))}
        >
          <SplitSquareVertical />
        </Button>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={`Remove ${title}`}
          onClick={() => setModel(removeTile(model, tile.key))}
        >
          <Trash2 />
        </Button>
      </div>
      <div className="pointer-events-none flex-1 p-2" aria-hidden>
        {tile.preview ? (
          <BlockRenderer
            block={tile.preview as unknown as Block}
            measure={false}
          />
        ) : (
          <p className="text-[12px] text-ink-tertiary">
            Appears when you finish editing.
          </p>
        )}
      </div>
    </div>
  );
}
