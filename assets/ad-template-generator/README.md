# Ad-template safe asset catalog

This is the only source-free fallback catalog for the VPS ad-template generator.
It contains editable-input defaults, not finished ads or source-ad pixels.

## Provenance boundary

- The eleven photographic assets are derived only from the Blockwise commit
  `fb6d88911fc0bbe748c4dd24e17366345cf9624a`, whose commit message explicitly
  describes its fixture assets as safe and fictional. Each manifest entry binds
  the original repository path and source SHA-256.
- The six neutral placeholders are project-owned SVG drawings in `sources/`.
  They contain no copied pixels, text, contact details, faces, or advertiser
  identity. The committed raster output and its SVG master are both bound in
  the manifest.
- Candidate-source images, generated template thumbnails, agent portraits,
  homepage handoff imagery, public ad examples, research media, and network
  downloads are excluded.

## Transform record

- Fictional PNG fixtures were transcoded with FFmpeg 8.1.1/libwebp using
  quality 88, compression level 6, and metadata removal.
- Procedural SVGs were rasterised with sharp 0.35.4 at density 144; WebP assets
  use quality 92 and effort 6. The neutral logo is a compressed PNG.

`catalog/manifest.json` is the runtime allowlist. A production catalog release
must contain exactly that manifest and the declared raster files. The SVG
masters remain source-only and are not copied into the runtime catalog root.
