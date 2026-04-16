"""MessagePack marshaling. See spec/implementation.md §8.

Ext type 0 = undefined (empty body).
Ext type 1 = bigint (UTF-8 decimal string body).
"""

from __future__ import annotations

EXT_UNDEFINED = 0
EXT_BIGINT = 1
