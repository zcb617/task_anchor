"use strict";

const path = require("node:path");
const childProcess = require("node:child_process");
const { selectRuntime } = require("./managed_exec_launcher.cjs");

function main() {
  let selected;
  try {
    selected = selectRuntime();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 1;
  }

  const child = childProcess.spawn(
    selected.candidate.command,
    [...selected.candidate.prefixArgs, path.join(__dirname, "claude_hook_entry.py")],
    {
      cwd: path.dirname(__dirname),
      env: process.env,
      stdio: "inherit",
      windowsHide: true,
    },
  );
  child.once("error", (error) => {
    process.stderr.write(`Task Anchor hook failed to start: ${error.message}\n`);
  });
  child.once("exit", (code, signal) => {
    process.exitCode = signal ? 1 : (typeof code === "number" ? code : 1);
  });
  return null;
}

if (require.main === module) {
  const result = main();
  if (typeof result === "number") {
    process.exitCode = result;
  }
}
