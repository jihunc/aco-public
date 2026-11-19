// Real FastAPI serialization OpenAPI -> maintained Hey API TypeScript generator.
import { createClient } from "@hey-api/openapi-ts";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const temp = mkdtempSync(join(tmpdir(), "aco-openapi-"));
try {
  const schema = execFileSync(
    join(root, ".venv/bin/python"),
    [
      "-c",
      "import json; from aco.api.app import create_app; from aco.core.settings import Settings; print(json.dumps(create_app(Settings()).openapi()))",
    ],
    { cwd: root, encoding: "utf8" },
  );
  const input = join(temp, "openapi.json");
  writeFileSync(input, schema);
  await createClient({
    input,
    output: join(temp, "types"),
    plugins: ["@hey-api/typescript"],
  });
  const generated = readFileSync(join(temp, "types/types.gen.ts"), "utf8");
  const target = join(root, "web/src/lib/generated-api.ts");
  if (process.argv.includes("--check")) {
    if (readFileSync(target, "utf8") !== generated)
      throw new Error(
        "Generated API drift: run node web/scripts/generate-api.mjs",
      );
    process.stdout.write("Generated API matches actual FastAPI OpenAPI.\n");
  } else writeFileSync(target, generated);
} finally {
  rmSync(temp, { recursive: true, force: true });
}
