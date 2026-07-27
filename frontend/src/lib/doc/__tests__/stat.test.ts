import { describe, expect, it } from "vitest";

import { deltaTone } from "../stat";

describe("deltaTone", () => {
  it("keeps the historical up=good default when direction is absent", () => {
    expect(deltaTone(5)).toBe("good");
    expect(deltaTone(-5)).toBe("bad");
  });

  it("inverts for down_is_good metrics like churn and cost", () => {
    expect(deltaTone(5, "down_is_good")).toBe("bad");
    expect(deltaTone(-5, "down_is_good")).toBe("good");
  });

  it("matches the default when up_is_good is explicit", () => {
    expect(deltaTone(5, "up_is_good")).toBe("good");
    expect(deltaTone(-5, "up_is_good")).toBe("bad");
  });

  it("is neutral for neutral metrics regardless of sign", () => {
    expect(deltaTone(5, "neutral")).toBe("neutral");
    expect(deltaTone(-5, "neutral")).toBe("neutral");
  });

  it("is neutral for a flat or missing delta", () => {
    expect(deltaTone(0)).toBe("neutral");
    expect(deltaTone(0, "down_is_good")).toBe("neutral");
    expect(deltaTone(null)).toBe("neutral");
    expect(deltaTone(null, "down_is_good")).toBe("neutral");
  });
});
