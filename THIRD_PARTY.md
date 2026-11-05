# Third-party software

Agent Board’s packaged collector includes the components below, with the
distribution-specific exception noted for certifi. Their original license texts
and notices are in `licenses/` in this source tree and in
`Agent Board.app/Contents/Resources/licenses` in the Mac download.

These notices apply to the named third-party components. They do not grant a
license to Agent Board’s own source code, name, artwork or provider logos.
Provider names and logos are addressed separately in `static/icons/README.md`.

## Bundled components

The runtime uses CPython 3.12.16 from the
[20261026 Python Standalone Builds release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026)
for Apple Silicon. The upstream component-to-license mapping is preserved in
`licenses/python-standalone/component-licenses.json`. Several libraries are linked statically
into Python, so they remain part of the runtime even without separate `.dylib`
files.

| Component | Version or build | License and local notices |
| --- | --- | --- |
| CPython and selected standard-library modules | 3.12.16 | Python Software Foundation License Version 2 and included historical terms: `Python-3.12.16-LICENSE.txt`. The standalone publisher’s combined notice is `python-standalone/LICENSE.cpython.txt`. Additional incorporated-code notices: `Python-3.12.16-incorporated-notices.rst`. |
| HACL* code incorporated into CPython’s hash modules | As shipped in CPython 3.12.16 | MIT: `Python-3.12.16-HACL-MIT-NOTICE.txt`. |
| certifi and its Mozilla-derived CA bundle, direct-download runtime only | 2026.1.4 | Mozilla Public License 2.0: `certifi-2026.1.4-LICENSE.txt` and `MPL-2.0.txt`. Source and notices are retained in both distributions as described below. |
| OpenSSL | 3.5.9 | Apache License 2.0: `python-standalone/LICENSE.openssl-3.txt`. |
| SQLite | 3.53.1 | Public-domain dedication: `python-standalone/LICENSE.sqlite.txt`. |
| libffi | As shipped in standalone build 20261026 | MIT: `python-standalone/LICENSE.libffi.txt`. |
| Expat | As shipped in standalone build 20261026 | MIT: `python-standalone/LICENSE.expat.txt`. |
| liblzma | As shipped in standalone build 20261026 | BSD Zero Clause License: `python-standalone/LICENSE.liblzma.txt`. |
| mpdecimal | As shipped in standalone build 20261026 | BSD 2-Clause: `python-standalone/LICENSE.mpdecimal.txt`. |
| bzip2 | As shipped in standalone build 20261026 | bzip2 license: `python-standalone/LICENSE.bzip2.txt`. |
| libuuid | As shipped in standalone build 20261026 | BSD 3-Clause: `python-standalone/LICENSE.libuuid.txt`. |
| PyInstaller bootloader and embedded loader files | 6.16.0 | GPL 2.0 or later with the project’s Bootloader Exception. The exception, full license and runtime-hook terms are preserved in `PyInstaller-6.16.0-COPYING.txt`. Runtime-hook headers are in `PyInstaller-6.16.0-runtime-hooks-NOTICE.txt`. |

The upstream standalone license directory is preserved in full, including
notices for optional modules and libraries that are not included in this app.
Its zlib, zlib-ng, libedit and ncurses notices are retained alongside its module
metadata; this Mac runtime links to system-provided zlib, libedit and ncurses.
The native display also links to macOS system frameworks supplied by Apple.
The download does not include Claude, Codex, their model runtimes, or their
accounts; those apps remain separately installed software.

## certifi source availability

The unmodified certifi 2026.1.4 source distribution is bundled at
`licenses/certifi-2026.1.4.tar.gz`, under Mozilla Public License 2.0. It contains
the source for certifi’s Python files and the exact `cacert.pem` CA bundle used
by the direct-download collector. The included source files and CA bundle were
compared byte for byte with the installed package before bundling. The Store
collector excludes certifi from its frozen runtime and does not load this CA
bundle. It retains the source archive and license notices for provenance.

The same source is available from the [certifi 2026.1.4 release on PyPI](https://pypi.org/project/certifi/2026.1.4/#files).
Its archive SHA-256 is:

```
ac726dd470482006e014ad384921ed6438c457018f4b3d204aea4281258b2120
```

## Build and provenance

The inventory was checked against the selected runtime’s build metadata and
PyInstaller’s frozen-collector analysis. Build-only tools such as altgraph,
macholib and packaging are not part of this runtime inventory because their
modules are not included in the frozen collector.

Agent Board does not patch these projects’ source code. The packaging step
selects modules, bundles runtime files, adjusts dynamic-library load paths and
code-signs binaries. The certifi CA bundle used by the direct-download runtime
is unchanged.

`licenses/provenance.json` records the source and SHA-256 of each supplied
notice and source archive, including the verified standalone build archive.
The installed Python license and the publisher’s combined Python notice are
both preserved, since the latter also includes historical incorporated-software
notices. Additional CPython notices came from the exact upstream 3.12.16 source tree;
the full MPL text came from Mozilla.

Upstream sources: [CPython 3.12.16](https://github.com/python/cpython/tree/v3.12.16),
[OpenSSL 3.5.9](https://github.com/openssl/openssl/releases/tag/openssl-3.5.9),
[SQLite copyright](https://sqlite.org/copyright.html),
[PyInstaller 6.16.0 licensing](https://github.com/pyinstaller/pyinstaller/blob/v6.16.0/COPYING.txt).
