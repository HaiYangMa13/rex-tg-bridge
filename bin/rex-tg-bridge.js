#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = path.resolve(__dirname, "..");
const PY_BRIDGE = path.join(PKG_ROOT, "python", "bridge.py");
const PY_HOOKS_DIR = path.join(PKG_ROOT, "python", "hooks");
const PKG_JSON = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, "package.json"), "utf8"));

function printHelp() {
  console.log(`rex-tg-bridge v${PKG_JSON.version}
24/7 Telegram Forum Topics <-> Rex Server bridge for coding agents.

Usage:
  rex-tg-bridge [start] [--env <file>]     Run the bridge daemon
  rex-tg-bridge setup [options]            Install agent hooks and/or systemd user service
  rex-tg-bridge doctor [--env <file>]      Check rex, python3, hooks, and bridge health
  rex-tg-bridge hooks-path                 Print absolute path to bundled hook scripts
  rex-tg-bridge --version | --help

Setup options:
  --agent <agy|claude|codex|all>           Configure agent hooks (default: agy)
  --systemd                                Install ~/.config/systemd/user/rex-tg-bridge.service
  --env-file <path>                        Env file path (default: ~/.config/rex-tg-bridge/env)

Environment variables:
  TG_BOT_TOKEN       Telegram Bot token from @BotFather (required)
  TG_PROXY           Optional HTTP/HTTPS proxy (e.g. http://127.0.0.1:7890)
  TG_API_BASE        Optional Telegram Bot API base URL (e.g. Cloudflare Worker proxy)
  HOOK_HOST          Local hook listener host (default: 127.0.0.1)
  HOOK_PORT          Local hook listener port (default: 18790)
  STATE_FILE         State JSON path (default: ~/.local/state/rex-tg-bridge/state.json)
  REX_BIN            Path to rex binary (default: auto-detected from PATH)
  CAPTURE_LINES      Default lines for /screen (default: 45)
  LOG_LEVEL          INFO | DEBUG | WARNING (default: INFO)`);
}

function parseEnvFile(filePath) {
  const result = {};
  if (!filePath || !fs.existsSync(filePath)) return result;
  const content = fs.readFileSync(filePath, "utf8");
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    result[key] = val;
  }
  return result;
}

function resolveDefaultEnvFile(explicitPath) {
  if (explicitPath) return path.resolve(explicitPath);
  const candidates = [
    path.join(os.homedir(), ".config", "rex-tg-bridge", "env"),
    "/etc/rex-tg-bridge.env",
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function parseArgs(argv) {
  const args = [...argv];
  let cmd = "start";
  if (args.length > 0 && !args[0].startsWith("-")) {
    cmd = args.shift();
  }
  const opts = {};
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "-h" || a === "--help") opts.help = true;
    else if (a === "-v" || a === "--version") opts.version = true;
    else if (a === "--systemd") opts.systemd = true;
    else if (a === "--env" || a === "--env-file") opts.envFile = args[++i];
    else if (a === "--agent") opts.agent = args[++i];
  }
  return { cmd, opts };
}

function runStart(opts) {
  const envFile = resolveDefaultEnvFile(opts.envFile);
  const fileEnv = parseEnvFile(envFile);
  const mergedEnv = { ...fileEnv, ...process.env };
  // Prefer file values if process.env has empty string
  for (const [k, v] of Object.entries(fileEnv)) {
    if (!process.env[k]) mergedEnv[k] = v;
  }
  const child = spawn("python3", ["-u", PY_BRIDGE], {
    stdio: "inherit",
    env: mergedEnv,
  });
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      if (!child.killed) child.kill(sig);
    });
  }
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    process.exit(code ?? 1);
  });
}

function setupAgyHook() {
  const configDir = path.join(os.homedir(), ".gemini", "config");
  const hooksPath = path.join(configDir, "hooks.json");
  const agyHook = path.join(PY_HOOKS_DIR, "agy_hook.py");
  fs.mkdirSync(configDir, { recursive: true });
  let existing = {};
  if (fs.existsSync(hooksPath)) {
    try {
      existing = JSON.parse(fs.readFileSync(hooksPath, "utf8"));
    } catch (err) {
      throw new Error(`Failed parsing ${hooksPath}: ${err.message}`);
    }
  }
  existing["rex-tg-bridge"] = {
    Stop: [{ type: "command", command: `python3 "${agyHook}"`, timeout: 5 }],
    PreToolUse: [
      {
        matcher: "ask_question",
        hooks: [{ type: "command", command: `python3 "${agyHook}"`, timeout: 5 }],
      },
    ],
  };
  fs.writeFileSync(hooksPath, JSON.stringify(existing, null, 2) + "\n", "utf8");
  console.log(`[ok] Updated agy hooks: ${hooksPath}`);
}

function printClaudeHookHint() {
  const hook = path.join(PY_HOOKS_DIR, "claude_hook.py");
  console.log(`\n[info] Add to ~/.claude/settings.json under "hooks":`);
  console.log(
    JSON.stringify(
      {
        Stop: [{ hooks: [{ type: "command", command: `python3 "${hook}" Stop` }] }],
        Notification: [{ hooks: [{ type: "command", command: `python3 "${hook}" Notification` }] }],
        PermissionRequest: [{ hooks: [{ type: "command", command: `python3 "${hook}" PermissionRequest` }] }],
      },
      null,
      2
    )
  );
}

function printCodexHookHint() {
  const hook = path.join(PY_HOOKS_DIR, "codex_hook.py");
  console.log(`\n[info] Add to ~/.codex/config.toml:`);
  console.log(`notify = ["python3", "${hook}"]`);
}

function setupSystemd(envFilePath) {
  if (process.platform !== "linux") {
    console.log("[warn] --systemd is only supported on Linux.");
    return;
  }
  const envTarget = envFilePath
    ? path.resolve(envFilePath)
    : path.join(os.homedir(), ".config", "rex-tg-bridge", "env");
  fs.mkdirSync(path.dirname(envTarget), { recursive: true });
  if (!fs.existsSync(envTarget)) {
    const example = fs.readFileSync(path.join(PKG_ROOT, ".env.example"), "utf8");
    fs.writeFileSync(envTarget, example, { mode: 0o600 });
    console.log(`[ok] Created env template: ${envTarget} (fill in TG_BOT_TOKEN)`);
  }

  const unitDir = path.join(os.homedir(), ".config", "systemd", "user");
  const unitPath = path.join(unitDir, "rex-tg-bridge.service");
  fs.mkdirSync(unitDir, { recursive: true });

  const pyBin = spawnSync("which", ["python3"], { encoding: "utf8" }).stdout.trim() || "/usr/bin/python3";
  const unitContent = `[Unit]
Description=Rex Server <-> Telegram Forum Topics Bridge
After=rex.service
Wants=rex.service

[Service]
Type=simple
EnvironmentFile=${envTarget}
ExecStart=${pyBin} -u ${PY_BRIDGE}
Restart=always
RestartSec=3
TimeoutStopSec=5

[Install]
WantedBy=default.target
`;
  fs.writeFileSync(unitPath, unitContent, "utf8");
  console.log(`[ok] Wrote systemd user unit: ${unitPath}`);
  spawnSync("systemctl", ["--user", "daemon-reload"], { stdio: "inherit" });
  console.log(`\nNext steps:
  1. Edit ${envTarget} and set TG_BOT_TOKEN (and TG_PROXY if needed)
  2. Enable 24/7 linger (once): loginctl enable-linger $USER
  3. Start service: systemctl --user enable --now rex-tg-bridge.service
  4. Send /bind in your Telegram Forum Topics supergroup`);
}

function runSetup(opts) {
  const agent = (opts.agent || "agy").toLowerCase();
  if (agent === "agy" || agent === "all") setupAgyHook();
  if (agent === "claude" || agent === "all") printClaudeHookHint();
  if (agent === "codex" || agent === "all") printCodexHookHint();
  if (opts.systemd) setupSystemd(opts.envFile);
}

function runDoctor(opts) {
  const check = (label, bin, args) => {
    const r = spawnSync(bin, args, { encoding: "utf8" });
    if (r.status === 0) {
      console.log(`[ok] ${label}: ${(r.stdout || r.stderr).trim().split("\n")[0]}`);
    } else {
      console.log(`[fail] ${label}: not found or failed`);
    }
  };
  check("python3", "python3", ["--version"]);
  check("rex", "rex", ["version"]);
  const envFile = resolveDefaultEnvFile(opts.envFile);
  console.log(`[info] env file: ${envFile || "(none found)"}`);
  console.log(`[info] hooks dir: ${PY_HOOKS_DIR}`);
  const envData = parseEnvFile(envFile);
  const host = process.env.HOOK_HOST || envData.HOOK_HOST || "127.0.0.1";
  const port = process.env.HOOK_PORT || envData.HOOK_PORT || "18790";
  const curl = spawnSync("curl", ["-sS", "-m", "3", `http://${host}:${port}/health`], { encoding: "utf8" });
  if (curl.status === 0 && curl.stdout.trim()) {
    console.log(`[ok] bridge health: ${curl.stdout.trim()}`);
  } else {
    console.log(`[info] bridge health: not running on http://${host}:${port}/health`);
  }
}

const { cmd, opts } = parseArgs(process.argv.slice(2));
if (opts.version) {
  console.log(PKG_JSON.version);
  process.exit(0);
}
if (opts.help || cmd === "help") {
  printHelp();
  process.exit(0);
}

switch (cmd) {
  case "start":
    runStart(opts);
    break;
  case "setup":
    runSetup(opts);
    break;
  case "doctor":
    runDoctor(opts);
    break;
  case "hooks-path":
    console.log(PY_HOOKS_DIR);
    break;
  default:
    console.error(`Unknown command: ${cmd}\n`);
    printHelp();
    process.exit(1);
}
