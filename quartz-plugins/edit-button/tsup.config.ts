import { defineConfig } from "tsup";
import type { Plugin } from "esbuild";

// Compile imported .scss files to a CSS string at build time so components can
// do `Component.css = style`. Mirrors the loader the community plugins use.
const scssPlugin: Plugin = {
  name: "scss-loader",
  setup(build) {
    build.onLoad({ filter: /\.scss$/ }, async (args) => {
      const sass = await import("sass");
      const result = sass.compile(args.path);
      return { contents: result.css, loader: "text" };
    });
  },
};

// preact and Quartz internals are provided by the host at runtime — never bundle
// them, or the plugin ships a second copy and hydration/singleton assumptions break.
const SINGLETON_EXTERNALS = [
  "preact",
  "preact/hooks",
  "preact/jsx-runtime",
  "preact/compat",
  "@jackyzha0/quartz",
  "@jackyzha0/quartz/*",
];

export default defineConfig({
  entry: {
    index: "src/index.ts",
    "components/index": "src/components/index.ts",
  },
  format: ["esm"],
  dts: true,
  tsconfig: "tsconfig.build.json",
  sourcemap: true,
  clean: true,
  treeshake: true,
  target: "es2022",
  splitting: false,
  // No catch-all noExternal: the only runtime dep (preact) is provided by the
  // Quartz host. .scss is inlined as a string by the loader above.
  external: SINGLETON_EXTERNALS,
  outDir: "dist",
  platform: "node",
  esbuildOptions(options) {
    options.jsx = "automatic";
    options.jsxImportSource = "preact";
  },
  esbuildPlugins: [scssPlugin],
});
