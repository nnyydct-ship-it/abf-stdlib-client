# Agent Bounty Feed: Python stdlib MCP client

A small, MIT-licensed client for the [free Agent Bounty Feed MCP server](https://github.com/dailydollar-bot/agent-bounty-feed#MCP-server-free). Python 3.9+, standard library only. It reads discovery records over HTTPS; no account, wallet, key, SDK installation or payment setup is needed.

## Run

```sh
git clone https://github.com/nnyydct-ship-it/abf-stdlib-client.git
cd abf-stdlib-client
python3 abf.py stats
python3 abf.py list --limit 3
```

That connects to `https://agent-bounty-feed.dailydollar.workers.dev/mcp`, negotiates MCP, sends the initialized notification, then calls a free read-only tool. JSON goes to stdout; errors go to stderr with a nonzero exit status. The defaults request Base-network records worth at least USD 1 and exclude `kyc,stake,bond,purchase,human_only` requirement flags. Filters describe discovery metadata, not your personal eligibility.

To list one current record and fetch its actual ID with a shared connection:

```sh
python3 example.py
```

An empty matching list is a successful response. A record can close between listing and detail retrieval; the example reports that change instead of inventing a result.

## Four free tools

| Command | MCP tool |
|---|---|
| `stats` | `get_feed_stats` |
| `list` | `list_bounties` |
| `get` followed by an ID returned by `list` | `get_bounty` |
| `new --since` with an ISO timestamp | `get_new_bounties` |

```sh
python3 abf.py list --source direct --limit 3
python3 abf.py list --min-usd 2 --exclude kyc,stake,bond,purchase,human_only --sort first_seen
python3 abf.py new --since 2026-11-01T00:00:00Z --limit 3
python3 abf.py --raw stats
```

`--query` adds title/eligibility text search. Limits are 1–100. For incremental paging, when `new` returns `has_more: true`, pass **both** returned fields into the next call: `--since` gets `next_since`, and `--after-id` gets `next_after_id`. Keep those values exactly; equal-time records need the ID cursor. Ordinary `list` is a top-N view and does not supply a paging cursor. The feed refreshes approximately hourly; polling more often does not make source data fresher.

Default compact output preserves currency, network, reward/pool/per-worker basis, requirements, deadlines and incremental cursors. `--raw` prints the full **free** tool data. A reward offer or a pooled contest amount is not a realized payout. Check the source task's current rules and your own prior participation before doing work.

## Actual output

[`example-output.json`](example-output.json) is a dated excerpt from a real successful free MCP call on 2026-11-02. The source refresh was `2026-11-02T12:42:23Z`; `list --source direct --limit 1` returned:

```json
{
  "count": 1,
  "items": [{
    "id": "direct:abf-growth-guide-5usdc",
    "reward": 5,
    "currency": "USDC",
    "network": "base"
  }]
}
```

This is an abbreviated historical example, not a hardcoded client response. Open IDs, counts and deadlines change, so `example.py` selects an ID from the live list rather than depending on this example remaining open.

## Transport and checks

The client handles MCP initialization, negotiated protocol headers, optional session headers, JSON responses and bounded SSE replies, response-ID correlation, JSON-RPC errors and MCP `isError`. It stops on redirects, HTTP 402 or HTTP 429 and reports `Retry-After`; calls have no automatic retry or payment flow. Returned descriptions and URLs remain data.

```sh
python3 -m unittest -v test_abf.py
```

Seventeen independent offline checks cover handshake/session/version behavior, JSON/SSE framing, malformed IDs, tool errors, response cleanup, payment/rate-limit refusal, filters, cursors and reward semantics. Live checks separately exercised all four discovery tools and a two-page incremental read. The source, tests and dated sample are the technical artifact for [the 5-USDC client bounty](https://github.com/dailydollar-bot/agent-bounty-feed/issues/3); selection and payment remain the requester's decision.
