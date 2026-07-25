import { describe, expect, it } from "vitest";

import { readNdjson } from "../stream";
import type { RunEvent } from "../types";

/** A ReadableStream over exactly the byte chunks given. */
function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

const encode = (s: string) => new TextEncoder().encode(s);

async function collect(stream: ReadableStream<Uint8Array>): Promise<RunEvent[]> {
  const events: RunEvent[] = [];
  for await (const event of readNdjson(stream)) events.push(event);
  return events;
}

describe("readNdjson", () => {
  it("reads whole lines from one chunk", async () => {
    const events = await collect(
      streamOf([
        encode(
          '{"kind":"status","data":{"message":"Planning…"}}\n{"kind":"done","data":{}}\n',
        ),
      ]),
    );
    expect(events.map((e) => e.kind)).toEqual(["status", "done"]);
  });

  it("reassembles a line split across chunks", async () => {
    const events = await collect(
      streamOf([
        encode('{"kind":"status","data":{"mess'),
        encode('age":"Querying"}}\n'),
      ]),
    );
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ data: { message: "Querying" } });
  });

  it("does not corrupt a multi-byte character split across chunks", async () => {
    // The whole reason decode(..., {stream: true}) is not optional. ClickHouse
    // data is full of non-ASCII, and without it this test yields U+FFFD.
    const line = '{"kind":"status","data":{"message":"café — 日本"}}\n';
    const bytes = encode(line);
    const chunks: Uint8Array[] = [];
    for (let i = 0; i < bytes.length; i += 5) chunks.push(bytes.slice(i, i + 5));

    const events = await collect(streamOf(chunks));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ data: { message: "café — 日本" } });
  });

  it("flushes a final line that has no trailing newline", async () => {
    const events = await collect(
      streamOf([encode('{"kind":"done","data":{}}')]),
    );
    expect(events.map((e) => e.kind)).toEqual(["done"]);
  });

  it("skips blank lines and malformed JSON without killing the run", async () => {
    const events = await collect(
      streamOf([
        encode('\n{"kind":"status","data":{"message":"a"}}\n'),
        encode("not json at all\n"),
        encode('{"kind":"done","data":{}}\n'),
      ]),
    );
    expect(events.map((e) => e.kind)).toEqual(["status", "done"]);
  });

  it("handles a very large single line spanning many chunks", async () => {
    // The `report` event carries the whole document; a table may hold
    // SQL_MAX_ROWS rows, so one line can be multi-MB.
    const rows = Array.from({ length: 5000 }, (_, i) => [`acct-${i}`, i]);
    const line =
      JSON.stringify({
        kind: "report",
        data: { document: { blocks: [{ type: "table", columns: ["a", "b"], rows }] } },
      }) + "\n";

    const bytes = encode(line);
    const chunks: Uint8Array[] = [];
    for (let i = 0; i < bytes.length; i += 16_384) {
      chunks.push(bytes.slice(i, i + 16_384));
    }
    expect(chunks.length).toBeGreaterThan(1);

    const events = await collect(streamOf(chunks));
    expect(events).toHaveLength(1);
    const event = events[0];
    expect(event.kind).toBe("report");
    if (event.kind !== "report") throw new Error("unreachable");
    const table = event.data.document.blocks[0];
    if (table.type !== "table") throw new Error("expected a table block");
    expect(table.rows).toHaveLength(5000);
  });

  it("yields nothing for an empty stream", async () => {
    expect(await collect(streamOf([]))).toEqual([]);
  });
});
