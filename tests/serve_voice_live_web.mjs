/** Fixed test-only standard Next project; actual copied source, external Daily double. */
import { spawn } from "node:child_process";
import { cp, copyFile, mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const req = createRequire(resolve(root, "web/package.json"));

export async function prepareProject() {
  if (process.env.ACO_TEST_PROFILE !== "voice-live-code")
    throw Error("Explicit contained Voice profile required");
  await mkdir(resolve(root, ".tooling"), { recursive: true });
  const project = await mkdtemp(resolve(root, ".tooling/voice-live-web-"));
  try {
    await cp(resolve(root, "web/src"), resolve(project, "src"), { recursive: true });
    for (const name of ["package.json", "tsconfig.json", "postcss.config.mjs", "next-env.d.ts"])
      await copyFile(resolve(root, "web", name), resolve(project, name));
    await symlink(resolve(root, "web/node_modules"), resolve(project, "node_modules"), "dir");
    await writeFile(resolve(project, "next.config.mjs"),
      `import base from ${JSON.stringify(pathToFileURL(resolve(root, "web/next.config.mjs")).href)};\n` +
      `export default {...base,experimental:{...base.experimental,externalDir:true},webpack(config){` +
      `config.resolve.alias["@daily-co/daily-js"]=${JSON.stringify(resolve(root, "tests/browser_voice_daily.ts"))};` +
      `console.info("VOICE_TEST_DAILY_BOUNDARY_INSTALLED");return config;}};\n`);
    return { project, cleanup: () => rm(project, { recursive: true, force: true }) };
  } catch (error) {
    await rm(project, { recursive: true, force: true });
    throw error;
  }
}

async function serve() {
  const owned = await prepareProject();
  const child = spawn(process.execPath, [req.resolve("next/dist/bin/next"), "dev", "--webpack", "--hostname", "127.0.0.1", "--port", "53000"],
    { cwd: owned.project, env: process.env, stdio: "inherit" });
  for (const signal of ["SIGTERM", "SIGINT"])
    process.once(signal, () => child.kill(signal));
  try {
    process.exitCode = await new Promise((resolveExit, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => resolveExit(code ?? (signal ? 1 : 0)));
    });
  } finally {
    await owned.cleanup();
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  await serve();
