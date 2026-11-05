# Offline evidence viewer

Open [index.html](index.html) directly in a browser. No server, build,
installation or Node runtime is needed. Keep the neighboring JavaScript, CSS
and `examples/` files in place.

Choose a Sentinel JSON export or drop one onto the page. Local-detail exports
can contain sensitive project text. The viewer does not upload files or save
them in browser storage. **Explore a synthetic example** opens a clearly
labelled demonstration with no real project measurements.

See [the viewer guide](../docs/local-viewer.md) for limits and interpretation.
For development only, run the dependency-free tests from the repository root
with Node 20 or newer and npm. No application server is needed:

```bash
npm test --offline --ignore-scripts --prefix viewer
```
