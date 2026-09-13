"""One package per agent environment (host) engrim plugs into.

Each host directory holds both halves of its integration: install-time wiring (`wiring.py`, with
any files it writes) and runtime behaviour (`hooks.py`, what happens on boot / prompt / stop),
so a host can be read, reviewed, or removed as a unit.
"""
