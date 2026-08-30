# Security Policy

## Supported version

Security fixes are applied to the current `2.x` line.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not open a public issue for path traversal, archive extraction, SSRF, browser-to-localhost, arbitrary file access, or denial-of-service findings.

Include the affected version, operating system, a minimal reproduction, impact, and any suggested mitigation. Avoid accessing files or systems that are not yours.

## Security model

Auto-MD is a single-user local development tool. It binds only to `127.0.0.1`, requires a per-process browser session for job access, accepts no credentials, and is not designed for public or LAN deployment. Source files are untrusted even in local mode; all path, archive, URL, workload, and artifact boundaries remain enforced.

