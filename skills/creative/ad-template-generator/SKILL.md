# Sole ad-template generator

Hermes is the only process owner. Frank only starts runs and displays their
source, iterations, scores, status, cost, final review, and Blockwise import.
Current cross-system endpoints, payloads, status handling and approval actions
are maintained in /projects/frank/docs/AD_TEMPLATE_GENERATOR.md. This skill
defines generator behavior only and must not duplicate that operational guide.

The builder and comparator are separate agent instances. Generator acceptance
policy and the complete six-field score gate are maintained in
/projects/frank/docs/AD_TEMPLATE_GENERATOR.md. This skill only records the
generator-specific renderer, asset, and process-boundary requirements below.
Use the controller for runs. Never simulate processor writes or fabricate
review evidence.

Every candidate is rendered with the shared Blockwise Node renderer named by
`AD_TEMPLATE_GENERATOR_CMD`. Builder output declares only normalized relative
paths from `AD_TEMPLATE_ASSET_CATALOG_DIR`; Hermes reads those source-free assets
and rejects inline bytes. Text layers may use only the documented bundled font
files and every text, image, colour, font, and asset reference must resolve inside
the same template. The accepted layered Feed and native Story template is
posted directly to `BLOCKWISE_TEMPLATE_IMPORT_URL`, then Hermes records the
returned template ID and quarantine status. Do not use the retired signed
TemplatePack release pipeline. Preserve the current controller's required
identities, hashes and versioned evidence. Never self-approve or bypass the
explicit operator approval gate.
