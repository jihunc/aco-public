// Shared module doubles for component tests that load TSX through a custom require.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const requireFromWeb = createRequire(new URL("../package.json", import.meta.url));
const ts = requireFromWeb("typescript");

function transpile(path) {
  const module = { exports: {} };
  const source = readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function("module", "exports", compiled)(module, module.exports);
  return module.exports;
}

export const libFormat = transpile("lib/format.ts");
// Without the workspace lookup provider, labels fall back to the short reference.
export const libInvoiceNumbers = {
  InvoiceRef: ({ id }) => libFormat.shortInvoiceLabel(id),
  InvoiceNumbersProvider: ({ children }) => children,
};
export function libDouble(name) {
  if (name === "@/lib/format") return libFormat;
  if (name === "@/lib/invoice-numbers") return libInvoiceNumbers;
  return undefined;
}
