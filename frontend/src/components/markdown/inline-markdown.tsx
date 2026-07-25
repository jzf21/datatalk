"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Inline markdown inside `heading.text` and `paragraph.text`.
 *
 * react-markdown compiles to React elements and never touches innerHTML, so
 * raw HTML from the model is rendered as literal text rather than executed --
 * safe by architecture rather than by discipline. `rehype-raw` must never be
 * added here.
 *
 * Everything block-level is unwrapped so the output can be dropped inside an
 * existing <h2> or <p> without nesting a paragraph in a paragraph.
 */
export const InlineMarkdown = memo(function InlineMarkdown({
  children,
}: {
  children: string;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      allowedElements={["p", "em", "strong", "code", "del", "a", "br"]}
      unwrapDisallowed
      components={{
        p: ({ children }) => <>{children}</>,
        code: ({ children }) => (
          <code className="cite rounded-[2px] bg-muted px-1 py-px text-ink-primary">
            {children}
          </code>
        ),
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
      }}
    >
      {children}
    </ReactMarkdown>
  );
});
