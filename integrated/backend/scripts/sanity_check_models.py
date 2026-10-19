"""
Standalone sanity check for the Model 4 / Model 5 integration.

Bypasses the API, auth, and WebSocket layers entirely -- this only tests
whether MLHealthIndexModel and MLRULPredictor are wired correctly and
producing sane numbers. Run this BEFORE testing through the full app stack,
so a problem here doesn't get confused with an auth/routing/websocket issue.

Usage (from the backend/ directory, so `vendor` resolves the same way the
app does):
    python -m scripts.sanity_check_models
"""
from __future__ import annotations

import numpy as np

from vendor.aerotwin_physics.simulator import RealTimeMonitor, TrajectorySimulator


def main() -> None:
    print("=" * 70)
    print("MODEL 4 / MODEL 5 INTEGRATION SANITY CHECK")
    print("=" * 70)

    # ------------------------------------------------------------
    # 1. Confirm the artefacts actually loaded (not silently
    #    fell back to the old formulas)
    # ------------------------------------------------------------
    monitor = RealTimeMonitor()

    model4_ok = monitor.health_model._model is not None
    model5_ok = monitor.rul_model._model is not None

    print("\nArtefact load status:")
    print(f"  Model 4 (health)  : {'LOADED' if model4_ok else 'FALLBACK -- not loaded!'}")
    print(f"  Model 5 (RUL)     : {'LOADED' if model5_ok else 'NOT LOADED -- will raise on estimate()!'}")

    if not (model4_ok and model5_ok):
        print("\nSTOP: fix artefact paths before continuing -- results below "
              "would be using fallbacks / would fail, not testing what you "
              "actually want to test.")
        return

    # ------------------------------------------------------------
    # 2. Generate one synthetic degrading trajectory (regime="rul",
    #    i.e. reaches the failure threshold within the mission -- the
    #    scenario Model 4/5 should respond most visibly to) and replay
    #    it through the monitor exactly as live telemetry would arrive.
    # ------------------------------------------------------------
    sim = TrajectorySimulator()
    trajectory = sim.simulate(index=0, regime="rul", force_fault=False)
    print(f"\nSimulated trajectory: {trajectory.trajectory_id} "
          f"({len(trajectory.times_s)} samples, "
          f"engine {trajectory.engine_id})")

    outputs = monitor.replay(trajectory)

    # ------------------------------------------------------------
    # 3. Inspect health index and RUL over the trajectory
    # ------------------------------------------------------------
    health_series = [o.health_index for o in outputs]
    rul_median = [o.rul.median for o in outputs if o.rul is not None]
    n_with_rul = len(rul_median)

    print(f"\nSamples with an RUL estimate: {n_with_rul} / {len(outputs)} "
          f"(early samples are expected to have none -- "
          f"min_samples_for_estimate)")

    print(f"\nHealth index over trajectory:")
    print(f"  first: {health_series[0]:.4f}")
    print(f"  last : {health_series[-1]:.4f}")
    print(f"  min  : {min(health_series):.4f}")
    print(f"  (expect: starts near 1.0, decreases toward the end for "
          f"a 'rul' regime trajectory -- if it's flat or increasing, "
          f"something is wrong)")

    if rul_median:
        print(f"\nRUL estimate over trajectory (seconds):")
        print(f"  first estimate: {rul_median[0]:.1f}s ({rul_median[0]/60:.1f} min)")
        print(f"  last estimate : {rul_median[-1]:.1f}s ({rul_median[-1]/60:.1f} min)")
        print(f"  (expect: should trend DOWN as the mission progresses and "
              f"health degrades -- flat or noisy-but-not-trending is a red "
              f"flag)")

    anomalies_flagged = sum(1 for o in outputs if o.residual.anomaly_flag)
    print(f"\nAnomaly flags raised: {anomalies_flagged} / {len(outputs)} samples")

    print("\n" + "=" * 70)
    print("Compare the health-index and RUL trends above against the "
          "notebooks' own validation numbers (Model 4: MAE~0.028 on "
          "similar 'rul'-regime missions; Model 5: MAE~805 cycles / "
          "~13.4 hours). Wildly different scale or a flat/non-responsive "
          "series -- not just an imperfect number -- means the wiring, "
          "not the model, is the problem: check feature order and the "
          "healthy-baseline JSON first.")
    print("=" * 70)


if __name__ == "__main__":
    main()