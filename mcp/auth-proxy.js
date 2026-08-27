import http from "http";
import crypto from "crypto";
import { spawn } from "child_process";
import { fileURLToPath } from "url";

const PUBLIC_PORT = Number(process.env.PORT || 8787);
let INTERNAL_PORT = Number(process.env.LINJIAN_INTERNAL_MCP_PORT || 8788);
if (INTERNAL_PORT === PUBLIC_PORT) INTERNAL_PORT = PUBLIC_PORT + 1;

const ACCESS_TOKEN = String(process.env.MCP_ACCESS_TOKEN || process.env.LINJIAN_TOKEN || "").trim();
const serverPath = fileURLToPath(new URL("./server.js", import.meta.url));

const child = spawn(process.execPath, [serverPath], {
  env: { ...process.env, PORT: String(INTERNAL_PORT) },
  stdio: "inherit",
});

child.on("exit", (code, signal) => {
  console.error(`Palm Window MCP upstream exited code=${code} signal=${signal || ""}`);
  process.exit(code ?? 1);
});

function safeEqual(a, b) {
  const left = Buffer.from(String(a || ""));
  const right = Buffer.from(String(b || ""));
  if (left.length !== right.length) return false;
  return left.length > 0 && crypto.timingSafeEqual(left, right);
}

function suppliedToken(req, url) {
  const auth = String(req.headers.authorization || "").trim();
  if (/^Bearer\s+/i.test(auth)) return auth.replace(/^Bearer\s+/i, "").trim();
  const headerToken = String(req.headers["x-auth-token"] || "").trim();
  if (headerToken) return headerToken;
  return String(url.searchParams.get("access_token") || url.searchParams.get("token") || "").trim();
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(body);
}

function proxyToUpstream(req, res, url) {
  url.searchParams.delete("token");
  url.searchParams.delete("access_token");

  const headers = { ...req.headers, host: `127.0.0.1:${INTERNAL_PORT}` };
  delete headers.authorization;
  delete headers["x-auth-token"];

  const upstream = http.request(
    {
      hostname: "127.0.0.1",
      port: INTERNAL_PORT,
      method: req.method,
      path: `${url.pathname}${url.search}`,
      headers,
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );

  upstream.on("error", (err) => {
    if (!res.headersSent) sendJson(res, 502, { ok: false, error: "mcp_upstream_unavailable" });
    else res.destroy(err);
  });

  req.pipe(upstream);
}

const proxy = http.createServer((req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  if (url.pathname === "/") {
    return res.end("掌心窗 secured MCP proxy is running. Use authenticated /mcp. SSE is disabled on this secured deployment.");
  }

  if (url.pathname === "/health") return proxyToUpstream(req, res, url);

  if (url.pathname === "/sse" || url.pathname === "/messages") {
    return sendJson(res, 403, { ok: false, error: "sse_disabled", message: "Use secured Streamable HTTP /mcp instead." });
  }

  if (url.pathname !== "/mcp") return sendJson(res, 404, { ok: false, error: "not_found" });

  if (req.method === "OPTIONS") return proxyToUpstream(req, res, url);

  if (!ACCESS_TOKEN) return sendJson(res, 503, { ok: false, error: "mcp_access_token_missing" });
  if (!safeEqual(suppliedToken(req, url), ACCESS_TOKEN)) {
    return sendJson(res, 401, { ok: false, error: "unauthorized" });
  }

  return proxyToUpstream(req, res, url);
});

proxy.listen(PUBLIC_PORT, "0.0.0.0", () => {
  console.log(`掌心窗 secured MCP proxy listening on 0.0.0.0:${PUBLIC_PORT}`);
  console.log(`MCP upstream listening on 127.0.0.1:${INTERNAL_PORT}`);
  console.log(`MCP auth source=${process.env.MCP_ACCESS_TOKEN ? "MCP_ACCESS_TOKEN" : "LINJIAN_TOKEN fallback"}`);
});

function shutdown() {
  try { child.kill("SIGTERM"); } catch {}
  proxy.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
