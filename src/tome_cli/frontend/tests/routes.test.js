import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { hrefFor, routeFromPath, slugFromHref, taskFromHref } from "../routes.js";

describe("hrefFor — the one URL writer ([[page-routes]])", () => {
  test("each base view writes its own path", () => {
    assert.equal(hrefFor({ view: "home" }), "/");
    assert.equal(hrefFor({ view: "board" }), "/tasks");
    assert.equal(hrefFor({ view: "log" }), "/log");
    assert.equal(hrefFor({ view: "chains" }), "/chains");
    assert.equal(hrefFor({ view: "page", slug: "some-page" }), "/page/some-page");
  });

  test("the page view with no slug falls back to the hub, mirroring showPage()", () => {
    assert.equal(hrefFor({ view: "page", slug: null }), "/");
  });

  test("an unknown view — and no argument at all — is the hub", () => {
    assert.equal(hrefFor({ view: "bogus" }), "/");
    assert.equal(hrefFor(), "/");
  });

  test("the task panel is a query parameter on whichever base is showing", () => {
    assert.equal(hrefFor({ view: "board", task: "task-9" }), "/tasks?task=task-9");
    assert.equal(hrefFor({ view: "chains", task: "task-9" }), "/chains?task=task-9");
    assert.equal(hrefFor({ view: "home", task: "task-9" }), "/?task=task-9");
  });

  test("slug and task are both encoded", () => {
    assert.equal(hrefFor({ view: "page", slug: "a b" }), "/page/a%20b");
    assert.equal(hrefFor({ view: "board", task: "a&b" }), "/tasks?task=a%26b");
  });
});

describe("routeFromPath — the inverse", () => {
  test("round-trips every base view through hrefFor", () => {
    for (const spec of [
      { view: "home", slug: null },
      { view: "board", slug: null },
      { view: "log", slug: null },
      { view: "chains", slug: null },
      { view: "page", slug: "some-page" },
    ]) {
      assert.deepEqual(routeFromPath(hrefFor(spec)), spec, spec.view);
    }
  });

  // A static host resolves /tasks to tasks/index.html and leaves the browser
  // on /tasks/ — the same route, spelled the way that host spells it.
  test("a trailing slash is the same route", () => {
    assert.deepEqual(routeFromPath("/tasks/"), { view: "board", slug: null });
    assert.deepEqual(routeFromPath("/log/"), { view: "log", slug: null });
    assert.deepEqual(routeFromPath("/chains/"), { view: "chains", slug: null });
    assert.deepEqual(routeFromPath("/page/some-page/"), { view: "page", slug: "some-page" });
  });

  test("an encoded slug comes back decoded", () => {
    assert.deepEqual(routeFromPath("/page/a%20b"), { view: "page", slug: "a b" });
  });

  test("unrecognized paths land on the hub, as an unknown ?view value used to", () => {
    for (const path of ["/bogus", "/page", "/page/", "/tasks/extra", "", null]) {
      assert.deepEqual(routeFromPath(path), { view: "home", slug: null }, String(path));
    }
  });

  // Only the first segment: a slug is one kebab-case segment, so anything
  // deeper is a malformed link rather than a nested page.
  test("a deeper /page/ path keeps only the first segment as the slug", () => {
    assert.deepEqual(routeFromPath("/page/real-slug/extra"), { view: "page", slug: "real-slug" });
  });
});

describe("href readers — what onContentClick uses", () => {
  test("slugFromHref reads /page/<slug>, and nothing else", () => {
    assert.equal(slugFromHref("/page/known"), "known");
    assert.equal(slugFromHref("/page/known/"), "known");
    assert.equal(slugFromHref("/page/known?task=task-1"), "known");
    assert.equal(slugFromHref("/tasks"), null);
    assert.equal(slugFromHref("/page/"), null);
    assert.equal(slugFromHref(""), null);
  });

  test("taskFromHref reads the panel parameter off any base", () => {
    assert.equal(taskFromHref("/tasks?task=task-9"), "task-9");
    assert.equal(taskFromHref("/chains?task=task-9"), "task-9");
    assert.equal(taskFromHref("/?task=task-9"), "task-9");
    assert.equal(taskFromHref("/tasks"), null);
    assert.equal(taskFromHref("/page/known"), null);
  });

  test("taskFromHref decodes, matching hrefFor's encode", () => {
    assert.equal(taskFromHref(hrefFor({ view: "board", task: "a&b" })), "a&b");
  });
});
