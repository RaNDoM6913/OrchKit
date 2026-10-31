"""Trusted in-process ownership of independent local file/process contexts.

Routing identities remain volatile and worker operations check native authority.
Use a broker and its contexts only in their creating thread and process. This
module adds no transport, native writer/process release or same-user OS fence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import threading

from .bridge_authorization import AuthorityLedger, LocalRunSessionAuthority
from .bridge_file_list import LocalBoundedFileLister
from .bridge_file_read import LocalBoundedFileReader
from .bridge_file_scope import LocalFileScope
from .bridge_process import LocalOwnedProcessOperations
from .worker_operations import (
    WorkerOperationOutcome, WorkerOperationRequest, WorkerRunBinding,
    require_matching_worker_run, validate_worker_outcome,
)


_CONTEXT_SEAL = object()


def _validate_overview_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("bridge_overview_invalid_limit")


class LocalRunContext:
    """One sealed broker-owned context; a copied object grants no routing access."""

    __slots__ = ("__broker", "__session_id", "__binding")

    def __init__(self, broker: LocalRunBroker, session_id: str,
                 binding: WorkerRunBinding, *, _seal: object = None):
        if _seal is not _CONTEXT_SEAL:
            raise ValueError("bridge_broker_context_unowned")
        self.__broker = broker
        self.__session_id = session_id
        self.__binding = binding

    @property
    def session_id(self) -> str:
        return self.__session_id

    @property
    def binding(self) -> WorkerRunBinding:
        return self.__binding

    def read(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__broker._read(self, request)

    def list(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__broker._list(self, request)

    def status(self) -> dict:
        return self.__broker._status(self)

    def process_start(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__broker._process_start(self, request)

    def process_poll(self, request: WorkerOperationRequest, *, cursor: int = 0
                     ) -> WorkerOperationOutcome:
        return self.__broker._process_poll(self, request, cursor=cursor)

    def process_terminate(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__broker._process_terminate(self, request)

    def patch_apply(self, request, replacement):
        return self.__broker._patch(self, request, replacement=replacement)

    def patch_reconcile(self, request):
        return self.__broker._patch(self, request, replacement=None)

    def close(self) -> None:
        self.__broker._close_context(self)


@dataclass(eq=False)
class _OwnedContext:
    session_id: str
    binding: WorkerRunBinding
    capability_file: Path
    scope: LocalFileScope | None = None
    reader: LocalBoundedFileReader | None = None
    lister: LocalBoundedFileLister | None = None
    processes: LocalOwnedProcessOperations | None = None
    patcher: object | None = None
    context: LocalRunContext | None = None
    closed: bool = False
    session_open: bool = True
    cleanup_unresolved: bool = False
    process_cleanup_unresolved: bool = False


class LocalRunBroker:
    """Own one private native authority and the resources of its contexts.

    Explicit close cleans retained process owners, routing slots and file scopes.
    Process cleanup uses only held direct children; its return does not prove an
    exit or release durable capacity. A failed call retains that owner for an
    explicit retry, independently of scope and routing cleanup.
    Known-busy scopes remain owned for an explicit close after handles unwind.
    An ambiguous scope-close failure remains visible and is never retried: the
    underlying numeric descriptor could already have been reused elsewhere.
    """

    def __init__(self, ledger: AuthorityLedger, *, max_worker_sessions: int = 3,
                 process_store=None, patch_enabled: bool = False):
        self.__authority = LocalRunSessionAuthority(
            ledger, max_worker_sessions=max_worker_sessions,
        )
        if process_store is not None:
            from .bridge_process_store import ProcessOperationStore
            if not isinstance(process_store, ProcessOperationStore):
                raise ValueError("process_invalid_store")
            process_store.require_authority(ledger)
        if type(patch_enabled) is not bool:
            raise ValueError("bridge_broker_invalid_patch_policy")
        self.__patch_enabled = patch_enabled
        self.__process_store = process_store
        self.__ledger = ledger
        self.__home = ledger.root.expanduser().resolve()
        self.__owner = (os.getpid(), threading.get_ident())
        self.__owned: list[_OwnedContext] = []
        self.__contexts: dict[LocalRunContext, _OwnedContext] = {}
        self.__closed = False

    def __require_owner(self) -> None:
        if self.__owner != (os.getpid(), threading.get_ident()):
            raise ValueError("bridge_broker_owner_mismatch")

    def overview_projection(self, *, limit: int = 32,
                            home: Path | None = None) -> dict:
        """Observe bounded volatile flags, never capability/native authority.

        Unlisted bindings are UNKNOWN even after this broker closes. Retained
        cleanup history can outnumber live routing slots, so bound the retained
        list itself rather than assuming the session admission limit bounds it.
        No resource owner or native write-capable connection is consulted.
        """
        self.__require_owner()
        _validate_overview_limit(limit)
        if home is not None and home.expanduser().resolve() != self.__home:
            raise ValueError("bridge_broker_home_mismatch")
        return {
            "scope": "THIS_BROKER_ONLY",
            "status": "UNKNOWN" if len(self.__owned) > limit else "OBSERVED",
            "coverage": "TRUNCATED" if len(self.__owned) > limit else "COMPLETE",
            "unlisted_route_state": "UNKNOWN",
            "external_connectivity": "UNKNOWN",
            "contexts": [
                {
                    "binding": asdict(owned.binding),
                    "route_state": (
                        "OPEN_IN_THIS_BROKER"
                        if not self.__closed and not owned.closed and owned.session_open
                        else "CLOSED_IN_THIS_BROKER"
                    ),
                    "current_native_authority": "UNKNOWN",
                    "cleanup_unresolved": (
                        owned.cleanup_unresolved or owned.process_cleanup_unresolved
                    ),
                }
                for owned in self.__owned[:limit]
            ],
        }

    def open_context(self, binding: WorkerRunBinding,
                     capability_file: Path, *, profiles=()) -> LocalRunContext:
        """Admit a context with an optional immutable operator profile snapshot.

        Profiles are trusted local policy, never worker request arguments. Each
        profiled context keeps one owner using the existing capacity policy of
        six globally and two per run in its authoritative home.
        """
        self.__require_owner()
        if self.__closed:
            raise ValueError("bridge_broker_closed")
        profiles = tuple(profiles)
        session_id = self.__authority.open_session(binding, capability_file)
        owned = _OwnedContext(session_id, binding, capability_file)
        self.__owned.append(owned)
        try:
            # Keep ownership as soon as a constructor returns, before adapters
            # or the final admission check can fail.
            owned.scope = LocalFileScope(
                self.__authority, session_id, binding, capability_file,
            )
            owned.reader = LocalBoundedFileReader(owned.scope)
            owned.lister = LocalBoundedFileLister(owned.scope)
            if self.__patch_enabled:
                from .bridge_file_patch import LocalBoundedFilePatcher
                owned.patcher = LocalBoundedFilePatcher(
                    self.__authority, session_id, binding, capability_file)
            if profiles:
                owned.processes = LocalOwnedProcessOperations(
                    self.__authority, session_id, binding, capability_file, profiles,
                    **({"process_store": self.__process_store} if self.__process_store is not None else {}),
                )
            self.__require_live(owned)
            context = LocalRunContext(self, session_id, binding, _seal=_CONTEXT_SEAL)
            owned.context = context
            self.__contexts[context] = owned
            return context
        except BaseException:
            failure = self.__close_owned(owned)
            if failure is not None:
                raise ValueError(failure) from None
            raise

    def __require_context(self, context: LocalRunContext) -> _OwnedContext:
        self.__require_owner()
        # Object identity, not a caller-provided session ID, selects resources.
        owned = self.__contexts.get(context)
        if (self.__closed or owned is None or owned.context is not context
                or owned.closed):
            raise ValueError("bridge_broker_context_closed")
        return owned

    def __require_live(self, owned: _OwnedContext) -> None:
        self.__authority.require_session(
            owned.session_id, owned.binding, owned.capability_file,
        )

    def __file_operation(self, context: LocalRunContext,
                         request: WorkerOperationRequest, *, listing: bool
                         ) -> WorkerOperationOutcome:
        owned = self.__require_context(context)
        require_matching_worker_run(owned.binding, request)
        self.__require_live(owned)
        outcome = owned.lister.list(request) if listing else owned.reader.read(request)
        # Adapters buffer bounded output. Recheck even when a native change
        # happened after their own final descriptor/authority observations.
        self.__require_live(owned)
        return validate_worker_outcome(request, outcome)

    def _read(self, context: LocalRunContext,
              request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__file_operation(context, request, listing=False)

    def _list(self, context: LocalRunContext,
              request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__file_operation(context, request, listing=True)

    def _patch(self, context: LocalRunContext, request: WorkerOperationRequest, *, replacement):
        owned = self.__require_context(context)
        require_matching_worker_run(owned.binding, request)
        self.__require_live(owned)
        if owned.patcher is None:
            raise ValueError("bridge_broker_patch_unavailable")
        result = (owned.patcher.reconcile(request) if replacement is None
                  else owned.patcher.apply(request, replacement))
        self.__require_live(owned)
        return result

    def __process_operation(self, context: LocalRunContext,
                            request: WorkerOperationRequest, *, kind: str,
                            cursor: int = 0) -> WorkerOperationOutcome:
        owned = self.__require_context(context)
        require_matching_worker_run(owned.binding, request)
        self.__require_live(owned)
        if owned.processes is None:
            outcome = WorkerOperationOutcome(
                request.operation_id, "REFUSED",
                error_code="bridge_broker_process_unavailable",
            )
        elif kind == "start":
            outcome = owned.processes.start(request)
        elif kind == "poll":
            outcome = owned.processes.poll(request, cursor=cursor)
        else:
            outcome = owned.processes.terminate(request)
        # Recheck even cached/buffered adapter results before exposing bytes.
        self.__require_live(owned)
        return validate_worker_outcome(request, outcome)

    def _process_start(self, context: LocalRunContext,
                       request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__process_operation(context, request, kind="start")

    def _process_poll(self, context: LocalRunContext,
                      request: WorkerOperationRequest, *, cursor: int = 0
                      ) -> WorkerOperationOutcome:
        return self.__process_operation(context, request, kind="poll", cursor=cursor)

    def _process_terminate(self, context: LocalRunContext,
                           request: WorkerOperationRequest) -> WorkerOperationOutcome:
        return self.__process_operation(context, request, kind="terminate")

    def _status(self, context: LocalRunContext) -> dict:
        owned = self.__require_context(context)
        self.__require_live(owned)
        with self.__ledger.connect() as conn:
            row = conn.execute(
                "SELECT r.state AS run_state, t.status AS task_state "
                "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                "WHERE r.run_id=? AND r.task_id=? AND r.attempt=?",
                (owned.binding.run_id, owned.binding.task_id, owned.binding.attempt),
            ).fetchone()
        if (row is None or row["run_state"] != "RUNNING"
                or row["task_state"] != "IN_PROGRESS"):
            raise ValueError("bridge_broker_authority_denied")
        status = {"binding": asdict(owned.binding), "native": {
            "run_state": row["run_state"], "task_state": row["task_state"],
        }}
        self.__require_live(owned)
        return status

    def __close_owned(self, owned: _OwnedContext) -> str | None:
        owned.closed = True
        failure = ("bridge_broker_cleanup_unresolved"
                   if owned.cleanup_unresolved else None)
        if owned.processes is not None:
            try:
                owned.processes.close()
            except BaseException:
                # This owner can safely retry its retained Popen objects;
                # numeric file descriptors remain governed separately below.
                owned.process_cleanup_unresolved = True
                failure = "bridge_broker_cleanup_unresolved"
            else:
                owned.processes = None
                owned.process_cleanup_unresolved = False
        if owned.scope is not None and not owned.cleanup_unresolved:
            try:
                owned.scope.close()
            except BaseException as exc:
                if isinstance(exc, ValueError) and str(exc) == "bridge_file_scope_busy":
                    failure = "bridge_broker_cleanup_failed"
                else:
                    owned.cleanup_unresolved = True
                    failure = "bridge_broker_cleanup_unresolved"
            else:
                owned.scope = None
                owned.reader = None
                owned.lister = None
        # Routing cleanup must run even if a scope remains busy or unresolved.
        if owned.session_open:
            try:
                self.__authority.close_session(owned.session_id)
            except BaseException:
                owned.cleanup_unresolved = True
                failure = "bridge_broker_cleanup_unresolved"
            else:
                owned.session_open = False
        if owned.process_cleanup_unresolved:
            failure = "bridge_broker_cleanup_unresolved"
        if failure is None:
            self.__owned.remove(owned)
            if owned.context is not None:
                self.__contexts.pop(owned.context, None)
        return failure

    def _close_context(self, context: LocalRunContext) -> None:
        self.__require_owner()
        if (not isinstance(context, LocalRunContext)
                or context._LocalRunContext__broker is not self):
            raise ValueError("bridge_broker_context_unowned")
        owned = self.__contexts.get(context)
        if owned is None:
            return
        if owned.context is not context:
            raise ValueError("bridge_broker_context_unowned")
        failure = self.__close_owned(owned)
        if failure is not None:
            raise ValueError(failure)

    def close(self) -> None:
        self.__require_owner()
        self.__closed = True
        failure = None
        for owned in tuple(self.__owned):
            current = self.__close_owned(owned)
            if current == "bridge_broker_cleanup_unresolved" or failure is None:
                failure = current
        if failure is not None:
            raise ValueError(failure)
