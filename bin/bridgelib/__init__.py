"""bridgelib — the pieces of ask-agent: core (paths, quota, helpers), adapters (one per peer CLI), runner
(process control, board registration, threads, execute). `ask-agent` is the CLI over these; `bridge-sync` imports
the peer table from adapters so the two never drift.
"""
