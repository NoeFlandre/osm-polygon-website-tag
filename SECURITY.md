# Security policy

## Report a vulnerability

Report a vulnerability in private. Use GitHub
[private vulnerability reporting](https://github.com/NoeFlandre/osm-polygon-website-tag/security/advisories/new).
Do not use a public issue or a pull request.

In your report, include these items:

- What is affected (CLI command, fetcher, workflow, or published dataset).
- How to reproduce the problem.
- The impact that you expect.

These items are in scope:

- A bypass of the SSRF defences of the fetcher (`web/web_fetch.py`). The defences are the scheme, redirect, DNS and IP checks, the timeouts, and the response-size limits.
- An exposure of the Hugging Face token.
- Any change that can alter the published dataset.

## Supported versions

The latest code on `main` and the latest published dataset get fixes. No other version gets fixes.
