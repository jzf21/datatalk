"use client";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { DocumentView } from "@/components/doc/block-renderer";
import { SourcesProvider } from "@/components/doc/sources-context";
import {
  FIXTURE_DASHBOARD,
  FIXTURE_EDGE_CASES,
  FIXTURE_QUERIES,
  FIXTURE_REPORT,
} from "@/mocks/fixtures";

/** Renderer preview against fixtures. No backend required. */
export default function PreviewPage() {
  return (
    <>
      <PageHeader title="Renderer preview" meta="Fixtures only — no backend." />
      <PageBody className="space-y-16">
        <SourcesProvider queries={FIXTURE_QUERIES}>
          <section>
            <h2 className="label-caps mb-4 text-ink-tertiary">Report document</h2>
            <DocumentView document={FIXTURE_REPORT} />
          </section>

          <section>
            <h2 className="label-caps mb-4 text-ink-tertiary">Dashboard grid</h2>
            <DocumentView document={FIXTURE_DASHBOARD} measure={false} id="dash" />
          </section>

          <section>
            <h2 className="label-caps mb-4 text-ink-tertiary">Edge cases</h2>
            <DocumentView document={FIXTURE_EDGE_CASES} measure={false} id="edge" />
          </section>
        </SourcesProvider>
      </PageBody>
    </>
  );
}
