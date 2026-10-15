# Vendored scripts

Downloaded once from the pinned jsDelivr URLs below, checked against the published SRI hashes, and
served from `/static/vendor/` (the CSP allows only same-origin scripts). `tests/web/test_web_static.py`
re-checks every hash. To upgrade, download the new pinned version, verify its hash with
`openssl dgst -sha384 -binary FILE | openssl base64 -A`, and update this table.

| File | Source | SRI (sha384) | Bytes |
|---|---|---|---|
| `htmx.min.js` | https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js | `sha384-2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg` | 52182 |
| `sse.min.js` | https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.min.js | `sha384-A986SAtodyH8eg8x8irJnYUk7i9inVQqYigD6qZ9evobksGNIXfeFvDwLSHcp31N` | 2853 |
| `idiomorph-ext.min.js` | https://cdn.jsdelivr.net/npm/idiomorph@0.8.0/dist/idiomorph-ext.min.js | `sha384-+iBk70O0UWQdSjXQSQ7amSxXCrsWq/6UOtvC9rDo0p7telbTSOyttExq6QCFBzNk` | 11037 |

Licences: see each package on npm (htmx.org, htmx-ext-sse, idiomorph); none of the minified files carries a header.
