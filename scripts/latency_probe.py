"""Repeat-ask latency probe CLI (spec §5.9, §12 Phase-3 exit metric): times
the LLM-free classify -> plan(cache hit) -> execute path against the ~1.5 s
repeat-ask SLO, against a real tenant in the dev DB.

Run with:  uv run python scripts/latency_probe.py --tenant demo-tenant
Requires the compose Postgres (`make db-up && make migrate && make demo`).
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.db.models import Tenant
from core.db.session import admin_session, tenant_session
from evals.latency_probe import DEFAULT_ASK_TEXT, ProbeReport, probe_repeat_ask, seed_plan_template


def _resolve_tenant(name: str) -> uuid.UUID:
    with admin_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.name == name)).scalar_one_or_none()
        if tenant is None:
            print(f"error: no tenant named {name!r} found", file=sys.stderr)
            sys.exit(2)
        return tenant.id


def _print_report(report: ProbeReport) -> None:
    d = report.decomposition
    print(f"samples: n={len(report.samples)}")
    print(f"  p50={report.p50_ms:.1f}ms  p95={report.p95_ms:.1f}ms  (SLO: {report.slo_ms}ms)")
    print(
        f"  decomposition (p50): classify={d.classify_p50:.1f}ms "
        f"plan={d.plan_p50:.1f}ms execute={d.execute_p50:.1f}ms"
    )
    verdict = "WITHIN SLO" if report.within_slo else "SLO BREACH"
    print(f"  {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repeat-ask latency probe: classify -> plan(cache hit) -> execute, vs the 1.5s SLO."
    )
    parser.add_argument("--tenant", default="demo-tenant", help="tenant name to probe (default: demo-tenant)")
    parser.add_argument("--n", type=int, default=20, help="number of repeats to sample (default: 20)")
    args = parser.parse_args()

    tenant_id = _resolve_tenant(args.tenant)

    try:
        with tenant_session(tenant_id) as session:
            seed_plan_template(session, tenant_id, DEFAULT_ASK_TEXT)
            report = probe_repeat_ask(session, tenant_id, DEFAULT_ASK_TEXT, n=args.n)
    except AssertionError as exc:
        print(f"error: probe preconditions not met: {exc}", file=sys.stderr)
        return 2

    _print_report(report)
    return 0 if report.within_slo else 1


if __name__ == "__main__":
    sys.exit(main())
