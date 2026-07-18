declare module "*.scss" {
  const content: string;
  export default content;
}

declare module "*.inline.ts" {
  const content: string;
  export default content;
}

interface Window {
  addCleanup(fn: (...args: unknown[]) => void): void;
}
