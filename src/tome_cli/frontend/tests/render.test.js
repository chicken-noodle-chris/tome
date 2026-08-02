import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { parseFrontmatter, renderMarkdown, stripLeadingH1 } from "../render.js";

describe("parseFrontmatter", () => {
  test("no frontmatter delimiters returns the whole input as body", () => {
    const raw = "# Just a heading\n\nbody text";
    assert.deepEqual(parseFrontmatter(raw), { frontmatter: {}, body: raw });
  });

  test("parses scalar key/value fields and preserves the body", () => {
    const raw = "---\ntitle: Hello\ntype: note\n---\nBody here\n";
    const { frontmatter, body } = parseFrontmatter(raw);
    assert.deepEqual(frontmatter, { title: "Hello", type: "note" });
    assert.equal(body, "Body here\n");
  });

  test("unquotes single- and double-quoted scalar values", () => {
    const raw = '---\na: "double"\nb: \'single\'\n---\nB';
    assert.deepEqual(parseFrontmatter(raw).frontmatter, { a: "double", b: "single" });
  });

  test("parses an inline bracketed list, unquoting each item", () => {
    const raw = '---\ntags: [a, "b c", d]\n---\nB';
    assert.deepEqual(parseFrontmatter(raw).frontmatter, { tags: ["a", "b c", "d"] });
  });

  test("parses a block list (empty key followed by '  - item' lines)", () => {
    const raw = "---\ntags:\n  - one\n  - two\ntitle: T\n---\nBody";
    assert.deepEqual(parseFrontmatter(raw).frontmatter, { tags: ["one", "two"], title: "T" });
  });

  test("blank lines inside the frontmatter block are skipped", () => {
    const raw = "---\ntitle: T\n\ntype: note\n---\nBody";
    assert.deepEqual(parseFrontmatter(raw).frontmatter, { title: "T", type: "note" });
  });

  test("handles CRLF line endings identically to LF", () => {
    const raw = "---\r\ntitle: CRLF\r\n---\r\nBody\r\n";
    const { frontmatter, body } = parseFrontmatter(raw);
    assert.deepEqual(frontmatter, { title: "CRLF" });
    assert.equal(body, "Body\r\n");
  });

  test("a key with no value and no following list lines is an empty array", () => {
    const raw = "---\ntags:\n---\nB";
    assert.deepEqual(parseFrontmatter(raw).frontmatter, { tags: [] });
  });
});

describe("renderMarkdown", () => {
  test("renders plain markdown to HTML", () => {
    assert.equal(renderMarkdown("# Hello"), "<h1>Hello</h1>\n");
  });

  test("a bare wikilink renders the target's title as link text, with the slug in the title attribute", () => {
    const html = renderMarkdown("See [[my-page]] for more.", (slug) =>
      slug === "my-page" ? { href: "/page/my-page", title: "My Page" } : null,
    );
    assert.equal(
      html,
      '<p>See <a class="wikilink" href="/page/my-page" title="my-page">My Page</a> for more.</p>\n',
    );
  });

  test("a piped wikilink keeps the author's label verbatim — the title lookup only fills bare links", () => {
    const html = renderMarkdown("[[my-page|Custom Label]]", (slug) =>
      slug === "my-page" ? { href: "/x", title: "My Page" } : null,
    );
    assert.equal(html, '<p><a class="wikilink" href="/x" title="my-page">Custom Label</a></p>\n');
  });

  test("falls back to the slug as label when the resolver has no title", () => {
    const html = renderMarkdown("[[my-page]]", (slug) => (slug === "my-page" ? { href: "/x" } : null));
    assert.equal(html, '<p><a class="wikilink" href="/x" title="my-page">my-page</a></p>\n');
  });

  test("an unresolved wikilink renders as broken, without needing an explicit resolver", () => {
    const html = renderMarkdown("[[missing]]");
    assert.equal(
      html,
      '<p><a class="wikilink wikilink--broken" href="/page/missing" title="no page: missing">missing</a></p>\n',
    );
  });

  test("escapes HTML-special characters in both the label and the broken-link title", () => {
    const html = renderMarkdown("[[target|A & B]]");
    assert.ok(html.includes("A &amp; B"));
    assert.ok(!html.includes("A & B\""));
  });

  test("a wikilink-shaped string inside a code span is left as literal text, not linkified", () => {
    const html = renderMarkdown("`[[not-a-link]]`");
    assert.equal(html, "<p><code>[[not-a-link]]</code></p>\n");
    assert.ok(!html.includes("wikilink"));
  });
});

describe("stripLeadingH1 ([[browse-ui-polish]], AC3 — no duplicate <h1>)", () => {
  test("drops a leading H1 matching the title, and the blank line after it", () => {
    assert.equal(stripLeadingH1("# My Page\n\nBody text", "My Page"), "Body text");
  });

  test("compares trimmed and case-insensitive", () => {
    assert.equal(stripLeadingH1("#   My Page  \n\nBody", "  my page  "), "Body");
  });

  test("leaves the body alone when the leading H1 says something else", () => {
    const body = "# A Different Heading\n\nBody";
    assert.equal(stripLeadingH1(body, "My Page"), body);
  });

  test("leaves the body alone when there is no leading H1 at all", () => {
    const body = "Just a paragraph, no heading.";
    assert.equal(stripLeadingH1(body, "My Page"), body);
  });

  test("tolerates CRLF line endings", () => {
    assert.equal(stripLeadingH1("# My Page\r\n\r\nBody\r\n", "My Page"), "Body\r\n");
  });

  test("tolerates leading blank lines before the heading", () => {
    assert.equal(stripLeadingH1("\n\n# My Page\n\nBody", "My Page"), "Body");
  });

  test("a heading with no title given is left alone", () => {
    const body = "# My Page\n\nBody";
    assert.equal(stripLeadingH1(body, ""), body);
    assert.equal(stripLeadingH1(body, undefined), body);
  });

  test("an H2 (or deeper) is never treated as the duplicate — only a leading H1", () => {
    const body = "## My Page\n\nBody";
    assert.equal(stripLeadingH1(body, "My Page"), body);
  });
});
