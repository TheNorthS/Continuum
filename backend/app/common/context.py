from contextvars import ContextVar

# None is reserved for local operator/maintenance processes. HTTP requests start with {} (untrusted).
actor = ContextVar("security_actor", default=None)
request_meta = ContextVar("security_request", default=None)

# Request/task-local intersection with ACLs; None keeps the legacy all-accessible scope.
knowledge_scope: ContextVar[tuple[str, ...] | None] = ContextVar("knowledge_scope", default=None)
