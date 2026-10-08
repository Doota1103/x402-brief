#!/usr/bin/env python3
"""x402-style pay-per-call API server for museforjustin (v2).

Sells GET /v1/brief for 0.01 USDC on Base (chainId 8453).

Architecture (Relay402-style facilitator / agent-server split):
  * Facilitator -- owns everything payment-related: builds the x402 payment
    challenge, verifies the on-chain payment via public Base RPC, and *settles*
    (burns the tx hash so it cannot be replayed). It has no business logic.
  * Agent server (the HTTP handler) -- owns business logic only: validates
    input, generates the brief, and delegates all payment mechanics to the
    facilitator. It never touches chain RPC or the used-tx ledger directly.

Pricing: settle-only-on-success. A payment is *verified* before generation but
*settled* (tx hash burned) only after the brief is generated successfully. If
generation fails, the server answers 500 WITHOUT settling, so the client can
retry the same request with the same payment -- failed calls are never charged.

Payment contract (unchanged from v1):
  1. Client calls /v1/brief?topic=... with no payment -> HTTP 402 + challenge.
  2. Client sends >= 0.01 USDC on Base to payTo.
  3. Client retries with header "X-Payment: <tx-hash>".
  4. Facilitator verifies the tx (receipt status, USDC contract, ERC20
     Transfer event, recipient == payTo, amount >= price, no replay).
  5. Agent server generates the brief; only on success does the facilitator
     settle. 200 + brief + receipt (+ PAYMENT-RESPONSE header).

Stdlib only for the server (http.server + urllib). No private key material is
ever loaded here; only the public payTo address is read from the wallet file.
"""
import datetime
import json
import os
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET_PATH = os.path.expanduser("~/.config/moltbook/x402-wallet.json")
# PaaS-ready: DATA_DIR / PORT / payTo are env-overridable; local defaults unchanged.
DATA_DIR = os.environ.get("X402_DATA_DIR", HERE)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass
LOG_PATH = os.path.join(DATA_DIR, "x402-server.log")
USED_TX_PATH = os.path.join(DATA_DIR, "x402-used-tx.json")

PORT = int(os.environ.get("PORT", "8931"))
BIND = "0.0.0.0"
BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # keccak("Transfer(address,address,uint256)")
PRICE_USDC = "0.01"
PRICE_UNITS = 10_000  # 0.01 USDC, 6 decimals
CHAIN_ID = 8453
CAIP2 = "eip155:8453"


def load_pay_to():
    # Env override lets hosted deploys set the public payTo address without
    # shipping the wallet file. Locally the wallet file is used as before.
    env_addr = os.environ.get("X402_PAY_TO", "").strip()
    if env_addr:
        return env_addr
    with open(WALLET_PATH) as f:
        return json.load(f)["address"]


PAY_TO = load_pay_to()


def log(msg):
    line = "%s %s" % (datetime.datetime.now(datetime.timezone.utc).isoformat(), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def rpc_call(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(BASE_RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


class Facilitator:
    """Relay402-style payment facilitator.

    Owns: challenge construction, on-chain payment verification, and
    settlement (single-use tx-hash ledger). The agent server calls
    check_payment() before doing work and settle() only after the work
    succeeded -- that is the settle-only-on-success guarantee.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._used = self._load_used()

    # -- ledger ---------------------------------------------------------
    def _load_used(self):
        try:
            with open(USED_TX_PATH) as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def _save_used(self):
        tmp = USED_TX_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(self._used), f)
        os.replace(tmp, USED_TX_PATH)

    # -- challenge ------------------------------------------------------
    def challenge(self):
        """x402 V2 payment challenge (plus legacy flat fields for v1 clients)."""
        return {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": CAIP2,
                    "amount": str(PRICE_UNITS),  # micro-units string, per x402 V2
                    "asset": USDC_BASE,
                    "payTo": PAY_TO,
                }
            ],
            "error": "payment_required",
            "price": PRICE_USDC,
            "asset": "USDC",
            "network": "base",
            "chainId": CHAIN_ID,
            "payTo": PAY_TO,
            "usdcContract": USDC_BASE,
            "instructions": (
                "Send at least %s USDC on Base (chainId %d) to %s, "
                "then retry this exact request with header 'X-Payment: <tx-hash>'. "
                "Each tx hash can be used exactly once, and it is only settled "
                "(consumed) after your brief is generated successfully -- "
                "failed calls are never charged."
                % (PRICE_USDC, CHAIN_ID, PAY_TO)
            ),
        }

    # -- verification (no side effects) ---------------------------------
    def check_payment(self, tx_hash):
        """Return (ok: bool, reason: str). Pure check -- does NOT settle."""
        h = (tx_hash or "").strip()
        if not (h.startswith("0x") and len(h) == 66
                and all(c in "0123456789abcdefABCDEF" for c in h[2:])):
            return False, "malformed tx hash"
        hl = h.lower()
        with self._lock:
            if hl in self._used:
                return False, "tx hash already used"
        try:
            resp = rpc_call("eth_getTransactionReceipt", [h])
        except Exception as e:  # network/RPC failure -> unverified, not paid
            return False, "rpc error: %s" % type(e).__name__
        rcpt = resp.get("result")
        if not rcpt:
            return False, "tx not found on Base"
        if rcpt.get("status") != "0x1":
            return False, "tx reverted or failed"
        for lg in rcpt.get("logs") or []:
            if (lg.get("address") or "").lower() != USDC_BASE.lower():
                continue
            topics = lg.get("topics") or []
            if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_SIG:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != PAY_TO.lower():
                continue
            try:
                amount = int(lg.get("data") or "0x0", 16)
            except ValueError:
                continue
            if amount >= PRICE_UNITS:
                return True, "ok"
        return False, "no qualifying USDC transfer (>= 0.01) to payTo in tx logs"

    # -- settlement (only after successful delivery) --------------------
    def settle(self, tx_hash):
        """Burn the tx hash so it cannot be replayed.

        Returns True if this call settled it, False if it was already used
        (e.g. a concurrent request consumed it first).
        """
        hl = (tx_hash or "").strip().lower()
        with self._lock:
            if hl in self._used:
                return False
            self._used.add(hl)
            self._save_used()
            return True


facilitator = Facilitator()


def make_brief(topic):
    t = topic.strip()
    return {
        "topic": t,
        "version": "v2-template",
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": [
            "This is a v2 template brief on '%s'. The paid content engine is not yet wired to live research sources, so what follows is a structured starter brief rather than a fully researched report." % t,
            "The topic '%s' would normally be covered across four angles: background and definitions, current state of play, key players and data points, and open questions. In v2, buyers get the shape of a proper brief with honest labeling instead of hallucinated specifics." % t,
            "Settlement is settle-only-on-success: your 0.01 USDC is consumed only after this brief is generated. If generation had failed, you could have retried with the same payment.",
            "Roadmap: the brief body will be generated from live web sources with citations, keeping the same 0.01 USDC price and the same X-Payment verification flow.",
        ],
        "key_points": [
            "v2 delivers a structured template brief, clearly labeled as such.",
            "Payment loop is fully on-chain: 0.01 USDC on Base -> X-Payment tx hash -> verified Transfer event -> brief + receipt.",
            "Each tx hash is single-use; replays are rejected.",
            "Settle-only-on-success: the tx hash is burned only after successful delivery.",
            "Price, asset, network, and payTo are always disclosed in the 402 challenge before any payment.",
        ],
        "note": "Template content. No live research was performed for this v2 response.",
    }


def discovery_doc():
    return {
        "service": "museforjustin-briefs",
        "description": "Pay-per-call research briefs. 0.01 USDC per call on Base. Settle-only-on-success.",
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": CAIP2,
                "amount": str(PRICE_UNITS),
                "asset": USDC_BASE,
                "payTo": PAY_TO,
            }
        ],
        "facilitator": {
            "pattern": "relay402-split",
            "settlement": "settle-only-on-success",
            "note": "Payment verification is separated from content generation; the tx hash is burned only after the brief is delivered.",
        },
        "payment": {
            "scheme": "x402-simple",
            "asset": "USDC",
            "network": "base",
            "chainId": CHAIN_ID,
            "payTo": PAY_TO,
            "usdcContract": USDC_BASE,
        },
        "endpoints": [
            {
                "path": "/v1/brief",
                "method": "GET",
                "params": {"topic": "string, required, max 200 chars"},
                "price": "0.01 USDC",
                "auth": "X-Payment: <base-tx-hash> after sending >= 0.01 USDC to payTo",
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    """Agent server: business logic only. All payment mechanics live in the facilitator."""
    server_version = "x402-brief/2.0"

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        client = self.client_address[0]

        if path == "/.well-known/x402":
            log("%s GET /.well-known/x402 -> 200" % client)
            return self._json(200, discovery_doc())

        if path != "/v1/brief":
            log("%s GET %s -> 404" % (client, path))
            return self._json(404, {"error": "not_found"})

        qs = urllib.parse.parse_qs(parsed.query)
        topic = (qs.get("topic") or [""])[0].strip()
        if not topic:
            return self._json(400, {"error": "missing required query param: topic"})
        if len(topic) > 200:
            return self._json(400, {"error": "topic too long (max 200 chars)"})

        # --- facilitator: payment gate (verify, do NOT settle yet) ---
        pay_header = self.headers.get("X-Payment")
        if not pay_header:
            log("%s GET /v1/brief topic=%r -> 402 (no payment)" % (client, topic))
            return self._json(402, facilitator.challenge())

        ok, reason = facilitator.check_payment(pay_header)
        if not ok:
            log("%s GET /v1/brief topic=%r -> 402 (bad payment: %s)" % (client, topic, reason))
            doc = facilitator.challenge()
            doc["payment_error"] = reason
            return self._json(402, doc)

        # --- agent server: generate the goods ---
        try:
            brief = make_brief(topic)
        except Exception as e:  # noqa: BLE001 -- generation failed: do NOT settle
            log("%s GET /v1/brief topic=%r -> 500 (generation failed, payment NOT settled: %s)"
                % (client, topic, type(e).__name__))
            return self._json(500, {
                "error": "brief_generation_failed",
                "detail": "The brief could not be generated. Your payment was NOT settled: "
                          "retry this exact request with the same X-Payment tx hash.",
            })

        # --- facilitator: settle only after successful delivery ---
        if not facilitator.settle(pay_header):
            log("%s GET /v1/brief topic=%r -> 402 (tx consumed by another request)" % (client, topic))
            doc = facilitator.challenge()
            doc["payment_error"] = "tx hash already used"
            return self._json(402, doc)

        receipt = {
            "txHash": pay_header.strip(),
            "paid": PRICE_USDC,
            "asset": "USDC",
            "network": "base",
            "settled_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "settlement": "settle-only-on-success",
        }
        brief["receipt"] = receipt
        log("%s GET /v1/brief topic=%r -> 200 (settled %s...)" % (client, topic, pay_header.strip()[:12]))
        return self._json(200, brief,
                          extra_headers={"PAYMENT-RESPONSE": json.dumps(receipt, separators=(",", ":"))})

    def log_message(self, fmt, *args):  # route stdlib noise into our log file
        log("httpd: " + fmt % args)


def main():
    log("starting x402-brief v2 server on %s:%d payTo=%s price=%s USDC (settle-only-on-success)"
        % (BIND, PORT, PAY_TO, PRICE_USDC))
    HTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
