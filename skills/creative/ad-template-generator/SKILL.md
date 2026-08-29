# Sole ad-template generator

Hermes is the only process owner. Frank is a display and command surface.

The builder and comparator are separate agent instances. Run one comparator after
each candidate iteration. Only after a comparator reaches 9.5 may two independent
final reviewer instances run. A failed final review automatically starts another
builder iteration.

The final step must invoke the unversioned deterministic CLI
`python3 -m tools.ad_template_generator`. It validates layered Feed and native
Story documents and writes canonical JSON plus deterministic SVG renders. The
process then calls `BLOCKWISE_TEMPLATE_IMPORT_URL` and records the returned
template ID and status. Never use vault uploads, hashes, signatures, or human
approval.
