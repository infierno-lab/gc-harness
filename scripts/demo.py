"""End-to-end demo of the Phase 0+1 spine: seed → digest → validate → execute →
gates → hashes → determinism → gate failure → fail-closed rejections.

Run with:  make demo   (or: uv run python scripts/demo.py)
Requires the compose Postgres (`make db-up && make migrate`).
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.catalog.digest import render_digest
from core.catalog.seed import apply_seed, grant_all_blocks
from core.db.models import GateVerdict, Tenant
from core.db.session import admin_session, tenant_session
from core.execute.runner import PlanRejectedError, execute_plan
from core.plan.plan_ir import PlanIR, parse_plan_yaml
from core.plan.validator import validate_plan
from packs.demand.catalog_seed import BLOCKS, METRICS

PLAN_PATH = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "elasticity_e2e_plan.yaml"


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def get_or_create_tenant(name: str) -> uuid.UUID:
    with admin_session() as s:
        tenant = s.execute(select(Tenant).where(Tenant.name == name)).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=name, status="active")
            s.add(tenant)
            s.flush()
        return tenant.id


def main() -> None:
    hr("1. SEED — register the demand pack in the catalog (idempotent)")
    demo_tenant = get_or_create_tenant("demo-tenant")
    ungranted_tenant = get_or_create_tenant("ungranted-tenant")
    with admin_session() as s:
        report = apply_seed(s, BLOCKS, METRICS)
        grant_all_blocks(s, demo_tenant)
    print(f"seed report: {report}")
    print(f"tenants: demo={demo_tenant}  ungranted={ungranted_tenant}")

    hr("2. CAPABILITY DIGEST — what the planner would see for this tenant")
    with tenant_session(demo_tenant) as s:
        digest = render_digest(s, demo_tenant)
    print(digest.text)
    print(f"digest hash: {digest.digest_hash}")

    hr("3. VALIDATE — hand-written Plan IR (the LLM's future output format)")
    plan = parse_plan_yaml(PLAN_PATH.read_text())
    print(PLAN_PATH.read_text().strip())
    with tenant_session(demo_tenant) as s:
        result = validate_plan(s, demo_tenant, plan)
    print(f"\nvalid={result.valid}  errors={result.errors}  requires_approval={result.requires_approval}")

    hr("4. EXECUTE — gated run, content hashes on every edge, zero LLM")
    with tenant_session(demo_tenant) as s:
        run1 = execute_plan(s, demo_tenant, plan, actor="demo@gobblecube.ai")
        verdicts = s.execute(
            select(GateVerdict).where(GateVerdict.run_id == uuid.UUID(run1.run_id))
        ).scalars().all()
    print(f"run {run1.run_id}: {run1.status}")
    for node_id, status in run1.node_statuses.items():
        print(f"  {node_id}: {status:10s} in={run1.input_hashes.get(node_id, {})} out={run1.output_hashes.get(node_id, {})}")
    for v in verdicts:
        print(f"  gate verdict: node={v.node_id} suite={v.check_suite} -> {v.verdict}")
    surface = run1.outputs["surface"]
    print(f"  published output 'surface': ref={surface.ref}  summary={surface.summary!r}")

    hr("5. DETERMINISM — same plan again, hashes must be byte-identical")
    with tenant_session(demo_tenant) as s:
        run2 = execute_plan(s, demo_tenant, plan, actor="demo@gobblecube.ai")
    identical = run1.output_hashes == run2.output_hashes
    print(f"run {run2.run_id}: {run2.status}")
    print(f"output hashes identical across independent runs: {identical}")
    assert identical, "determinism violated!"

    hr("6. GATE FAILURE — force bad elasticities; the gate must condemn them")
    bad = plan.model_copy(deep=True)
    for node in bad.nodes:
        if node.id == "n2":
            node.params = {**node.params, "force_positive_elasticities": True}
    with tenant_session(demo_tenant) as s:
        run3 = execute_plan(s, demo_tenant, bad, actor="demo@gobblecube.ai")
        verdicts = s.execute(
            select(GateVerdict).where(GateVerdict.run_id == uuid.UUID(run3.run_id))
        ).scalars().all()
    print(f"run {run3.run_id}: {run3.status}")
    for node_id, status in run3.node_statuses.items():
        print(f"  {node_id}: {status}")
    for v in verdicts:
        print(f"  gate verdict: node={v.node_id} suite={v.check_suite} -> {v.verdict}")
    print(f"  published outputs: {list(run3.outputs) or 'NONE (unpublished, fail-closed)'}")

    hr("7. REJECTED: ungranted tenant — same plan, zero grants")
    try:
        with tenant_session(ungranted_tenant) as s:
            execute_plan(s, ungranted_tenant, plan, actor="intruder@example.com")
        print("!!! executed — should never happen")
    except PlanRejectedError as e:
        codes = sorted({err.code for err in e.errors})
        print(f"PlanRejectedError (no Run row created): error codes = {codes}")

    hr("8. REJECTED: model node without a blocking validation gate")
    gateless = PlanIR(
        plan_ir_version=plan.plan_ir_version,
        intent_summary="elasticity without post_model_checks — invalid by construction",
        nodes=[n.model_copy(deep=True) for n in plan.nodes if n.id != "n3"],
        outputs=dict(plan.outputs),
        assumptions=list(plan.assumptions),
        estimated_cost=plan.estimated_cost,
    )
    try:
        with tenant_session(demo_tenant) as s:
            execute_plan(s, demo_tenant, gateless, actor="demo@gobblecube.ai")
        print("!!! executed — should never happen")
    except PlanRejectedError as e:
        for err in e.errors:
            print(f"PlanRejectedError: {err.code} on {err.node_id}: {err.message}")

    hr("DONE — the spine works end-to-end with no LLM anywhere in the path")


if __name__ == "__main__":
    main()
