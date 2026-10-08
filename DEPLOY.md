# Deploying the x402 Research Brief server — exact steps

Goal: a stable public HTTPS URL for the brief server so it can be listed on
minia2a.uk (`publish-service`) and agent402.tools (`POST /api/index/register`).
Both marketplaces reject loopback/ephemeral-tunnel origins, so this needs real hosting.

Recommended platform: **Render** (free tier, managed TLS, stable `*.onrender.com` URL).
Why: free tier = 750 instance-hours/month (one service kept awake 24/7 ≈ 730h,
fits); native Python runtime needs no Docker knowledge; `render.yaml` Blueprint
auto-configures the service; the server is stdlib-only so builds take seconds.
No credit card was required for the free tier per most sources — but one source
(Sept 2026) reports Render began asking for a card at signup for identity
verification. Verify at signup; if a card is demanded, use the Zeabur fallback below.

## Steps (Justin or anyone with his GitHub)

1. **Push these files to GitHub.** Create a repo (e.g. `Doota1103/x402-brief`) and
   upload every file in this folder — GitHub web UI → *Add file → Upload files*
   works, no CLI needed. Files: `x402-server.py`, `Dockerfile`,
   `render.yaml`, `requirements.txt`, `.env.example`, `DEPLOY.md` (this file).

2. **Sign up at https://render.com** (GitHub login is easiest). Stay on the free plan.

3. **New → Web Service → connect the repo.** Render detects `render.yaml` and
   pre-fills: Runtime Python, Build `pip install -r requirements.txt`,
   Start `python x402-server.py`, plan **Free**. (Manual alternative: same values
   typed by hand; either works.)

4. **Environment → add variable:**
   - `X402_PAY_TO` = `0xE972312a00F195F6bc1746ed6af43945509C94bA`
   (`PORT` is injected by Render automatically; the server reads it.)

5. **Create Web Service.** First deploy takes ~2–4 minutes. You get a URL like
   `https://x402-brief.onrender.com`.

6. **Verify** (from any machine):
   - `curl "https://<url>/v1/brief?topic=test"` → HTTP **402** with JSON
     containing `"payTo": "0xE972312a00F195F6bc1746ed6af43945509C94bA"`.
   - `curl "https://<url>/.well-known/x402"` → HTTP **200** discovery doc.

7. **Keep it awake (optional but recommended).** Free services sleep after
   ~15 min idle and wake on the next request (~30–60s cold start). A free
   https://uptimerobot.com monitor pinging `https://<url>/.well-known/x402`
   every 10 minutes keeps it warm; sleeping instances don't burn hours, so
   24/7 stays inside the 750h/month budget either way.

8. **List it.** With the public URL in hand:
   - minia2a.uk: `publish-service` (1 signed EIP-191 call; publisher
     `museforjustin` is already registered).
   - agent402.tools: `POST /api/index/register {"origin": "https://<url>"}`.

## Honest limitations of the free tier

- **Sleep:** covered above; keepalive mitigates it.
- **Ephemeral disk:** the used-tx ledger (`x402-used-tx.json`, the replay
  protection) lives on the container disk and resets if the instance is
  replaced. For $0.01/settlement this is an accepted trade-off; do not treat
  the free deployment as hardened payment infra.
- **512MB RAM:** far more than this stdlib server needs.
- **No private key on the server, ever.** Payment verification is done via
  public Base RPC; only the public `payTo` address is configured.

## Fallback: Zeabur (if Render demands a card)

Zeabur offers a $5 trial credit, no sleep, and Docker deploys; the included
`Dockerfile` works there unchanged (build from repo → expose port → set
`X402_PAY_TO`). Verify current trial terms at signup — it is a trial, not
perpetual free.

## Not recommended (checked and rejected)

- **Koyeb:** free tier now requires a card ($29 hold) and is closed to new signups.
- **Hugging Face Spaces:** Docker hosting moved behind the $9/mo PRO plan (mid-2026).
- **Fly.io / Railway / Northflank:** card required and/or trial-only in 2026.
- **PythonAnywhere free:** outbound network restricted to a whitelist —
  `mainnet.base.org` RPC calls would be blocked.
- **cloudflared/localtunnel from the agent VM:** tested 2026-10-08 — the
  sandbox egress proxy blocks tunnel data channels.
