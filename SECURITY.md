# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/NoeFlandre/osm-polygon-website-tag/security/advisories/new),
not in a public issue or pull request.

Include what is affected (CLI command, fetcher, workflow, published dataset),
how to reproduce it, and the impact you expect. In scope, among others:

- bypassing the fetcher's SSRF defences (`web/web_fetch.py`): scheme, redirect,
  DNS and IP checks, timeouts and response-size limits;
- exposure of the Hugging Face token;
- anything that could alter the published dataset.

## Supported versions

Only the latest code on `main` and the latest published dataset receive fixes.
