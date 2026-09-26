# Security Policy

## Supported versions

OrchKit has no versioned public release yet. Security maintenance applies to the current public `main` branch while the project remains in this early source stage. No response-time or patch-availability commitment is made for unreleased source.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, pull request, or chat transcript.

GitHub private vulnerability reporting is enabled for this repository. Use the repository's **Report a vulnerability** flow under GitHub Security Advisories so the report and follow-up discussion remain private. Do not open a public issue, discussion, pull request, or chat with vulnerability details. Do not include unrelated secrets, credentials, capability data, raw runtime state, or personal information in a report.

Please include a concise description, affected source revision, reproduction steps or a proof of concept that avoids sensitive data, impact, and any suggested mitigation. Do not access data or systems beyond what is needed to demonstrate the issue.

## Scope notes

The documented same-user limitation is an architectural boundary, not a hidden vulnerability: OrchKit is a cooperative local control plane and is not an OS sandbox. Reports are most useful when they identify a failure of a documented workflow control, an unexpected authority escalation, unsafe state handling, or a contradiction between the implementation and this public documentation.

## Response process

The maintainer will triage a private report, request additional reproduction details when needed, and decide whether to accept, reject, or continue investigating it. Accepted issues can be handled through a private GitHub Security Advisory while a fix and release plan are prepared. The reporter and maintainer should coordinate before publishing technical details or a public advisory.

## Disclosure

Please allow the maintainer to assess a report privately before publishing technical details. This policy does not promise a fixed response, remediation, disclosure, or release timeline.
