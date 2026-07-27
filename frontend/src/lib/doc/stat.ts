import type { StatDirection } from "@/lib/api/types";

export type DeltaTone = "good" | "bad" | "neutral";

/**
 * The colour of a stat tile's delta. The arrow and the sr-only word always
 * follow the *sign*; only the tone follows goodness, because for churn, cost
 * or latency a fall is the improvement. Absent direction keeps the historical
 * up=good behaviour.
 */
export function deltaTone(
  delta: number | null,
  direction?: StatDirection,
): DeltaTone {
  if (delta === null || delta === 0 || direction === "neutral") return "neutral";
  const goodWhenRising = (direction ?? "up_is_good") === "up_is_good";
  return delta > 0 === goodWhenRising ? "good" : "bad";
}
