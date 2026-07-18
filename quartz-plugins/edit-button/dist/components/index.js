import fs from 'fs';
import path from 'path';
import { jsxs, jsx } from 'preact/jsx-runtime';

// src/components/EditButton.tsx

// src/components/styles/edit-button.scss
var edit_button_default = ".edit-button {\n  display: inline-flex;\n  align-items: center;\n  gap: 0.4rem;\n  padding: 0.25rem 0.6rem;\n  border: 1px solid var(--lightgray);\n  border-radius: 5px;\n  font-size: 0.85rem;\n  line-height: 1;\n  color: var(--darkgray);\n  background: var(--light);\n  text-decoration: none;\n  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;\n}\n.edit-button > svg {\n  flex-shrink: 0;\n}\n.edit-button:hover {\n  background: var(--lightgray);\n  border-color: var(--gray);\n  color: var(--dark);\n}";
var defaultOptions = {
  label: "Edit"
};
function vscodeUri(absPath) {
  let p = absPath.replace(/\\/g, "/");
  if (!p.startsWith("/")) p = "/" + p;
  const encoded = p.split("/").map((seg) => encodeURIComponent(seg).replace(/%3A/gi, ":")).join("/");
  return "vscode://file" + encoded;
}
var EditButton_default = ((userOpts) => {
  const EditButton = ({ fileData, displayClass }) => {
    const opts = { ...defaultOptions, ...userOpts };
    const filePath = fileData.filePath;
    if (!filePath) return null;
    let realPath;
    try {
      realPath = fs.realpathSync(path.resolve(filePath));
    } catch {
      return null;
    }
    const href = vscodeUri(realPath);
    return /* @__PURE__ */ jsxs(
      "a",
      {
        href,
        class: `edit-button ${displayClass ?? ""}`,
        title: `Open ${path.basename(realPath)} in VS Code`,
        "aria-label": `Open this page's source in VS Code`,
        children: [
          /* @__PURE__ */ jsxs(
            "svg",
            {
              "aria-hidden": "true",
              width: "14",
              height: "14",
              viewBox: "0 0 24 24",
              fill: "none",
              stroke: "currentColor",
              "stroke-width": "2",
              "stroke-linecap": "round",
              "stroke-linejoin": "round",
              children: [
                /* @__PURE__ */ jsx("path", { d: "M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" }),
                /* @__PURE__ */ jsx("path", { d: "M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" })
              ]
            }
          ),
          /* @__PURE__ */ jsx("span", { children: opts.label })
        ]
      }
    );
  };
  EditButton.css = edit_button_default;
  return EditButton;
});

export { EditButton_default as EditButton };
//# sourceMappingURL=index.js.map
//# sourceMappingURL=index.js.map