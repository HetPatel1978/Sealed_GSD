"""Bind coordinator callbacks; ceilings remain in this process's private vault."""

from sealed.private import vault
from sealed.private.qualifier import qualify
from sealed.private.secret_checker import SecretChecker
from sealed.service.coordinator import Runtime


def for_run(job_id, run_id):
    return Runtime(SecretChecker(job_id),
        lambda quotes, flagged, excluded: qualify(job_id, quotes, flagged, excluded),
        ready=vault.has(job_id))


def run_coordinator_demo():
    """Compose the offline fixture without exposing its private value to orchestration."""
    from sealed.service.sim_demo import run_demo

    vault.store("coordinator-demo", 150.0)
    try:
        return run_demo(runtime=for_run)
    finally:
        vault.forget("coordinator-demo")
