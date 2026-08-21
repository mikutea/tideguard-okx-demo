import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { access, mkdir, rm } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import { createServer } from "vite";

const execFileAsync = promisify(execFile);
const frontendRoot = fileURLToPath(new URL("..", import.meta.url));
const projectRoot = path.dirname(frontendRoot);

function browserCandidates() {
  const candidates = [process.env.MOHENG_TEST_BROWSER];
  if (process.platform === "win32") {
    for (const base of [
      process.env.ProgramW6432,
      process.env.ProgramFiles,
      process.env["ProgramFiles(x86)"],
      process.env.LOCALAPPDATA,
    ]) {
      if (!base) continue;
      candidates.push(
        path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
        path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
      );
    }
  } else if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    );
  } else {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/usr/bin/microsoft-edge",
    );
  }
  return [...new Set(candidates.filter(Boolean))];
}

async function findBrowser() {
  for (const candidate of browserCandidates()) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next known system-browser location.
    }
  }
  throw new Error("No installed Chromium browser found for the real component DOM contract test");
}

test("HistoricalReplayConsole renders V6/V5 contracts and responds to real DOM controls", { timeout: 45_000 }, async () => {
  const browser = await findBrowser();
  const profileRoot = path.join(projectRoot, ".pytest-work", `frontend-browser-${process.pid}-${randomUUID()}`);
  await mkdir(profileRoot, { recursive: true });

  const server = await createServer({
    root: frontendRoot,
    configFile: path.join(frontendRoot, "vite.config.ts"),
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });

  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object", "Vite test server did not expose a local port");
    const url = `http://127.0.0.1:${address.port}/tests/historical-replay-console.browser.html`;

    const { stdout } = await execFileAsync(browser, [
      "--headless=new",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-features=OptimizationHints,MediaRouter",
      "--disable-gpu",
      "--disable-sync",
      "--metrics-recording-only",
      "--no-first-run",
      "--no-pings",
      "--no-proxy-server",
      "--proxy-bypass-list=*",
      `--user-data-dir=${profileRoot}`,
      "--dump-dom",
      url,
    ], {
      encoding: "utf8",
      maxBuffer: 4 * 1024 * 1024,
      timeout: 30_000,
      windowsHide: true,
    });

    assert.match(stdout, /id="browser-test-result"[^>]*data-status="passed"/);
    assert.match(stdout, /data-v6-mapped="true"/);
    assert.match(stdout, /data-v5-retired-invalid="true"/);
    assert.match(stdout, /data-semantic-fields="true"/);
    assert.match(stdout, /data-playback-controls="true"/);
  } finally {
    await server.close();
    await rm(profileRoot, { recursive: true, force: true });
  }
});
