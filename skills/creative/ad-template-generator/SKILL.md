# Sole ad-template generator

Hermes is the only process owner. Frank only starts runs and displays their
source, iterations, scores, status, cost, final review, and Blockwise import.

The builder and comparator are separate agent instances. Run one comparator after
each candidate iteration. Only after a comparator reaches 9.5 may two independent
final reviewer instances run. A failed final review automatically starts another
builder iteration. Every comparator and final reviewer scores exactly five fields:
layout geometry, hierarchy and typography, colour and tone, editable decomposition,
and native Story composition. A source identity leak, flattened critical layer,
clipped or unsafe content, missing asset, or Feed-derived Story is a hard failure.

Every candidate is rendered with the shared Blockwise Node renderer named by
`AD_TEMPLATE_GENERATOR_CMD`. Builder output declares only normalized relative
paths from `AD_TEMPLATE_ASSET_CATALOG_DIR`; Hermes reads those source-free assets
and rejects inline bytes. Text layers may use only the documented bundled font
files and every text, image, colour, font, and asset reference must resolve inside
the same template. The accepted layered Feed and native Story template is
posted directly to `BLOCKWISE_TEMPLATE_IMPORT_URL`, then Hermes records the
returned template ID and status. Never use vault uploads, releases, hashes,
signatures, process versions, or human approval.
