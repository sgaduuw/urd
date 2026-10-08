# Vendored third-party code

Committed so the app never contacts a CDN: a `<script src>` to one would be
smaller and would also mean every reader of a report about internal work
announces themselves to a third party. The files are served by `urd serve` from
`/vendor/`.

| File | Upstream | Version | Licence |
| --- | --- | --- | --- |
| `uplot.min.js` | https://github.com/leeoniya/uPlot | 1.6.31 | MIT |
| `uplot.min.css` | https://github.com/leeoniya/uPlot | 1.6.31 | MIT |
| `htmx.min.js` | https://github.com/bigskysoftware/htmx | 4.0.0 | 0BSD |

SHA-256 as fetched from `cdn.jsdelivr.net/npm/uplot@1.6.31/dist/`:

```
2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2  uplot.min.js
df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04  uplot.min.css
```

SHA-256 of `htmx.min.js`, from the npm package `htmx.org@4.0.0`,
`dist/htmx.min.js`:

```
e484d9171a9db30a39c8f16e3d709d4137f3211c659f8e6125816635033d593f  htmx.min.js
```

uPlot is audited to make no network request of its own: no `fetch`,
`XMLHttpRequest`, `WebSocket`, `sendBeacon`, `EventSource`, `import()` or `src=`,
and no `url()` in the CSS. A test asserts this. htmx is exempt by design,
because requesting this app's own routes is its purpose. Its scope is limited
by its default `mode: 'same-origin'` and by the app's Content-Security-Policy.

To upgrade: replace the files, update the version and hashes above, re-run the
audit, and check the report still renders with JavaScript disabled.
