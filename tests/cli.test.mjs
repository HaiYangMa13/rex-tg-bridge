import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const CLI = path.join(ROOT, "bin", "rex-tg-bridge.js");

test("rex-tg-bridge --version prints semver", () => {
  const res = spawnSync(process.execPath, [CLI, "--version"], { encoding: "utf8" });
  assert.equal(res.status, 0);
  assert.match(res.stdout.trim(), /^\d+\.\d+\.\d+/);
});

test("rex-tg-bridge --help prints usage", () => {
  const res = spawnSync(process.execPath, [CLI, "--help"], { encoding: "utf8" });
  assert.equal(res.status, 0);
  assert.match(res.stdout, /rex-tg-bridge/);
  assert.match(res.stdout, /setup/);
});

test("rex-tg-bridge hooks-path points to python/hooks", () => {
  const res = spawnSync(process.execPath, [CLI, "hooks-path"], { encoding: "utf8" });
  assert.equal(res.status, 0);
  assert.equal(res.stdout.trim(), path.join(ROOT, "python", "hooks"));
});
