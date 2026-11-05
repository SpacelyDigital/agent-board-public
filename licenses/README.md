# Bundled runtime licenses

See `THIRD_PARTY.md` one directory above this folder in the source tree, or
`THIRD_PARTY.md` in the app’s `Contents/Resources` directory, for the component
inventory and license mapping.

The notices in this folder apply to their named third-party components only.
They do not license Agent Board’s own code or artwork.

`python-standalone/` preserves the full license directory and component-license
metadata excerpt from the exact CPython 3.12.16 standalone build used for this app,
release 20261026, Apple Silicon, pgo+lto. The upstream directory also includes
notices for optional components that this app does not ship. The metadata excerpt identifies component-to-license relationships and omits
upstream build-machine paths. Its original metadata hash is retained.

The certifi source archive is included for offline access to the exact
MPL-covered source distributed with the collector. Its license is preserved
separately and inside the archive.

`provenance.json` records where the included original texts came from and their
SHA-256 hashes. HACL* and runtime-hook notices preserve original source-file
license headers.
