/**
 * PCLink Code Lookup Worker
 * Cloudflare Worker that maps 6-digit pairing codes to server URLs
 * 
 * Endpoints:
 *   POST /register  { code: "123456", url: "wss://..." }  → agent calls this
 *   GET  /lookup?code=123456                               → app calls this
 * 
 * Deploy to Cloudflare Workers (free tier: 100,000 req/day)
 * Requires a KV namespace called PCLINK_CODES bound to this worker
 */

const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const CODE_TTL_SECONDS = 600; // codes expire after 10 minutes

export default {
  async fetch(request, env) {
    const url  = new URL(request.url);
    const path = url.pathname;

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // ── POST /register ──────────────────────────────────────────
    if (path === "/register" && request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "Invalid JSON" }, 400);
      }

      const { code, url: serverUrl } = body;

      if (!code || !serverUrl) {
        return json({ error: "Missing code or url" }, 400);
      }

      if (!/^\d{6}$/.test(code)) {
        return json({ error: "Code must be 6 digits" }, 400);
      }

      if (!serverUrl.startsWith("ws://") && !serverUrl.startsWith("wss://")) {
        return json({ error: "URL must start with ws:// or wss://" }, 400);
      }

      // Store in KV with TTL
      await env.PCLINK_CODES.put(code, serverUrl, { expirationTtl: CODE_TTL_SECONDS });
      console.log(`[REGISTER] Code ${code} → ${serverUrl}`);

      return json({ success: true, expires_in: CODE_TTL_SECONDS });
    }

    // ── GET /lookup?code=123456 ──────────────────────────────────
    if (path === "/lookup" && request.method === "GET") {
      const code = url.searchParams.get("code");

      if (!code) {
        return json({ error: "Missing code parameter" }, 400);
      }

      if (!/^\d{6}$/.test(code)) {
        return json({ error: "Code must be 6 digits" }, 400);
      }

      const serverUrl = await env.PCLINK_CODES.get(code);

      if (!serverUrl) {
        return json({ error: "Code not found or expired", found: false }, 404);
      }

      console.log(`[LOOKUP] Code ${code} → ${serverUrl}`);
      return json({ success: true, found: true, url: serverUrl });
    }

    // ── Health check ─────────────────────────────────────────────
    if (path === "/" || path === "/health") {
      return json({ status: "ok", service: "PCLink Code Lookup" });
    }

    return json({ error: "Not found" }, 404);
  }
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}
