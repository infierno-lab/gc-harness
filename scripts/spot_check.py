"""Reproducibility spot-check CLI (spec §9): re-execute a sample of a
tenant's recent succeeded plans and diff output hashes against the original
run's recorded hashes.

Run with:  uv run python scripts/spot_check.py --tenant demo-tenant
Requires the compose Postgres (`make db-up && make migrate`).
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.db.models import Tenant
from core.db.session import admin_session, tenant_session
from core.execute.spot_check import SpotCheckReport, run_spot_check


def _resolve_tenant(name: str) -> uuid.UUID:
    with admin_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.name == name)).scalar_one_or_none()
        if tenant is None:
            print(f"error: no tenant named {name!r} found", file=sys.stderr)
            sys.exit(2)
        return tenant.id


def _print_report(report: SpotCheckReport) -> None:
    if not report.checked and not report.skipped:
        print("nothing to check — no succeeded runs found for this tenant")
        return

    for result in report.checked:
        mark = "✓" if result.match else "✗"
        print(
            f"  {mark} plan {result.plan_ir_hash[:12]}  "
            f"original={result.original_run_id[:8]} replay={result.replay_run_id[:8]}"
        )
        for mismatch in result.mismatches:
            print(
                f"      MISMATCH node={mismatch.node_id} port={mismatch.port} "
                f"original={(mismatch.original_hash or '<none>')[:12]} "
                f"replay={(mismatch.replay_hash or '<none>')[:12]}"
            )

    for skip in report.skipped:
        print(f"  ? skipped run={skip.run_id[:8]}: {skip.reason}")

    print(f"\nchecked={len(report.checked)} skipped={len(report.skipped)} all_match={report.all_match}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducibility spot-check: replay recent plans, diff hashes.")
    parser.add_argument("--tenant", default="demo-tenant", help="tenant name to sweep (default: demo-tenant)")
    parser.add_argument("--sample-size", type=int, default=3, help="max distinct plans to replay (default: 3)")
    args = parser.parse_args()

    tenant_id = _resolve_tenant(args.tenant)

    with tenant_session(tenant_id) as session:
        report = run_spot_check(session, tenant_id, sample_size=args.sample_size, actor="spot-check-cli")

    _print_report(report)

    if not report.checked and not report.skipped:
        return 2
    return 0 if report.all_match else 1


if __name__ == "__main__":
    sys.exit(main())
