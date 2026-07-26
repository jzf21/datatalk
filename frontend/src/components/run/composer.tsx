"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

/**
 * The hero composer -- the one elevated object on the page.
 *
 * An editorial serif prompt sits above a soft card; the controls live inside the
 * card's footer so the whole thing reads as a single object. See DESIGN.md §4.
 * `heading`/`subheading` are optional so the composer can also be dropped into a
 * denser context (e.g. Analyze) without the hero.
 */
export function Composer({
  heading,
  subheading,
  placeholder,
  examples = [],
  submitLabel,
  disabled,
  onSubmit,
  initialValue = "",
}: {
  heading?: string;
  subheading?: string;
  placeholder: string;
  examples?: string[];
  submitLabel: string;
  disabled?: boolean;
  onSubmit: (request: string, useMemory: boolean) => void;
  initialValue?: string;
}) {
  const [text, setText] = useState(initialValue);
  const [useMemory, setUseMemory] = useState(true);

  const submit = () => {
    const trimmed = text.trim();
    if (trimmed && !disabled) onSubmit(trimmed, useMemory);
  };

  return (
    <div className="mx-auto w-full max-w-[640px]">
      {heading && (
        <div className="mb-6 text-center">
          <h2 className="display display-lg">{heading}</h2>
          {subheading && (
            <p className="display mt-2 text-[17px] leading-snug text-ink-tertiary italic">
              {subheading}
            </p>
          )}
        </div>
      )}

      <div className="hero-composer px-2 pb-2 pt-1">
        <Label htmlFor="request" className="sr-only">
          {heading ?? "What do you want to know?"}
        </Label>
        <Textarea
          id="request"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          }}
          placeholder={placeholder}
          rows={3}
          className="resize-none border-0 bg-transparent px-3 pt-3 text-[15px] shadow-none focus-visible:ring-0 dark:bg-transparent"
        />

        <div className="flex items-center justify-between gap-4 px-2 pb-1">
          <div className="flex items-center gap-2">
            <Switch
              id="use-memory"
              checked={useMemory}
              onCheckedChange={setUseMemory}
            />
            <Label
              htmlFor="use-memory"
              className="text-[13px] text-ink-secondary"
            >
              Apply house rules
            </Label>
          </div>

          <Button onClick={submit} disabled={disabled || !text.trim()}>
            {submitLabel}
            <kbd className="ml-1.5 text-[11px] opacity-60">⌘↵</kbd>
          </Button>
        </div>
      </div>

      {examples.length > 0 && !text && (
        <ul className="mt-4 flex flex-wrap justify-center gap-1.5">
          {examples.map((example) => (
            <li key={example}>
              <button
                type="button"
                onClick={() => setText(example)}
                className="rounded-full border border-border bg-card px-3 py-1 text-[12px] text-ink-secondary transition-colors hover:border-border-strong hover:text-ink-primary"
              >
                {example}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
