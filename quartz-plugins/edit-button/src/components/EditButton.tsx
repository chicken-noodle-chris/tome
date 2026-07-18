import type {
  QuartzComponent,
  QuartzComponentProps,
  QuartzComponentConstructor,
} from "@quartz-community/types";
import fs from "fs";
import path from "path";
import style from "./styles/edit-button.scss";

export interface EditButtonOptions {
  /** Text shown on the button. */
  label: string;
}

const defaultOptions: EditButtonOptions = {
  label: "Edit",
};

/**
 * Turn an absolute filesystem path into a `vscode://file/…` URI, cross-platform.
 * VS Code wants forward slashes and a single leading slash before the path — on
 * Windows that yields `vscode://file/C:/Users/…`, on POSIX `vscode://file/home/…`.
 */
function vscodeUri(absPath: string): string {
  let p = absPath.replace(/\\/g, "/");
  if (!p.startsWith("/")) p = "/" + p;
  // Encode spaces and other unsafe chars per-segment, keeping slashes and the
  // Windows drive colon intact.
  const encoded = p
    .split("/")
    .map((seg) => encodeURIComponent(seg).replace(/%3A/gi, ":"))
    .join("/");
  return "vscode://file" + encoded;
}

export default ((userOpts?: Partial<EditButtonOptions>) => {
  const EditButton: QuartzComponent = ({ fileData, displayClass }: QuartzComponentProps) => {
    const opts = { ...defaultOptions, ...userOpts };
    const filePath = fileData.filePath as string | undefined;
    if (!filePath) return null;

    // `content/` is a junction/symlink to the vault's `wiki/`, so realpath maps
    // the rendered page back to its true source file — that's what VS Code opens.
    // Virtual pages (the board, tag/folder indexes) have no backing file and
    // throw here; render nothing for them.
    let realPath: string;
    try {
      realPath = fs.realpathSync(path.resolve(filePath));
    } catch {
      return null;
    }

    const href = vscodeUri(realPath);
    return (
      <a
        href={href}
        class={`edit-button ${displayClass ?? ""}`}
        title={`Open ${path.basename(realPath)} in VS Code`}
        aria-label={`Open this page's source in VS Code`}
      >
        <svg
          aria-hidden="true"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
        </svg>
        <span>{opts.label}</span>
      </a>
    );
  };

  EditButton.css = style;
  return EditButton;
}) satisfies QuartzComponentConstructor;
