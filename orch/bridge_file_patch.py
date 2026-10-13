"""Bounded trusted-local replacement with durable, non-replaying recovery."""
from __future__ import annotations
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets

from .bridge_authorization import LocalRunSessionAuthority
from .bridge_file_scope import LocalFileScope
from .bridge_patch_execution import (PatchExecutionStore, runtime_operation_lock,
                                     execution_record_hash)
from .bridge_patch_intent import FilePatchReplacement, LocalFilePatchIntentService
from .bridge_patch_metadata import file_signature, stable_identity, read_file_metadata, copy_file_metadata
from .worker_operations import WorkerOperationRequest, WorkerRunBinding, WORKER_MAX_OUTPUT_BYTES


def _time():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _response(status, operation_id, *, historical=False, error_code=None):
    result = {'status': status, 'operation_id': operation_id, 'historical': historical,
              'retry_allowed': False}
    if error_code is not None: result['error_code'] = error_code
    return result


def _capture(fd):
    before = file_signature(os.fstat(fd))
    if before['size'] > WORKER_MAX_OUTPUT_BYTES:
        raise ValueError('patch_content_too_large')
    metadata = read_file_metadata(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, size = [], 0
    while True:
        data = os.read(fd, min(8192, WORKER_MAX_OUTPUT_BYTES + 1 - size))
        if not data: break
        chunks.append(data);size += len(data)
        if size > WORKER_MAX_OUTPUT_BYTES: raise ValueError('patch_content_too_large')
    content = b''.join(chunks)
    after = file_signature(os.fstat(fd))
    checked = read_file_metadata(fd)
    if before != after or metadata['xattrs_digest'] != checked['xattrs_digest']:
        raise ValueError('patch_content_changed')
    value = {'identity': after, 'sha256': hashlib.sha256(content).hexdigest(),
             'size_bytes': len(content), 'xattrs_digest': checked['xattrs_digest']}
    return value, content, checked


def _same_material(actual, expected, *, stable=False):
    identity_matches = (stable_identity(actual['identity']) == stable_identity(expected['identity'])
                        if stable else actual['identity'] == expected['identity'])
    return identity_matches and all(actual[k] == expected[k]
                                   for k in ('sha256', 'size_bytes', 'xattrs_digest'))


def _entry(parent, name):
    try: return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError: return None


class LocalBoundedFilePatcher:
    """One participating dispatch per ID; reconcile never writes result bytes.

    Existing results are historical receipts, not statements about later file
    contents. Advisory locking and descriptor checks are not a same-user fence.
    """
    def __init__(self, authority, session_id, binding, capability_file):
        if (not isinstance(authority, LocalRunSessionAuthority)
                or not isinstance(session_id, str) or not isinstance(binding, WorkerRunBinding)
                or not isinstance(capability_file, Path)):
            raise ValueError('patch_invalid_authority')
        self._authority, self._session, self._binding, self._cap = authority, session_id, binding, capability_file
        self._home = authority._orch.root
        self._store = PatchExecutionStore(self._home)
        self._intent = LocalFilePatchIntentService(authority, session_id, binding, capability_file)

    def _authorize(self):
        self._authority.require_session(self._session, self._binding, self._cap)

    def _validate(self, request):
        if not isinstance(request, WorkerOperationRequest): raise ValueError('patch_invalid_request')
        if request.kind != 'file.patch' or request.binding != self._binding:
            raise ValueError('patch_scope_denied')
        self._authorize()

    def _base(self, request, intent, phase):
        return {'schema_version': 1, 'phase': phase, 'request': asdict(request),
                'intent_fingerprint': intent['fingerprint'], 'created_at': _time()}

    def _append(self, record, authorize):
        result = self._store.append(record, authorize=authorize)
        if result['status'] == 'UNKNOWN':
            raise ValueError('patch_execution_acknowledgement_unknown')
        if result['status'] not in {'CREATED', 'EXISTING'}:
            raise ValueError('patch_execution_publication_unknown')
        return result

    def _result(self, request, intent, records, disposition, basis, target, *, temp_absent):
        record = self._base(request, intent, 'RESULT')
        record.update(dispatch_sha256=execution_record_hash(records['dispatch']),
                      publication_sha256=(execution_record_hash(records['publication'])
                                          if 'publication' in records else None),
                      disposition=disposition,
                      basis={'kind': basis, 'target': target, 'temp_absent': temp_absent})
        return record

    def _read_target(self, scope, request):
        read = replace(request, kind='file.read', max_output_bytes=WORKER_MAX_OUTPUT_BYTES)
        with scope.open_readonly(read) as fd: return _capture(fd)[0]

    def _authorized_recheck(self, handle, *, original=True):
        self._authorize();handle.recheck(original_required=original)

    def _historical_result(self, request, records):
        # A visible terminal record establishes history only after renewed
        # acknowledgement of its exact evidence under current service authority.
        renewed = self._store.acknowledge_existing(
            request.binding.run_id, request.operation_id, records,
            authorize=self._authorize, expected_request=asdict(request),
            expected_binding=self._binding,
        )
        return _response(renewed['result']['disposition'], request.operation_id, historical=True)

    def apply(self, request, replacement):
        if not isinstance(replacement, FilePatchReplacement): raise ValueError('patch_invalid_companion')
        operation = getattr(request, 'operation_id', 'invalid')
        try:
            with runtime_operation_lock(self._home):
                try:
                    self._validate(request)
                except (OSError, TypeError, ValueError):
                    return _response('REFUSED', operation, error_code='patch_authority_denied')
                prepared = self._intent.prepare(request, replacement)
                if prepared['status'] not in {'CREATED', 'EXISTING'}:
                    return _response(prepared['status'], operation,
                                     error_code=prepared.get('error_code', 'patch_intent_denied'))
                intent = prepared['record']
                found = self._store.inspect(request.binding.run_id, operation)
                if found['status'] == 'UNKNOWN': return _response('UNKNOWN', operation, error_code='patch_execution_unknown')
                if found['status'] == 'RECORDED':
                    if found['records']['dispatch']['intent_fingerprint'] != intent['fingerprint']:
                        return _response('CONFLICT', operation, error_code='patch_id_conflict')
                    if found['terminal']:
                        return self._historical_result(request, found['records'])
                    return _response('UNKNOWN', operation, error_code='patch_reconciliation_required')
                with LocalFileScope(self._authority, self._session, self._binding, self._cap) as scope:
                    with scope.open_patch_target(request) as handle:
                        original, data, metadata = _capture(handle.fd)
                        if (original['sha256'] != replacement.baseline.sha256
                                or len(data) != replacement.baseline.size_bytes):
                            return _response('REFUSED', operation, error_code='patch_original_mismatch')
                        temp_name = '.orch-patch-' + secrets.token_hex(16) + '.tmp'
                        dispatch = self._base(request, intent, 'DISPATCH')
                        dispatch.update(anchors=handle.anchors, original=original, temp_name=temp_name)
                        self._append(dispatch, lambda: self._authorized_recheck(handle))
                        records = {'dispatch': dispatch}
                        return self._apply_dispatched(scope, handle, request, replacement, intent,
                                                      records, metadata)
        except (OSError, TypeError, ValueError) as exc:
            # No exception text or payload is model-visible. A prior dispatch
            # is independently inspected before any future action.
            if isinstance(exc, ValueError) and str(exc) == 'patch_execution_busy':
                return _response('UNKNOWN', operation, error_code='patch_execution_busy')
            try:
                found = self._store.inspect(self._binding.run_id, operation)
                uncertain = (found['status'] != 'ABSENT'
                             or (isinstance(exc, ValueError)
                                 and str(exc) == 'patch_execution_acknowledgement_unknown'))
            except Exception: uncertain = True
            return _response('UNKNOWN' if uncertain else 'REFUSED', operation,
                             error_code='patch_execution_unavailable')

    def _apply_dispatched(self, scope, handle, request, replacement, intent, records, metadata):
        temp = records['dispatch']['temp_name'];fd = -1;owned = None;publication = False
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(temp, flags, 0o600, dir_fd=handle.parent_fd)
            owned = file_signature(os.fstat(fd))
            offset = 0
            while offset < len(replacement.replacement):
                n = os.write(fd, replacement.replacement[offset:])
                if n <= 0: raise OSError('write failed')
                offset += n
            copy_file_metadata(metadata, fd)
            staged, data, _ = _capture(fd)
            if data != replacement.replacement: raise ValueError('patch_staging_changed')
            os.fsync(fd)
            record = self._base(request, intent, 'PUBLICATION')
            record.update(dispatch_sha256=execution_record_hash(records['dispatch']), staged=staged)
            self._append(record, lambda: self._authorized_recheck(handle))
            records['publication'] = record;publication = True
            self._authorized_recheck(handle)
            current = _capture(handle.fd)[0]
            checked = _capture(fd)[0]
            named = _entry(handle.parent_fd, temp)
            if (not _same_material(current, records['dispatch']['original'])
                    or not _same_material(checked, staged)
                    or named is None or file_signature(named) != checked['identity']):
                raise ValueError('patch_publication_changed')
            # A rename error can have side effects; publication now makes its
            # outcome uncertain and forbids cleanup/re-dispatch.
            os.replace(temp, handle.leaf, src_dir_fd=handle.parent_fd, dst_dir_fd=handle.parent_fd)
            os.fsync(handle.parent_fd)
            self._authorized_recheck(handle, original=False)
            observed = self._read_target(scope, request)
            if (not _same_material(observed, staged, stable=True)
                    or _entry(handle.parent_fd, temp) is not None):
                raise ValueError('patch_readback_changed')
            result = self._result(request, intent, records, 'APPLIED', 'APPLY', observed, temp_absent=True)
            self._append(result, lambda: self._authorized_recheck(handle, original=False))
            return _response('APPLIED', request.operation_id)
        except (OSError, TypeError, ValueError):
            # Even a failed publication acknowledgement can leave publication
            # evidence. Inspect it before selecting a pre-publication cleanup.
            found = self._store.inspect(request.binding.run_id, request.operation_id)
            if (publication or found['status'] != 'RECORDED'
                    or 'publication' in found.get('records', {})):
                return _response('UNKNOWN', request.operation_id, error_code='patch_reconciliation_required')
            try:
                self._authorized_recheck(handle)
                original = _capture(handle.fd)[0]
                if not _same_material(original, records['dispatch']['original']): raise ValueError('original changed')
                named = _entry(handle.parent_fd, temp)
                if named is not None:
                    if (owned is None or (named.st_dev, named.st_ino) != (owned['dev'], owned['ino'])
                            or fd < 0 or (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (owned['dev'], owned['ino'])):
                        raise ValueError('foreign temporary')
                    self._authorized_recheck(handle)
                    os.unlink(temp, dir_fd=handle.parent_fd);os.fsync(handle.parent_fd)
                if _entry(handle.parent_fd, temp) is not None: raise ValueError('temporary not absent')
                original = self._read_target(scope, request)
                if not _same_material(original, records['dispatch']['original']): raise ValueError('original changed')
                result = self._result(request, intent, records, 'NOT_APPLIED', 'FAILED_BEFORE_PUBLICATION',
                                      original, temp_absent=True)
                self._append(result, lambda: self._authorized_recheck(handle))
                return _response('NOT_APPLIED', request.operation_id)
            except (OSError, TypeError, ValueError):
                return _response('UNKNOWN', request.operation_id, error_code='patch_reconciliation_required')
        finally:
            if fd >= 0: os.close(fd)

    def reconcile(self, request):
        operation = getattr(request, 'operation_id', 'invalid')
        try:
            with runtime_operation_lock(self._home):
                self._validate(request)
                found = self._store.inspect(self._binding.run_id, operation)
                if found['status'] != 'RECORDED': return _response('UNKNOWN', operation, error_code='patch_execution_unknown')
                records = found['records'];dispatch = records['dispatch']
                if dispatch['request'] != asdict(request): return _response('CONFLICT', operation, error_code='patch_id_conflict')
                if found['terminal']: return self._historical_result(request, records)
                from .bridge_operation_journal import OperationIntentJournal
                stored = OperationIntentJournal(self._home).inspect(self._binding.run_id, operation)
                if stored['status'] != 'RECORDED': raise ValueError('intent unknown')
                intent = stored['record']
                with LocalFileScope(self._authority, self._session, self._binding, self._cap) as scope:
                    with scope.open_patch_target(request) as handle:
                        if handle.anchors != dispatch['anchors']: raise ValueError('anchors changed')
                        current = _capture(handle.fd)[0];publication = records.get('publication')
                        temp = dispatch['temp_name'];named = _entry(handle.parent_fd, temp)
                        if publication and _same_material(current, publication['staged'], stable=True):
                            if named is not None: raise ValueError('ambiguous staging alias')
                            self._authorized_recheck(handle);os.fsync(handle.parent_fd)
                            observed = self._read_target(scope, request)
                            if not _same_material(observed, publication['staged'], stable=True): raise ValueError('readback changed')
                            result = self._result(request, intent, records, 'APPLIED', 'RECONCILE_APPLIED', observed, temp_absent=True)
                            self._append(result, lambda: self._authorized_recheck(handle))
                            return _response('APPLIED', operation)
                        if not _same_material(current, dispatch['original']): raise ValueError('original changed')
                        basis = 'RECONCILE_NO_PUBLICATION'
                        if publication:
                            if named is None: raise ValueError('staging missing')
                            fd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                         dir_fd=handle.parent_fd)
                            try:
                                staged = _capture(fd)[0]
                                if (not _same_material(staged, publication['staged'], stable=True)
                                        or (named.st_dev, named.st_ino) != (staged['identity']['dev'], staged['identity']['ino'])):
                                    raise ValueError('staging substituted')
                                self._authorized_recheck(handle)
                                again = _entry(handle.parent_fd, temp)
                                if again is None or (again.st_dev, again.st_ino) != (named.st_dev, named.st_ino):
                                    raise ValueError('staging changed')
                                os.unlink(temp, dir_fd=handle.parent_fd);os.fsync(handle.parent_fd)
                            finally: os.close(fd)
                            basis = 'RECONCILE_STAGED'
                        elif named is not None: raise ValueError('unowned staging')
                        self._authorized_recheck(handle)
                        if _entry(handle.parent_fd, temp) is not None: raise ValueError('staging not absent')
                        observed = self._read_target(scope, request)
                        if not _same_material(observed, dispatch['original']): raise ValueError('original changed')
                        result = self._result(request, intent, records, 'NOT_APPLIED', basis, observed, temp_absent=True)
                        self._append(result, lambda: self._authorized_recheck(handle))
                        return _response('NOT_APPLIED', operation)
        except (OSError, TypeError, ValueError):
            return _response('UNKNOWN', operation, error_code='patch_reconciliation_required')
