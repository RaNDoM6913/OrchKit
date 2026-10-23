"""Trusted in-process ownership of independent local read/list/status contexts.

Routing identities remain volatile and every operation checks native authority.
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
from .worker_operations import (
    WorkerOperationOutcome, WorkerOperationRequest, WorkerRunBinding,
    require_matching_worker_run, validate_worker_outcome,
)


_CONTEXT_SEAL = object()


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
    context: LocalRunContext | None = None
    closed: bool = False
    session_open: bool = True
    cleanup_unresolved: bool = False


class LocalRunBroker:
    """Own one private native authority and the resources of its contexts.

    Explicit close releases only this broker's routing slots and file scopes.
    Known-busy scopes remain owned for an explicit close after handles unwind.
    An ambiguous scope-close failure remains visible and is never retried: the
    underlying numeric descriptor could already have been reused elsewhere.
    """

    def __init__(self, ledger: AuthorityLedger, *, max_worker_sessions: int = 3):
        self.__authority = LocalRunSessionAuthority(
            ledger, max_worker_sessions=max_worker_sessions,
        )
        self.__ledger = ledger
        self.__owner = (os.getpid(), threading.get_ident())
        self.__owned: list[_OwnedContext] = []
        self.__contexts: dict[LocalRunContext, _OwnedContext] = {}
        self.__closed = False

    def __require_owner(self) -> None:
        if self.__owner != (os.getpid(), threading.get_ident()):
            raise ValueError("bridge_broker_owner_mismatch")

    def open_context(self, binding: WorkerRunBinding,
                     capability_file: Path) -> LocalRunContext:
        self.__require_owner()
        if self.__closed:
            raise ValueError("bridge_broker_closed")
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
