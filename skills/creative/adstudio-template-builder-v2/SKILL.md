# adstudio-template-builder-v2

## Purpose

Execute Frank Ad Studio's private, durable template-build job. This skill is
only for a `schema://hermes.tool-run-command/v1` Tool run. It must never create
or continue a chat.

## Runtime

- The dedicated, committed builder checkout is `/opt/ad-template-builder`.
- Run every builder command from that directory with its locked Node runtime.
- The canonical gallery is `src/lib/adstudio/template-gallery-v2` and the
  canonical assets are `src/lib/adstudio/template-assets-v2`. Never look for
  an unversioned `template-gallery` or `template-assets` directory.
- Inputs are private paths supplied by the Tool controller. Outputs remain
  beneath Hermes' private Tool asset, checkpoint, and release directories.
- The Tool run's pinned model-policy revision is authoritative. Do not inherit
  a Hub/chat model, change a started stage's route, or invent current pricing.

## Pipeline

Run and checkpoint these stages in order:

1. `source`: verify the private source ref and hash.
2. `analyse`: extract the source contract with the configured vision chain.
3. `decompose`: run `node scripts/adstudio/v2/ingest.mjs decompose --id <id>`.
4. `restyle`: sanitize identity, copy, palette, assets, and source-photo slots.
5. `story-draft`: run the deterministic story layout command.
6. `check`: run the deterministic builder check.
7. `subject-invariance`: run
   `node scripts/adstudio/v2/subject-invariance.mjs --id <id>`.
8. `studio-qa`: return previews and evidence, then stop for Frank approval.
9. `ready`: after 100% zoom approval, rerun every release gate.
10. `release`: write the immutable pack beneath
    `$HERMES_HOME/tool_releases/ad-template-generator` and return its receipt.

The analyse stage uses the configured vision route to write the builder's
evidence and layered draft contracts; it is not a deterministic CLI subcommand.
If a deterministic command rejects the resulting candidate, stop and report
the evidence; never make the model simulate a passing result.

## Image-model boundary

An image model may change pixels only inside a declared text-cleanup mask or an
explicit optional story-margin extension mask. Never ask a model to paint,
clone, or recreate a whole ad. Pixels outside a mask are immutable during that
attempt. Decomposition, rendering, hashing, validation, subject-invariance,
and packaging are deterministic VPS work and make no model calls.

## Release law

Release only a provider-neutral TemplatePack with Feed and Story layouts,
layered document, editable image/text contracts, copy/CTA/form contracts,
safe assets and previews, model-policy and trace provenance, QA and
subject-invariance evidence, sanitization receipt, 100% zoom approval,
checksum, and Ed25519 signature receipt.

Exclude raw sources, replaceable source-photo pixels, advertiser identity,
private prompts, credentials, reviewer identity, temporary URLs, drafts, and
internal paths. Return only the compact redacted Tool result requested by the
controller. Hidden reasoning and unrestricted command output are never part of
events or the pack.
