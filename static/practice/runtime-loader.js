'use strict';
// Fetch from the trusted page so normal HTTP caching survives disposable,
// opaque-origin workers. No account data is included in the asset bundle.
const PYTHON_RUNTIME_BASE = '/assets/runtime/314.0.8/';
const PYTHON_RUNTIME_FILES = ['pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json'];
const pythonAssetCache = new Map();

async function loadPythonAssets(signal, progress) {
  const response = await fetch(PYTHON_RUNTIME_BASE + 'manifest.json', {signal, credentials:'omit'});
  if (!response.ok) throw new Error('Python runtime is missing from this deployment. Restart or redeploy the app.');
  const manifest = await response.json();
  if (manifest.version !== '314.0.8' || manifest.files.length !== PYTHON_RUNTIME_FILES.length || !PYTHON_RUNTIME_FILES.every(name => manifest.files.some(file => file.name === name))) {
    throw new Error('Python runtime manifest does not match this workspace. Refresh the page.');
  }
  const total = manifest.files.reduce((sum, file) => sum + file.bytes, 0);
  const loaded = new Map(manifest.files.map(file => [file.name, pythonAssetCache.get(file.name)?.byteLength || 0]));
  const report = () => progress([...loaded.values()].reduce((a, b) => a + b, 0), total);
  report();
  await Promise.all(manifest.files.map(async file => {
    if (pythonAssetCache.has(file.name)) return;
    const result = await fetch(PYTHON_RUNTIME_BASE + file.name, {signal, credentials:'omit'});
    if (!result.ok) throw new Error(`Could not download Python (${file.name}). Try Run again.`);
    const chunks = [];
    if (result.body) {
      const reader = result.body.getReader();
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        chunks.push(value); loaded.set(file.name, loaded.get(file.name) + value.byteLength); report();
      }
    } else {
      chunks.push(new Uint8Array(await result.arrayBuffer()));
    }
    const bytes = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
    if (bytes !== file.bytes) throw new Error(`Python download was incomplete (${file.name}). Try Run again.`);
    const buffer = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
    pythonAssetCache.set(file.name, buffer.buffer); loaded.set(file.name, bytes); report();
  }));
  signal.throwIfAborted();
  return Object.fromEntries(PYTHON_RUNTIME_FILES.map(name => [name, pythonAssetCache.get(name)]));
}
