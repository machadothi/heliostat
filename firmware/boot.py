"""Runs before main.py on every boot. Keep this minimal and crash-free.

Anything that can fail belongs in app.py, where the safe-boot guard in main.py
can catch it.
"""

import gc

import machine

machine.freq(160_000_000)

# Collect eagerly rather than waiting until the heap is nearly exhausted. The
# control loop allocates little, but JSON parsing in the HTTP server spikes.
gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
