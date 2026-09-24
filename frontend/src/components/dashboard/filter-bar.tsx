"use client";

import { CalendarRange, Check, ChevronDown, IterationCw, ListFilter } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { FilterDef, FilterValue, FilterValues } from "@/lib/api/types";
import {
  DATE_PRESETS,
  describeSprint,
  isDateValue,
  isDimensionValue,
  isSprintValue,
} from "@/lib/dashboards/filters";
import { cn } from "@/lib/utils";

/**
 * The controls above a dashboard's grid.
 *
 * Built on `popover` + `command`, which were already installed and used
 * nowhere. A Radix Select would have been the obvious reach, but it can do
 * neither multi-select nor search, and a Region filter with two hundred distinct
 * values needs both -- so it would have meant two dropdown idioms in one bar.
 */
export function FilterBar({
  defs,
  values,
  onChange,
  onReset,
  isDefault,
  unfiltered,
}: {
  defs: FilterDef[];
  values: FilterValues;
  onChange: (id: string, value: FilterValue) => void;
  onReset: () => void;
  isDefault: boolean;
  unfiltered: string[];
}) {
  // No filters configured -- render nothing at all, so the dozens of dashboards
  // that predate this feature do not grow an empty bar.
  if (!defs.length) return null;

  return (
    <div className="sticky top-14 z-10 -mx-6 mb-2 flex flex-wrap items-center gap-2 border-b border-border bg-background/95 px-6 py-2 backdrop-blur">
      {defs.map((def) => {
        const props = {
          def,
          value: values[def.id],
          onChange: (v: FilterValue) => onChange(def.id, v),
        };
        if (def.kind === "date_range") return <DateRangeFilter key={def.id} {...props} />;
        if (def.kind === "sprint") return <SprintFilter key={def.id} {...props} />;
        return <DimensionFilter key={def.id} {...props} />;
      })}

      {!isDefault && (
        <Button variant="ghost" size="sm" onClick={onReset}>
          Reset
        </Button>
      )}

      {unfiltered.length > 0 && (
        <span className="ml-auto text-[12px] text-ink-tertiary">
          {unfiltered.length} widget{unfiltered.length === 1 ? "" : "s"} ignore
          these filters
        </span>
      )}
    </div>
  );
}

function TriggerButton({
  icon,
  label,
  active,
}: {
  icon: React.ReactNode;
  label: string;
  active: boolean;
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      className={cn(active && "border-ink-tertiary")}
    >
      {icon}
      {label}
      <ChevronDown className="size-3 text-ink-tertiary" />
    </Button>
  );
}

function DateRangeFilter({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: FilterValue | undefined;
  onChange: (v: FilterValue) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = isDateValue(value) ? value : {};
  const preset = DATE_PRESETS.find((p) => p.id === current.preset);
  const isCustom = current.preset === "custom";
  const label = isCustom
    ? `${current.from ?? "…"} → ${current.to ?? "…"}`
    : (preset?.label ?? "Any time");

  // Custom dates are held locally and committed once, on Apply. Writing on every
  // keystroke would push a history entry per character typed -- so Back would no
  // longer undo one deliberate change -- and would start, then abort, a
  // warehouse refresh for each half-typed date.
  const [draftFrom, setDraftFrom] = useState(current.from ?? "");
  const [draftTo, setDraftTo] = useState(current.to ?? "");
  const draftChanged =
    draftFrom !== (current.from ?? "") || draftTo !== (current.to ?? "");

  const applyCustom = () => {
    if (!draftFrom && !draftTo) return;
    onChange({ preset: "custom", from: draftFrom, to: draftTo });
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <span>
          <TriggerButton
            icon={<CalendarRange className="size-3.5" />}
            label={`${def.label}: ${label}`}
            active={!!current.preset}
          />
        </span>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-0">
        <Command>
          <CommandList>
            <CommandGroup>
              {DATE_PRESETS.map((p) => (
                <CommandItem
                  key={p.id}
                  onSelect={() => {
                    onChange({ preset: p.id });
                    setOpen(false);
                  }}
                  className="justify-between"
                >
                  {p.label}
                  {p.id === current.preset && <Check className="size-3.5" />}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
        {/*
          Native date inputs rather than a calendar component: react-day-picker
          and a date library would be the only new dependencies this whole
          feature needs, and the native control is keyboard-accessible,
          locale-aware, and hands mobile the OS picker for free.
        */}
        <div className="space-y-2 border-t border-border p-3">
          <Label className="label-caps text-ink-tertiary">Custom range</Label>
          <div className="flex items-center gap-2">
            <Input
              type="date"
              aria-label={`${def.label} from`}
              value={draftFrom}
              onChange={(e) => setDraftFrom(e.target.value)}
            />
            <span className="text-ink-tertiary">–</span>
            <Input
              type="date"
              aria-label={`${def.label} to`}
              value={draftTo}
              onChange={(e) => setDraftTo(e.target.value)}
            />
          </div>
          <Button
            size="sm"
            className="w-full"
            disabled={(!draftFrom && !draftTo) || !draftChanged}
            onClick={applyCustom}
          >
            Apply
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function DimensionFilter({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: FilterValue | undefined;
  onChange: (v: FilterValue) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = isDimensionValue(value) ? value : { all: true };
  const selected = new Set(current.values ?? []);
  const options = def.options ?? [];
  // Labels are display only: the allowlist, the URL and the request all carry
  // the value (an account id, an epic key), never the label.
  const labelOf = (option: string) => def.option_labels?.[option] ?? option;

  const label = current.all
    ? "All"
    : selected.size === 1
      ? labelOf([...selected][0])
      : `${selected.size} selected`;

  const toggle = (option: string) => {
    const next = new Set(selected);
    if (next.has(option)) next.delete(option);
    else next.add(option);
    // An empty selection means "no constraint", which is what All already says.
    onChange(next.size ? { values: [...next].sort() } : { all: true });
  };

  if (!options.length) {
    return (
      <Button
        variant="outline"
        size="sm"
        disabled
        // Not "refresh to populate": nothing on the refresh path re-probes
        // options. They are gathered when filters are saved, so re-saving is
        // genuinely the way to retry a probe that failed.
        title="No values were found for this column. Re-save the dashboard's filters to try again."
      >
        <ListFilter className="size-3.5" />
        {def.label} — no values
      </Button>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <span>
          <TriggerButton
            icon={<ListFilter className="size-3.5" />}
            label={`${def.label}: ${label}`}
            active={!current.all}
          />
        </span>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-0">
        <Command>
          <CommandInput placeholder={`Search ${def.label.toLowerCase()}…`} />
          <CommandList>
            <CommandEmpty>No matches.</CommandEmpty>
            <CommandGroup>
              <CommandItem
                onSelect={() => onChange({ all: true })}
                className="justify-between"
              >
                All
                {current.all && <Check className="size-3.5" />}
              </CommandItem>
              {options.map((option) => (
                <CommandItem
                  key={option}
                  // cmdk searches `value`: the label is what people type, the
                  // raw value keeps two identically-named people distinct.
                  value={`${labelOf(option)} ${option}`}
                  onSelect={() => toggle(option)}
                  className="justify-between"
                >
                  <span className="truncate">{labelOf(option)}</span>
                  {selected.has(option) && <Check className="size-3.5 shrink-0" />}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
        {def.options_truncated && (
          <p className="border-t border-border px-3 py-2 text-[12px] text-ink-tertiary">
            Showing the first {options.length} values found. Selecting “All” is
            not limited to this list.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}

const SPRINT_GROUPS: { id: string; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "closed", label: "Closed" },
  { id: "future", label: "Future" },
];
const LAST_N_CHOICES = [3, 6, 12];

/**
 * Sprints by rule ("active", "last 6") or by name.
 *
 * Rules are the default because they keep meaning the right thing as sprints
 * roll over: a saved link to "the active sprint" should not freeze on the
 * sprint that happened to be active when it was copied.
 */
function SprintFilter({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: FilterValue | undefined;
  onChange: (v: FilterValue) => void;
}) {
  const [open, setOpen] = useState(false);
  const single = def.multi === false;
  const current = isSprintValue(value) ? value : { mode: "active" as const };
  const picked = new Set(current.mode === "ids" ? (current.ids ?? []) : []);
  const options = def.options ?? [];
  const labelOf = (id: string) => def.option_labels?.[id] ?? `Sprint ${id}`;

  const choose = (v: FilterValue) => {
    onChange(v);
    setOpen(false);
  };

  const toggle = (id: string) => {
    if (single) return choose({ mode: "ids", ids: [id] });
    const next = new Set(picked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next.size ? { mode: "ids", ids: [...next].sort() } : { mode: "active" });
  };

  const rules: { key: string; label: string; value: FilterValue; on: boolean }[] = [
    { key: "active", label: "Active sprint", value: { mode: "active" }, on: current.mode === "active" },
    ...(single
      ? [{
          key: "last",
          label: "Last completed sprint",
          value: { mode: "last_n" as const, n: 1 },
          on: current.mode === "last_n",
        }]
      : LAST_N_CHOICES.map((n) => ({
          key: `last${n}`,
          label: `Last ${n} sprints`,
          value: { mode: "last_n" as const, n },
          on: current.mode === "last_n" && current.n === n,
        }))),
    ...(single
      ? []
      : [{ key: "all", label: "All sprints", value: { mode: "all" as const }, on: current.mode === "all" }]),
  ];

  const grouped = SPRINT_GROUPS.map((g) => ({
    ...g,
    ids: options.filter((id) => (def.option_groups?.[id] ?? "closed") === g.id),
  })).filter((g) => g.ids.length);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <span>
          <TriggerButton
            icon={<IterationCw className="size-3.5" />}
            label={`${def.label}: ${describeSprint(def, current)}`}
            active={current.mode !== (def.default as { mode?: string } | null)?.mode}
          />
        </span>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-0">
        <Command>
          <CommandInput placeholder="Search sprints…" />
          <CommandList>
            <CommandEmpty>No matching sprints.</CommandEmpty>
            <CommandGroup>
              {rules.map((r) => (
                <CommandItem
                  key={r.key}
                  value={r.label}
                  onSelect={() => choose(r.value)}
                  className="justify-between"
                >
                  {r.label}
                  {r.on && <Check className="size-3.5" />}
                </CommandItem>
              ))}
            </CommandGroup>
            {grouped.map((g) => (
              <CommandGroup key={g.id} heading={g.label}>
                {g.ids.map((id) => (
                  <CommandItem
                    key={id}
                    value={`${labelOf(id)} ${id}`}
                    onSelect={() => toggle(id)}
                    className="justify-between"
                  >
                    <span className="truncate">{labelOf(id)}</span>
                    {picked.has(id) && <Check className="size-3.5 shrink-0" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
        {!options.length && (
          <p className="border-t border-border px-3 py-2 text-[12px] text-ink-tertiary">
            No sprints were found in the synced data.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
