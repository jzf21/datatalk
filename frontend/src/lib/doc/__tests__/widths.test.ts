import { describe, expect, it } from "vitest";

import { childWidths, mediumWidth } from "../widths";
import type { Block } from "@/lib/api/types";

const bare = (n: number): Block[] =>
  Array.from({ length: n }, () => ({ type: "paragraph", text: "x" }) as Block);

const sized = (...widths: number[]): Block[] =>
  widths.map((width) => ({ type: "paragraph", text: "x", width }) as Block);

describe("childWidths", () => {
  it("splits evenly when nothing is declared", () => {
    expect(childWidths(bare(2))).toEqual([6, 6]);
    expect(childWidths(bare(3))).toEqual([4, 4, 4]);
    expect(childWidths(bare(4))).toEqual([3, 3, 3, 3]);
  });

  it("distributes the remainder instead of leaving a hole", () => {
    // The legacy renderer used round(12/5) = 2 each, summing to 10.
    const five = childWidths(bare(5));
    expect(five).toEqual([3, 3, 2, 2, 2]);
    expect(five.reduce((a, b) => a + b, 0)).toBe(12);

    expect(childWidths(bare(7)).reduce((a, b) => a + b, 0)).toBe(12);
    expect(childWidths(bare(8)).reduce((a, b) => a + b, 0)).toBe(12);
  });

  it("honours declared widths that fit", () => {
    expect(childWidths(sized(3, 3, 6))).toEqual([3, 3, 6]);
  });

  it("scales proportionally when widths exceed 12", () => {
    const widths = childWidths(sized(12, 12));
    expect(widths).toEqual([6, 6]);
    expect(widths.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(12);
  });

  it("clamps out-of-range declared widths", () => {
    // 99 clamps to 12, then [12, 6] scales proportionally back into 12.
    expect(childWidths(sized(99, 6))).toEqual([8, 4]);
    expect(childWidths(sized(0, 6))).toEqual([1, 6]);
  });

  it("gives undeclared children the leftover columns", () => {
    expect(childWidths([...sized(8), ...bare(1)])).toEqual([8, 4]);
    expect(childWidths([...sized(6), ...bare(2)])).toEqual([6, 3, 3]);
  });

  it("never returns a zero or negative span", () => {
    for (let n = 1; n <= 16; n++) {
      expect(childWidths(bare(n)).every((w) => w >= 1)).toBe(true);
    }
  });

  it("handles an empty row", () => {
    expect(childWidths([])).toEqual([]);
  });
});

describe("mediumWidth", () => {
  it("halves a KPI row and expands wider cells to full width", () => {
    expect(mediumWidth(3)).toBe(6); // 4-up KPIs -> 2-up
    expect(mediumWidth(4)).toBe(6);
    expect(mediumWidth(6)).toBe(12); // half-width chart -> full
    expect(mediumWidth(12)).toBe(12);
  });
});
