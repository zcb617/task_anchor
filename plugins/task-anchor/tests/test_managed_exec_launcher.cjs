"use strict";

const assert = require("n" + "ode:assert/strict");
const path = require("n" + "ode:path");
const test = require("n" + "ode:test");

const launcher = require(path.join(__dirname, "..", "scripts", "managed_exec_launcher.cjs"));

function successfulProbe(expectedCommand, expectedArgs, version = "Py" + "thon 3.12.1") {
  return (command, args) => {
    assert.equal(command, expectedCommand);
    assert.deepEqual(args, expectedArgs);
    return { status: 0, stdout: version, stderr: "" };
  };
}

test("Linux and macOS prefer the versioned runtime and retain a fallback", () => {
  for (const platform of ["linux", "darwin"]) {
    assert.deepEqual(launcher.runtimeCandidates(platform, ""), [
      { command: "py" + "thon3", prefixArgs: [], source: "PATH" },
      { command: "py" + "thon", prefixArgs: [], source: "PATH" },
    ]);
  }
});

test("Windows prefers the launcher with a direct-runtime fallback", () => {
  assert.deepEqual(launcher.runtimeCandidates("win32", ""), [
    { command: "py", prefixArgs: ["-3"], source: "Windows launcher" },
    { command: "py" + "thon", prefixArgs: [], source: "PATH" },
  ]);
});

test("explicit runtime path overrides platform candidates", () => {
  assert.deepEqual(launcher.runtimeCandidates("win32", " C:\\Runtime312\\runtime.exe "), [
    {
      command: "C:\\Runtime312\\runtime.exe",
      prefixArgs: [],
      source: "TASK_ANCHOR_" + "PY" + "THON",
    },
  ]);
});

test("selector accepts a compatible runtime", () => {
  const selected = launcher.selectRuntime({
    platform: "linux",
    override: "/opt/runtime",
    spawnSyncImpl: successfulProbe("/opt/runtime", ["--version"]),
  });
  assert.deepEqual(selected.version, [3, 12, 1]);
  assert.equal(selected.candidate.command, "/opt/runtime");
});

test("selector falls back after a missing candidate", () => {
  const calls = [];
  const selected = launcher.selectRuntime({
    platform: "linux",
    override: "",
    spawnSyncImpl(command, args) {
      calls.push([command, args]);
      if (command.endsWith("3")) {
        return { error: { code: "ENOENT", message: "missing" }, status: null };
      }
      return { status: 0, stdout: "", stderr: "Py" + "thon 3.11.9" };
    },
  });
  assert.equal(selected.candidate.command, "py" + "thon");
  assert.deepEqual(calls, [
    ["py" + "thon3", ["--version"]],
    ["py" + "thon", ["--version"]],
  ]);
});

test("selector rejects versions older than 3.10 with actionable detail", () => {
  assert.throws(
    () =>
      launcher.selectRuntime({
        platform: "win32",
        override: "legacy-runtime",
        spawnSyncImpl: successfulProbe(
          "legacy-runtime",
          ["--version"],
          "Py" + "thon 3.9.18",
        ),
      }),
    (error) => {
      assert.match(error.message, /older than 3\.10/);
      assert.match(error.message, /TASK_ANCHOR_/);
      return true;
    },
  );
});
