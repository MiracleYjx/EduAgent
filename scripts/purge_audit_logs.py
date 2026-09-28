"""Run controlled audit retention maintenance (no background worker)."""

from backend.app.core.database import get_session_factory
from backend.app.services.audit_service import AuditService


def main() -> None:
    removed = AuditService(get_session_factory()).purge_expired()
    print(f"Expired audit events removed: {removed}")


if __name__ == "__main__":
    main()
