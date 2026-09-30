"""Runs before main.py on every boot AND every soft reset. Keep it trivial.

mpremote soft-resets the board each time it connects, so anything here runs at
exactly the moment the host is waiting for a REPL prompt. An earlier version
called machine.freq() here: re-clocking the CPU disturbs UART0 mid-handshake,
and half of all mpremote connections failed until the board was reset by hand.
The chip already boots at 160 MHz; there is nothing to set.

Anything that can fail belongs in app.py, where main.py's safe-boot guard can
catch it.
"""

import gc

# Collect before the heap is nearly exhausted: JSON parsing in the HTTP server
# allocates in bursts. Harmless at boot -- it touches no hardware.
gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
