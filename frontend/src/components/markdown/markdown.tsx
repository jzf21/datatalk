"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Block-level markdown -- the `analysis` payloads from /api/analyze and
 * /api/dashboards/{id}/analyze.
 *
 * remark-gfm is required: analysis output uses pipe tables and strikethrough,
 * which the legacy hand-rolled `md()` parsed with a buggy regex.
 *
 * Memoized because a long Q&A thread would otherwise rebuild a unified
 * processor for every turn on every scroll-triggered render.
 */
export const Markdown = memo(function Markdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h2 className="mt-8 text-[20px] font-semibold tracking-[-0.012em] text-ink-primary first:mt-0">
              {children}
            </h2>
          ),
          h2: ({ children }) => (
            <h3 className="mt-8 text-[17px] font-semibold tracking-[-0.008em] text-ink-primary first:mt-0">
              {children}
            </h3>
          ),
          h3: ({ children }) => (
            <h4 className="mt-6 text-[15px] font-semibold text-ink-primary first:mt-0">
              {children}
            </h4>
          ),
          p: ({ children }) => (
            <p className="mt-4 text-[15px] leading-[1.55] text-ink-primary first:mt-0">
              {children}
            </p>
          ),
          ul: ({ children }) => (
            <ul className="mt-4 list-disc space-y-1 pl-5 text-[15px] leading-[1.55] text-ink-primary marker:text-ink-disabled">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="mt-4 list-decimal space-y-1 pl-5 text-[15px] leading-[1.55] text-ink-primary marker:text-ink-disabled">
              {children}
            </ol>
          ),
          strong: ({ children }) => (
            <strong className="font-semibold">{children}</strong>
          ),
          blockquote: ({ children }) => (
            <blockquote className="mt-4 border-l-2 border-border-strong pl-3 text-ink-secondary">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-8 border-border" />,
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-link underline underline-offset-2"
            >
              {children}
            </a>
          ),
          code: ({ children, ...props }) => {
            const inline = !("data-language" in props);
            return inline ? (
              <code className="cite rounded-[2px] bg-muted px-1 py-px text-ink-primary">
                {children}
              </code>
            ) : (
              <code className="cite block text-ink-primary">{children}</code>
            );
          },
          pre: ({ children }) => (
            <pre className="mt-4 overflow-x-auto rounded-[6px] border border-border bg-muted p-3">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="mt-4 overflow-x-auto rounded-[6px] border border-border">
              <table className="w-full border-collapse text-[13px]">
                {children}
              </table>
            </div>
          ),
          th: ({ children }) => (
            <th className="label-caps border-b border-border-strong bg-muted px-3 py-2 text-left text-ink-secondary">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-border px-3 py-2 text-ink-primary">
              {children}
            </td>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
