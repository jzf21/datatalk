"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

export function Composer({
  placeholder,
  examples = [],
  submitLabel,
  disabled,
  onSubmit,
  initialValue = "",
}: {
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
    <div className="space-y-3">
      <div>
        <Label htmlFor="request" className="label-caps text-ink-secondary">
          What do you want to know?
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
          className="mt-1.5 resize-y bg-muted text-[15px]"
        />
      </div>

      {examples.length > 0 && !text && (
        <ul className="flex flex-wrap gap-1.5">
          {examples.map((example) => (
            <li key={example}>
              <button
                type="button"
                onClick={() => setText(example)}
                className="rounded-[4px] border border-border bg-card px-2 py-1 text-[12px] text-ink-secondary hover:border-border-strong hover:text-ink-primary"
              >
                {example}
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <Switch
            id="use-memory"
            checked={useMemory}
            onCheckedChange={setUseMemory}
          />
          <Label htmlFor="use-memory" className="text-[13px] text-ink-secondary">
            Apply house rules
          </Label>
        </div>

        <Button onClick={submit} disabled={disabled || !text.trim()}>
          {submitLabel}
          <kbd className="ml-1.5 text-[11px] opacity-60">⌘↵</kbd>
        </Button>
      </div>
    </div>
  );
}
