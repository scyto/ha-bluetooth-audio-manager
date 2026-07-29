"""Test package.

The add-on logs at INFO by default, and several tests deliberately drive
error paths (corrupt store, exhausted MPD port pool). Silence logging so a
failing assertion is the only thing in the output.
"""

import logging

logging.disable(logging.CRITICAL)
