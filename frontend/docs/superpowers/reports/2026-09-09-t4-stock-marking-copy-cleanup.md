# Remaining stock prototype marking copy

T3's scoped follow-up identified one stock source-strip chip advertising
`KIZ/returns`. The standalone HTML now says `Возвраты` and no longer lists marking
in its explanatory sentence. The active React stock page owns its separate
`StockLiveSourceStripIsland`; this is a prototype-copy cleanup, not a React
feature or runtime cutover.

The two `ожидает КИЗ` literals in `ORDERS_PRINT_FALLBACK` belong to the retained
historical `missing_honest_sign` example. They were deliberately left unchanged:
this slice does not erase historical code fields, promote blocked orders to
ready, or alter normalization, print markup, ordinary barcodes or returns.
Historical registry source identifiers and negative compatibility tests are
also not rewritten as if their evidence never existed.

Verification: source diff is one prose-only HTML line; every inline script is
byte-for-byte identical to HEAD. Generator executed normally; decoded generated
HTML equals source and SHA-256 is
`f43eb1a768eba99e08c6fcc7d73cf92406d957cd45be3fb95ca8d55b137a2906`.
`git diff --check` exits 0. No browser/build gate was run for this prose-only
change, no independent review claimed. Main critical pass verified only the
intended source strip changed and all compatibility logic was preserved.
No provider, backend, schema, production, actual export/print, flag or push action.
