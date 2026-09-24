# Security Policy

## Supported versions

OrchKit has no versioned public release yet. Security maintenance applies to the current public `main` branch while the project remains in this early source stage. No response-time or patch-availability commitment is made for unreleased source.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, pull request, or chat transcript.

GitHub private vulnerability reporting is not enabled for this repository at the time of this policy. If you discover a vulnerability, contact the repository maintainer privately through a contact method that the maintainer has explicitly made public on their GitHub profile. If no such method is available, open an issue titled `Private security contact requested` without vulnerability details and wait for a private channel. Do not include secrets, credentials, capability data, raw runtime state, or personal information in a report.

Please include a concise description, affected source revision, reproduction steps or a proof of concept that avoids sensitive data, impact, and any suggested mitigation. Do not access data or systems beyond what is needed to demonstrate the issue.

## Scope notes

The documented same-user limitation is an architectural boundary, not a hidden vulnerability: OrchKit is a cooperative local control plane and is not an OS sandbox. Reports are most useful when they identify a failure of a documented workflow control, an unexpected authority escalation, unsafe state handling, or a contradiction between the implementation and this public documentation.

## Disclosure

Please allow the maintainer to assess a report privately before publishing technical details. This policy does not promise a fixed disclosure timeline or support channel while private reporting remains unconfigured.
