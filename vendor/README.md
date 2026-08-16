# Vendored third-party code

Committed rather than fetched, so a saved `report.html` renders offline, on a
machine with no network, and in five years when the CDN has moved. That is the
whole reason this directory exists; a `<script src>` to a CDN would be smaller
and would also mean every reader of a report about internal work announces
themselves to a third party.

| File | Upstream | Version | Licence |
| --- | --- | --- | --- |
| `uplot.min.js` | https://github.com/leeoniya/uPlot | 1.6.31 | MIT |
| `uplot.min.css` | https://github.com/leeoniya/uPlot | 1.6.31 | MIT |

SHA-256 as fetched from `cdn.jsdelivr.net/npm/uplot@1.6.31/dist/`:

```
2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2  uplot.min.js
df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04  uplot.min.css
```

Audited before committing: no `fetch`, `XMLHttpRequest`, `WebSocket`,
`sendBeacon`, `EventSource`, `import()` or `src=`, and no `url()` in the CSS.
The only URL in either file is the attribution comment at the top of the JS,
which is a comment and fetches nothing. A test asserts these properties hold, so
an upgrade that introduces a network call fails the suite rather than the reader.

To upgrade: replace both files, update the version and hashes above, re-run the
audit, and check the report still renders with JavaScript disabled.
