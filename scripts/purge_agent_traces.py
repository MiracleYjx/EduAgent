"""Run controlled 30-day Agent Trace retention maintenance (no worker)."""

from backend.app.core.database import get_session_factory
from backend.app.services.trace_service import TraceService


def main() -> None:
    removed = TraceService(get_session_factory()).purge_expired()
    print(f"Expired Agent Trace events removed: {removed}")


if __name__ == "__main__":
    main()
