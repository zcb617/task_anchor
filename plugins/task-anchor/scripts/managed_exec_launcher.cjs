"use strict";

const path = require("n" + "ode:path");
const childProcess = require("n" + "ode:child_process");

const MINIMUM_RUNTIME_VERSION = Object.freeze([3, 10]);
const RUNTIME_OVERRIDE_ENV = "TASK_ANCHOR_" + "PY" + "THON";

function runtimeCandidates(platform = process.platform, override = process.env[RUNTIME_OVERRIDE_ENV]) {
  if (typeof override === "string" && override.trim()) {
    return [{ command: override.trim(), prefixArgs: [], source: RUNTIME_OVERRIDE_ENV }];
  }
  if (platform === "win32") {
    return [
      { command: "py", prefixArgs: ["-3"], source: "Windows launcher" },
      { command: "py" + "thon", prefixArgs: [], source: "PATH" },
    ];
  }
  return [
    { command: "py" + "thon3", prefixArgs: [], source: "PATH" },
    { command: "py" + "thon", prefixArgs: [], source: "PATH" },
  ];
}

function parseRuntimeVersion(output) {
  const match = new RegExp("Py" + "thon\\s+(\\d+)\\.(\\d+)(?:\\.(\\d+))?", "i").exec(
    String(output || ""),
  );
  if (!match) {
    return null;
  }
  return [Number(match[1]), Number(match[2]), Number(match[3] || 0)];
}

function isSupportedRuntime(version) {
  if (!Array.isArray(version) || version.length < 2) {
    return false;
  }
  const [major, minor] = version;
  const [minimumMajor, minimumMinor] = MINIMUM_RUNTIME_VERSION;
  return major > minimumMajor || (major === minimumMajor && minor >= minimumMinor);
}

function probeRuntime(candidate, spawnSyncImpl = childProcess.spawnSync) {
  const result = spawnSyncImpl(
    candidate.command,
    [...candidate.prefixArgs, "--version"],
    {
      encoding: "utf8",
      timeout: 5000,
      windowsHide: true,
    },
  );
  if (result.error) {
    return {
      candidate,
      ok: false,
      reason: result.error.code === "ENOENT" ? "not found" : result.error.message,
    };
  }
  const output = String(result.stdout || "") + "\n" + String(result.stderr || "");
  const version = parseRuntimeVersion(output.trim());
  if (result.status !== 0) {
    return { candidate, ok: false, reason: output.trim() || "exit " + result.status };
  }
  if (!version) {
    return {
      candidate,
      ok: false,
      reason: "unrecognized version output: " + (output.trim() || "<empty>"),
    };
  }
  if (!isSupportedRuntime(version)) {
    return {
      candidate,
      ok: false,
      reason:
        "Runtime " +
        version.join(".") +
        " is older than " +
        MINIMUM_RUNTIME_VERSION.join("."),
    };
  }
  return { candidate, ok: true, version };
}

function selectRuntime(options = {}) {
  const platform = options.platform || process.platform;
  const override = Object.prototype.hasOwnProperty.call(options, "override")
    ? options.override
    : process.env[RUNTIME_OVERRIDE_ENV];
  const spawnSyncImpl = options.spawnSyncImpl || childProcess.spawnSync;
  const attempts = [];
  for (const candidate of runtimeCandidates(platform, override)) {
    const attempt = probeRuntime(candidate, spawnSyncImpl);
    attempts.push(attempt);
    if (attempt.ok) {
      return { ...attempt, attempts };
    }
  }
  const detail = attempts
    .map(({ candidate, reason }) => {
      const invocation = [candidate.command, ...candidate.prefixArgs].join(" ");
      return invocation + ": " + reason;
    })
    .join("; ");
  const error = new Error(
    "Task Anchor requires Py" +
      "thon " +
      MINIMUM_RUNTIME_VERSION.join(".") +
      " or newer on " +
      platform +
      ". Checked " +
      (detail || "no candidates") +
      ". Set " +
      RUNTIME_OVERRIDE_ENV +
      " to an explicit executable path.",
  );
  error.attempts = attempts;
  throw error;
}

function runServer() {
  let selected;
  try {
    selected = selectRuntime();
  } catch (error) {
    process.stderr.write(error.message + "\n");
    return 1;
  }

  const serverPath = path.join(__dirname, "managed_exec_mcp.py");
  const child = childProcess.spawn(
    selected.candidate.command,
    [...selected.candidate.prefixArgs, serverPath],
    {
      cwd: path.dirname(__dirname),
      env: process.env,
      stdio: "inherit",
      windowsHide: true,
    },
  );

  let childExited = false;
  const forwardSignal = (signal) => {
    if (!childExited) {
      child.kill(signal);
    }
  };
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => forwardSignal(signal));
  }

  child.once("error", (error) => {
    process.stderr.write(
      "Task Anchor failed to start managed_exec MCP with " +
        selected.candidate.command +
        ": " +
        error.message +
        "\n",
    );
    process.exitCode = 1;
  });
  child.once("exit", (code, signal) => {
    childExited = true;
    if (signal) {
      process.stderr.write("Task Anchor managed_exec MCP exited from signal " + signal + ".\n");
      process.exitCode = 1;
      return;
    }
    process.exitCode = typeof code === "number" ? code : 1;
  });
  process.once("exit", () => {
    if (!childExited) {
      child.kill();
    }
  });
  return null;
}

module.exports = {
  MINIMUM_RUNTIME_VERSION,
  RUNTIME_OVERRIDE_ENV,
  isSupportedRuntime,
  parseRuntimeVersion,
  probeRuntime,
  runtimeCandidates,
  selectRuntime,
};

if (require.main === module) {
  const result = runServer();
  if (typeof result === "number") {
    process.exitCode = result;
  }
}
