# ORCH post-v0.11 — frozen backup member integrity

**Date:** 2026-09-19. **Scope:** non-database files admitted into ORCH state backups.

## Problem

The backup census rejected symlinks when enumerating ORCH-owned files, but `ZipFile.write(path)` reopened each path later. A same-user filesystem race could therefore replace a previously censused regular file with a symlink or different inode before ZIP ingestion. ZIP CRC also detects accidental corruption but does not cryptographically bind restored config/evidence bytes to the backup manifest.

## Frozen member read

Backup members are now opened no-follow and verified as regular files on the opened descriptor. Bytes are streamed from that exact descriptor directly into the ZIP. ORCH records device/inode/size/mtime/ctime before the stream and rechecks them after; mutation during the read fails closed and the unpublished temporary backup is discarded.

Each streamed `files/...` member records exact byte count and SHA-256 in `state/manifest.json:file_evidence`.

## Verification contract

For backups that carry `file_evidence`, `state verify-backup` requires its member set to match the exact `files/...` ZIP members, validates evidence structure and recorded sizes, then streams every member and verifies its SHA-256. A ZIP whose payload is modified and whose CRC is legitimately recalculated therefore still fails with `backup_file_evidence_mismatch`.

Older valid backups without `file_evidence` remain readable and restoreable; verification records a compatibility warning instead of rejecting them. This preserves the existing legacy-schema restore contract.

## Evidence

- regular-to-symlink replacement after backup census fails with `backup_member_unsafe:config.json`, does not publish the requested ZIP, and does not read or alter the external victim;
- tampering `files/config.json` while rebuilding a syntactically valid ZIP is blocked by manifest SHA-256 evidence;
- state/recovery suite: **50/50 PASS**;
- complete deterministic source suite: **211/211 PASS**;
- Python compilation and `git diff --check`: PASS.

No Codex/model invocation is used. <separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
