import { timingSafeEqual } from "node:crypto";

const OWNER = "imgho6";
const REPO = "POEBDTOD";
const WORKFLOW = "telegram-publisher.yml";
const API = "https://api.github.com";

function authorized(req, secret) {
  const value = req.headers.authorization || "";
  const expected = `Bearer ${secret}`;
  const a = Buffer.from(value);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

async function github(path, token, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`GitHub API ${response.status}: ${detail.slice(0, 500)}`);
  }
  return response.status === 204 ? null : response.json();
}

export default async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "POST") {
    res.setHeader("Allow", "GET, POST");
    return res.status(405).json({ ok: false, error: "method_not_allowed" });
  }

  const secret = process.env.CRON_SECRET;
  const token = process.env.GITHUB_ACTIONS_TOKEN;
  if (!secret || !token) {
    return res.status(503).json({ ok: false, error: "scheduler_not_configured" });
  }
  if (!authorized(req, secret)) {
    return res.status(401).json({ ok: false, error: "unauthorized" });
  }

  try {
    const runs = await github(
      `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=10`,
      token
    );
    const active = (runs.workflow_runs || []).find(
      (run) => run.status === "queued" || run.status === "in_progress"
    );

    if (active) {
      return res.status(200).json({
        ok: true,
        dispatched: false,
        reason: "publisher_already_active",
        run_id: active.id,
        run_status: active.status,
      });
    }

    await github(
      `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      token,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    return res.status(202).json({
      ok: true,
      dispatched: true,
      workflow: WORKFLOW,
      ref: "main",
      timestamp: new Date().toISOString(),
    });
  } catch (error) {
    console.error("External scheduler dispatch failed:", error);
    return res.status(502).json({ ok: false, error: "dispatch_failed" });
  }
}
