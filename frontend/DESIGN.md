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
- **Chrome surfaces stay colorless.** The sidebar sits on the *same plane* as the canvas,
  divided by a single hairline. Do not introduce a "sidebar surface" or a brand-colored
  header. Hue enters the chrome only through the two accents below — as a *fill on a
  control*, never as a plane.
- **Two accents, two jobs, and that is the whole list.**
  - **Ledger green** (`--primary`) is *action*: the filled button, the default badge, a
    checked box, a thrown switch. It is deep and low-chroma — `oklch(0.44 0.085 155)`,
    a full 0.09 below `--chart-6`/`--status-good` in both lightness and chroma and 12°
    off their hue — so a button can never be misread as a series mark or as "good".
    Companions: `--primary-hover` (darker, so hover never *fades* toward the paper),
    `--primary-tint` (soft fill), `--primary-text` (green as text on paper, 8.13:1).
  - **Stamp blue** (`--link` / `--ring`) is *navigation and focus*: links, focus rings,
    selection. Deliberately darker than `--chart-1` so it is never a data series.
  - Green fills, blue links. A green link or a blue button is a bug.
- **Data color is off-limits for decoration.** `--chart-1…8` and the status hues exist
  only inside charts and status. Never use a chart hue for a button, tag, or accent —
  and never reach for `--status-good` when you mean `--primary`.
- **No gradient accents, no colored hero, no colored page or sidebar.** The warmth comes
  from the paper and the serif; the green is a mark *on* that paper, not a wash over it.
  (This is what keeps the cream-serif look from reading as a template.)

### 2b. The terminal plane — where color lives

Machine output renders on its own dark surface: **the terminal figure** (`--terminal`,
`--terminal-raised`, the `--terminal-ink` levels, `--terminal-border`). It is a cool
night slate set into the warm page — a screen embedded in print — and it is the *only*
place the UI goes dark. The chrome around it stays paper.

Color on that plane (and its paper-side echoes) has exactly three jobs:

- **Credit / debit** (`--credit` mint, `--debit` soft red): values in *signed* numeric
  columns render as tinted pills. Unsigned measures stay plain ink — coloring every
  number is noise, and the sign character stays in the text so color never carries the
  meaning alone.
- **The pulse teal** (`--pulse` on slate, `--pulse-text` on paper): "verified against a
  captured query." Tick chips on the terminal figure, resolved dataset ids in the run
  log, the live shimmer. It is the trust mark, nothing else.
- **Pending amber** (`--status-warning`, pre-existing): a query in flight, a retry.

None of these are ever used decoratively, and never on buttons, nav, or chrome.

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

## 4. The signatures: the hero composer & the terminal figure

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

The second signature is the **terminal figure** (§2b): data tables sit on the dark
terminal plane, `rounded-[10px]`, no border (the plane defines itself), with a raised
caption band, white-alpha hairlines, and value/status color inside. Use the `.terminal`
component class — it sets `color-scheme: dark` (native scrollbars) and swaps the focus
ring to the pulse teal, since the stamp blue disappears on slate.

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
