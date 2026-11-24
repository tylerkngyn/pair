# Bundled Python runtime

Unmodified core runtime assets from the official `pyodide@314.0.8` npm
distribution: https://registry.npmjs.org/pyodide/-/pyodide-314.0.8.tgz

Source code for this version: https://github.com/pyodide/pyodide/tree/314.0.8
Pyodide is licensed under MPL-2.0; see `PYODIDE_LICENSE.txt`. Python and
Emscripten license notices are included as `PYTHON_LICENSE.txt` and
`EMSCRIPTEN_LICENSE.txt`. The standard library and WebAssembly binary include
upstream Python/runtime components under their respective notices.

The five runtime files are listed with sizes and SHA-256 hashes in
`manifest.json`. No npm installation, build step, paid service, or external
runtime download is needed to start this application.

`runtime-loader.js` fetches these files from FastAPI in the trusted workspace,
reports byte progress, and keeps the resulting buffers in memory for later
runs. Versioned URLs are HTTP-cacheable across page loads. A copy is passed to
each isolated worker, which imports module blobs and resolves runtime fetches
against its in-memory asset bundle. Real network access is denied by CSP.

When updating Pyodide, update the versioned directory, all five files and their
manifest, `PYTHON_RUNTIME_BASE`, the loader's version check, and runtime tests
together. Verify the application with external HTTP requests blocked. A new
version directory is required so immutable browser caches remain correct.

Workspace scripts and `runner.html` use `rev=bundled-v2` URLs and a matching
handshake version. Keep those bootstrap versions in sync when changing the
runner protocol; they bypass old cached HTML that may still reference a CDN.
