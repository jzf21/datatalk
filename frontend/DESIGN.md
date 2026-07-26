# DataTalk — design guidelines

The product answers questions about a warehouse, and **every number cites the query
behind it**. The interface has one job: make those answers feel *edited* — trustworthy,
calm, and readable — never like raw tool output.

Direction, in one line: **a data broadsheet.** Your warehouse, set like a well-run
report. Warm paper, an editorial serif for the things a person reads, a monospace voice
for the things a machine can prove.

This is a reskin, not a rebuild. The token system in `globals.css` is the source of
truth; this file explains the *intent* so new UI stays in voice.

---

## 1. The three voices

Type does the heavy lifting. Every piece of text belongs to exactly one voice — mixing
them is how the design frays.

| Voice | Face | Where it speaks |
|-------|------|-----------------|
| **Display** | Newsreader (serif) | Hero prompts, page titles, the wordmark, empty-state headlines, section headings. The "read me" voice. |
| **UI** | IBM Plex Sans | Everything interactive: nav, buttons, labels, body copy, form controls. The default. |
| **Data** | IBM Plex Mono (`.cite`) | Anything a machine can prove — dataset ids (`q1`), column names, row counts, SQL, versions, the `RP`/`DB` ledger marks. The "trust me" voice. |

Rules:
- **Display is used with restraint.** A page has *one* serif moment that matters (the
  hero prompt, or the page title) — not a serif on every heading down to h4. If serif is
  everywhere, it's nowhere.
- **Never set numbers a user must trust in the UI or display voice.** Captured values,
  ids, and counts are always `.cite` (mono). This mirrors the backend trust rule: the LLM
  never types numbers, and neither does the chrome.
- Newsreader ships an italic — use it for a single editorial accent (an emphasized word in
  an empty state), never for whole paragraphs.

Type scale (display): 15px title → 20px section → 28px page hero → 40px empty-state hero.
Weights 400/500 only; let size carry hierarchy, not boldness. Tracking tightens as size
grows (`-0.01em` at title, `-0.02em` at hero).

---

## 2. Color — don't touch the palette, respect its jobs

The palette in `globals.css` is validated and deliberate. It is **warm-neutral, single
hue family** for all chrome; color is reserved for data.

- **Paper & ink.** Background is ledger cream (`--background`), surfaces step up by
  *lightness only* (`--card`, `--popover`). Ink has four levels (`--ink-primary` →
  `--ink-disabled`) and all four get used — hierarchy comes from ink level, not from
  boxes and borders.
- **Chrome is colorless.** The sidebar sits on the *same plane* as the canvas, divided by
  a single hairline. Do not introduce a "sidebar surface" or a brand-colored header.
- **One accent: the stamp blue** (`--link` / `--ring`) for links, focus, selection. It is
  deliberately darker than `--chart-1` so it can never be mistaken for a data series.
- **Data color is off-limits for decoration.** `--chart-1…8` and the status hues exist
  only inside charts and status. Never use a chart hue for a button, tag, or accent.
- **No terracotta, no gradient accents, no colored hero.** The warmth comes from the paper
  and the serif, not from a hue. (This is what keeps the cream-serif look from reading as
  a template.)

---

## 3. Depth, shape, motion

- **Flat in-flow, shadow only when floating.** In-flow surfaces (cards, rails, headers)
  never cast a shadow — they're separated by hairlines. Only things that *float* (popovers,
  the hero composer, dialogs) use `--shadow-overlay`.
- **Radius is quiet.** Base `--radius` is 6px. The hero composer is the one place we go
  soft and large (`rounded-2xl`) — it's the signature object, so it earns the roundness.
- **Motion is a courtesy.** 120ms color transitions on interactive elements; the shimmer on
  load; nothing else. `prefers-reduced-motion` is already honored globally — keep it that way.

---

## 4. The signature: the hero composer

The one memorable object. It's how the Claude-app calm enters the product.

```
            Ask your warehouse            ← Newsreader, 28–40px, ink-primary
      a question worth an answer.         ← second line, ink-tertiary (editorial)

   ┌────────────────────────────────────────────┐   ← rounded-2xl, card surface,
   │  e.g. Summarize resolution trends by …      │      --shadow-overlay, hairline
   │                                              │
   │  ⌥ Apply house rules            Generate ⌘↵  │   ← quiet controls INSIDE the card
   └────────────────────────────────────────────┘
        Summarize trends   ·   SLA breaches   ·      ← example chips, ghost, below
```

- The prompt line above the box is **display voice** and is the hero — not a small-caps
  label. Copy is a plain invitation, sentence case, specific: "Ask your warehouse a
  question." Never "Enter your query."
- Controls live *inside* the composer's footer row, muted, so the box reads as one object.
- The box is the only elevated in-flow element on the page. Everything else stays flat.
- Empty states elsewhere (empty library, no connection) borrow the same shape: a serif
  headline + one plain sentence + one action. An empty screen is an invitation to act.

---

## 5. Layout & rhythm

- Content column: `max-width: 1040px`, prose held to `68ch` (the `.doc-measure` rule).
- Sidebar: 264px, icon + label nav, same plane as canvas.
- **More air than before.** Page hero sections get generous vertical space (`py-10`+); the
  Claude look is defined by whitespace as much as by the serif. Density stays only where
  data lives — tables and the library rail are meant to be dense.
- Block grid (`.doc-row`) uses *container* queries, not viewport — the Sources rail changes
  the available width. Don't swap those for `md:`/`lg:` breakpoints.

---

## 6. Writing (copy is design material)

- Write from the user's side of the screen. "House rules," not "memory config."
- Actions name what happens: **Generate**, **Save**, **Ask** — and keep the same word
  through the flow (a "Generate" button produces a report, not a "Submission").
- Errors don't apologize and are never vague: say what happened and the next step. Use the
  stable machine codes' human wording from `lib/api/client.ts` — one place, one voice.
- Sentence case everywhere except `.label-caps` (small-caps utility labels).

---

## 7. Quality floor (non-negotiable)

Responsive to mobile · visible keyboard focus (`--ring`, never removed) · reduced motion
respected · 4.5:1 contrast for text · status never by color alone (dot + label). If a new
component can't meet these, it's not done.
