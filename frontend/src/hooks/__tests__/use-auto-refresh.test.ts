import { describe, expect, it } from "vitest";

import {
  REFRESH_OPTIONS,
  labelFor,
  parseInterval,
  storageKey,
} from "../use-auto-refresh";

describe("parseInterval", () => {
  it("treats absent and empty as off", () => {
    expect(parseInterval(null)).toBeNull();
    expect(parseInterval("")).toBeNull();
  });

  it("accepts every offered interval", () => {
    for (const option of REFRESH_OPTIONS) {
      if (option.ms === null) continue;
      expect(parseInterval(String(option.ms))).toBe(option.ms);
    }
  });

  it("rejects a value outside the offered set", () => {
    // localStorage is user-writable. A hand-edited "1" would otherwise poll a
    // warehouse a thousand times a second.
    expect(parseInterval("1")).toBeNull();
    expect(parseInterval("999")).toBeNull();
  });

  it("rejects nonsense rather than coercing it", () => {
    expect(parseInterval("abc")).toBeNull();
    expect(parseInterval("NaN")).toBeNull();
    expect(parseInterval("Infinity")).toBeNull();
  });
});

describe("labelFor", () => {
  it("names each option, and calls the absent one Off", () => {
    expect(labelFor(null)).toBe("Off");
    expect(labelFor(30_000)).toBe("30s");
    expect(labelFor(12_345)).toBe("Off");
  });
});

describe("storageKey", () => {
  it("is per dashboard, so one choice does not leak to another", () => {
    expect(storageKey(1)).not.toBe(storageKey(2));
  });
});

describe("REFRESH_OPTIONS", () => {
  it("starts at Off, so a dashboard never polls unasked", () => {
    expect(REFRESH_OPTIONS[0].ms).toBeNull();
  });
});
