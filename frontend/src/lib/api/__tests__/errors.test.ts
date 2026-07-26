import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";

import {
  ApiError,
  announceApiError,
  apiErrorMessage,
  describeNetworkError,
} from "../client";

describe("apiErrorMessage", () => {
  it("translates the API's machine codes", () => {
    expect(apiErrorMessage("not_authenticated")).toMatch(/session has expired/i);
    expect(apiErrorMessage("no_connection")).toMatch(/data source/i);
    expect(apiErrorMessage("no_org")).toMatch(/workspace/i);
    expect(apiErrorMessage("forbidden")).toMatch(/permission/i);
  });

  it("passes unknown detail through rather than guessing", () => {
    expect(apiErrorMessage("some_new_code")).toBe("some_new_code");
  });
});

describe("describeNetworkError", () => {
  it("translates an ApiError instead of showing the raw slug", () => {
    // The regression: a 401 mid-run used to render the literal
    // "not_authenticated" in the run's error banner.
    const message = describeNetworkError(new ApiError(401, "not_authenticated"));
    expect(message).not.toBe("not_authenticated");
    expect(message).toMatch(/session has expired/i);
  });

  it("names the two-server failure mode for a bare TypeError", () => {
    expect(describeNetworkError(new TypeError("Failed to fetch"))).toMatch(
      /Could not reach the DataTalk API/,
    );
  });

  it("reports an abort as a stop, not a failure", () => {
    const abort = new DOMException("aborted", "AbortError");
    expect(describeNetworkError(abort)).toBe("Stopped.");
  });
});

describe("announceApiError", () => {
  let events: string[];
  const record = (e: Event) => events.push(e.type);

  // The suite runs in node, and this only needs the event bus half of `window`.
  // EventTarget is a global in Node 18+, so that costs no extra dependency.
  beforeEach(() => {
    events = [];
    const bus = new EventTarget();
    vi.stubGlobal("window", bus);
    bus.addEventListener("datatalk:unauthorized", record);
    bus.addEventListener("datatalk:no-connection", record);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("announces an expired session so the shell can drop to sign-in", () => {
    announceApiError(new ApiError(401, "not_authenticated"));
    expect(events).toEqual(["datatalk:unauthorized"]);
  });

  it("announces a missing connection so the shell can open settings", () => {
    announceApiError(new ApiError(409, "no_connection"));
    expect(events).toEqual(["datatalk:no-connection"]);
  });

  it("stays quiet for an unrelated 409", () => {
    announceApiError(new ApiError(409, "some_other_conflict"));
    expect(events).toEqual([]);
  });

  it("stays quiet for ordinary failures", () => {
    announceApiError(new ApiError(400, "Empty request."));
    announceApiError(new ApiError(404, "not_found"));
    announceApiError(new ApiError(500, "boom"));
    expect(events).toEqual([]);
  });
});
