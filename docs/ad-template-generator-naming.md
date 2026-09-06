# Ad Template Generator naming contract

Hermes calls the product and its active implementation **Ad Template
Generator**. New source uses the matching identifier forms:

- `ad_template_generator` for Python modules, functions, and payload fields;
- `AdTemplateGenerator` for classes;
- `AD_TEMPLATE_GENERATOR` for constants.

The canonical implementation modules are:

- `gateway.ad_template_generator_process`;
- `gateway.ad_template_generator_layer_refinement`;
- `gateway.ad_template_generator_photo_qa`.

The model catalogue response publishes
`ad_template_generator_capabilities`. During the compatibility window it
also publishes `ad_studio_capabilities` with the same data for older clients.

## Intentional compatibility boundaries

This source rename does not rewrite durable or external contracts:

- tool ID and workspace scope `ad-template-generator`;
- process value `exact-clone`;
- checkpoint file `exact-clone-checkpoint.json` and its existing schema;
- Blockwise `blockwise.ad-template` documents, route scopes, asset catalogue,
  renderer settings, and font declarations;
- historical runs, events, policies, receipts, and deployment evidence.

The old `gateway.exact_clone_*` import paths resolve to the canonical module
objects so extension monkeypatches and callers retain module identity. The old
`ExactCloneOrchestrator`, `validate_exact_clone_output`, policy helper, and
route/catalog symbols remain import aliases only.

Existing seed-15 policies whose stored name is `Sole ad-template process`
remain valid and are not migrated merely for display naming. New seed-15
