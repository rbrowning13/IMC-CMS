"""
AI routing handlers.

Each handler:
- Receives the full chat context
- Either returns a ChatTurn or None
- Performs NO side effects beyond thread_state updates it explicitly owns

chat_engine.py will iterate through these in order.
"""

from typing import Optional, Callable, List

# Type alias to avoid circular imports at this stage
ChatTurn = object


# -------------------------------------------------------------------
# Routing handler signatures
# -------------------------------------------------------------------

HandlerFn = Callable[..., Optional[ChatTurn]]


# -------------------------------------------------------------------
# Routing handlers (stubs – to be populated incrementally)
# -------------------------------------------------------------------

def route_claims(*args, **kwargs) -> Optional[ChatTurn]:
    return None


def route_billables(*args, **kwargs) -> Optional[ChatTurn]:
    return None


def route_invoices(*args, **kwargs) -> Optional[ChatTurn]:
    return None


def route_reports(*args, **kwargs) -> Optional[ChatTurn]:
    return None


def route_system_health(*args, **kwargs) -> Optional[ChatTurn]:
    question = kwargs.get("question", "")
    q = (kwargs.get("q") or question or "").lower()
    context = kwargs.get("context")
    db = kwargs.get("db")

    # Health‑specific intent
    if not any(
        k in q
        for k in [
            "health",
            "server",
            "disk",
            "storage",
            "backup",
            "uptime",
            "memory",
            "status",
        ]
    ):
        return None

    # Health queries are allowed even without explicit system scope
    # (UI does not always provide system context)
    if context is not None and getattr(context, "scope", None) not in (None, "system"):
        return None

    try:
        from system.health import basic_health_snapshot
        from ai.chat_engine import make_answer

        health = basic_health_snapshot(data_path="/")

        lines = []

        disk = health.get("disk", {})
        if disk:
            free = disk.get("free_gb")
            total = disk.get("total_gb")
            if free is not None and total is not None:
                lines.append(
                    f"Disk: {free:.1f}GB free of {total:.1f}GB"
                )

        mem = health.get("memory_mb")
        if mem is not None:
            lines.append(f"Memory: {mem:.0f}MB used")

        uptime = health.get("uptime_seconds")
        if uptime:
            hours = uptime / 3600
            lines.append(f"Uptime: {hours:.1f} hours")

        if not lines:
            lines.append("System health information is available but limited.")

        return make_answer(
            text="System health:\n" + "\n".join(lines),
            sources=["SYSTEM.HEALTH"],
            scope="system",
        )

    except Exception:
        return make_answer(
            text="System health status is unavailable.",
            sources=["SYSTEM.HEALTH"],
            scope="system",
        )


def route_workload(*args, **kwargs) -> Optional[ChatTurn]:
    question = kwargs.get("question", "")
    q = (kwargs.get("q") or question or "").lower()

    context = kwargs.get("context")
    db = kwargs.get("db")
    BillableItemModel = kwargs.get("BillableItemModel")
    SettingsModel = kwargs.get("SettingsModel")

    if not any(
        k in q
        for k in [
            "workload",
            "busy",
            "overloaded",
            "how am i doing",
            "am i behind",
            "capacity",
            "hours",
        ]
    ):
        return None

    if not context or getattr(context, "scope", None) != "system":
        return None

    from ai.sources import answer_workload_overview
    from ai.chat_engine import make_answer

    text = answer_workload_overview(
        db=db,
        BillableItemModel=BillableItemModel,
        SettingsModel=SettingsModel,
    )

    return make_answer(
        text=text,
        sources=["BILLABLES", "SETTINGS"],
        scope="system",
    )

def route_system(*args, **kwargs) -> Optional[ChatTurn]:
    question = kwargs.get("question", "")
    q = (kwargs.get("q") or kwargs.get("question") or "").lower()

    context = kwargs.get("context")
    db = kwargs.get("db")
    ClaimModel = kwargs.get("ClaimModel")
    InvoiceModel = kwargs.get("InvoiceModel")
    BillableItemModel = kwargs.get("BillableItemModel")
    ProviderModel = kwargs.get("ProviderModel")
    EmployerModel = kwargs.get("EmployerModel")
    CarrierModel = kwargs.get("CarrierModel")

    # Basic system-level intent detection (exclusive: no health/status/server/disk/storage/backup/uptime)
    if not any(
        k in q
        for k in [
            "system",
            "overview",
            "snapshot",
            "summary",
        ]
    ):
        return None

    # Only answer when context is system-scoped
    if not context or getattr(context, "scope", None) != "system":
        return None

    from ai.sources import answer_system_overview
    from ai.chat_engine import make_answer

    text = answer_system_overview(
        db=db,
        ClaimModel=ClaimModel,
        InvoiceModel=InvoiceModel,
        BillableItemModel=BillableItemModel,
        ProviderModel=ProviderModel,
        EmployerModel=EmployerModel,
        CarrierModel=CarrierModel,
    )
    sources = ["SYSTEM"]

    return make_answer(
        text=text,
        sources=sources,
        scope="system",
    )


def route_fallback(*args, **kwargs) -> Optional[ChatTurn]:
    return None


# -------------------------------------------------------------------
# Ordered routing table
# -------------------------------------------------------------------

ROUTES: List[HandlerFn] = [
    route_claims,
    route_billables,
    route_invoices,
    route_reports,
    route_workload,
    route_system_health,
    route_system,
    route_fallback,
]