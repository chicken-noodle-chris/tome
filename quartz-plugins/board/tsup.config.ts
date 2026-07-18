import { defineConfig } from "tsup";
import type { Plugin } from "esbuild";
import path from "path";

// Compile imported .scss to a CSS string, and bundle+minify imported
// `*.inline.ts` client scripts to a self-contained JS string, so components can
// do `Component.css = style` / `Component.afterDOMLoaded = script`. Mirrors the
// loader the community plugins use.
const assetLoaderPlugin: Plugin = {
  name: "asset-loader",
  setup(parentBuild) {
    const absWorkingDir = parentBuild.initialOptions.absWorkingDir ?? process.cwd();

    parentBuild.onLoad({ filter: /\.scss$/ }, async (args) => {
      const sass = await import("sass");
      const result = sass.compile(args.path);
      return { contents: result.css, loader: "text" };
    });

    parentBuild.onLoad({ filter: /\.inline\.ts$/ }, async (args) => {
      const esbuild = await import("esbuild");
      const fs = await import("fs");
      let text = await fs.promises.readFile(args.path, "utf8");
      text = text.replace(/^export default /gm, "");
      text = text.replace(/^export /gm, "");

      const resolveDir = path.dirname(args.path);
      const sourcefile = path.relative(absWorkingDir, args.path);

      const result = await esbuild.build({
        stdin: { contents: text, loader: "ts", resolveDir, sourcefile },
        write: false,
        bundle: true,
        minify: true,
        platform: "browser",
        format: "esm",
        target: "es2020",
        sourcemap: false,
      });

      const js = result.outputFiles?.[0]?.text;
      if (!js) throw new Error(`asset-loader: no JS output for ${args.path}`);
      return { contents: js, loader: "text" };
    });
  },
};

// preact and Quartz internals are provided by the host at runtime — never bundle
// them. Everything else (yaml, etc.) is bundled so the shipped dist is
// self-contained and Quartz can use it without installing dependencies.
const SINGLETON_EXTERNALS = [
  "preact",
  "preact/hooks",
  "preact/jsx-runtime",
  "preact/compat",
  "@jackyzha0/quartz",
  "@jackyzha0/quartz/*",
  // Provided by the Quartz host (it depends on yaml directly); resolved from the
  // host's node_modules at runtime via the installer's peer-linking, so it must
  // not be bundled — its CJS build does a dynamic require("process") that breaks
  // under ESM.
  "yaml",
];

export default defineConfig({
  entry: { index: "src/index.ts" },
  format: ["esm"],
  dts: true,
  tsconfig: "tsconfig.build.json",
  sourcemap: true,
  clean: true,
  treeshake: true,
  target: "es2022",
  splitting: false,
  // No catch-all noExternal: the only runtime deps (preact, yaml) are provided
  // by the Quartz host, so nothing from node_modules should be bundled. .scss
  // and *.inline.ts are inlined as strings by the asset loader below.
  external: SINGLETON_EXTERNALS,
  outDir: "dist",
  platform: "node",
  esbuildOptions(options) {
    options.jsx = "automatic";
    options.jsxImportSource = "preact";
  },
  esbuildPlugins: [assetLoaderPlugin],
});
