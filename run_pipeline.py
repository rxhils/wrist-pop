"""End-to-end pipeline: Scout → Strategist → Writer → Gate.

Usage:
  python run_pipeline.py                    # run all 4 stages
  python run_pipeline.py --from strategist  # skip scout (use existing report)
  python run_pipeline.py --from writer      # skip scout + strategist
  python run_pipeline.py --from gate        # only run gate
  python run_pipeline.py --only scout       # just one stage
"""

from __future__ import annotations
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

STAGES = [
    "scout", "strategist", "writer", "gate", "asset_director", "visual",
    "manual_reel", "manual_post", "output_director", "session_summary",
]
# Off-chain agents — runnable via --only but not in the default sequence
EXTRA_STAGES = ["scheduler", "render_image", "render_video", "reel_director"]
ALL_STAGES = STAGES + EXTRA_STAGES

def _run(name: str) -> int:
    print(f"\n{'#' * 60}\n# STAGE: {name.upper()}\n{'#' * 60}")
    start = time.time()
    if name == "scout":
        from run_scout import main as scout_main
        rc = scout_main()
    elif name == "strategist":
        from run_strategist import main as strat_main
        rc = strat_main()
    elif name == "writer":
        from run_writer import main as writer_main
        rc = writer_main()
    elif name == "gate":
        from run_gate import main as gate_main
        rc = gate_main()
    elif name == "asset_director":
        from run_asset_director import main as ad_main
        rc = ad_main()
    elif name == "visual":
        from run_visual import main as visual_main
        rc = visual_main()
    elif name == "manual_reel":
        from run_manual_reel import main as mr_main
        rc = mr_main()
    elif name == "manual_post":
        from run_manual_post import main as mp_main
        rc = mp_main()
    elif name == "output_director":
        from run_output_director import main as od_main
        rc = od_main()
    elif name == "scheduler":
        from run_scheduler import main as sched_main
        rc = sched_main()
    elif name == "render_image":
        from run_render_image import main as img_main
        rc = img_main()
    elif name == "render_video":
        from run_render_video import main as vid_main
        rc = vid_main()
    elif name == "reel_director":
        from run_reel_director import main as rd_main
        rc = rd_main()
    elif name == "session_summary":
        from run_session_summary import main as ss_main
        rc = ss_main()
    else:
        raise ValueError(f"unknown stage: {name}")
    elapsed = time.time() - start
    print(f"\n[pipeline] stage '{name}' finished in {elapsed:.1f}s (rc={rc})")
    return rc

def main() -> int:
    parser = argparse.ArgumentParser(description="Royal Pop content pipeline")
    parser.add_argument("--from", dest="from_stage", choices=STAGES, default="scout")
    parser.add_argument("--only", dest="only_stage", choices=ALL_STAGES, default=None,
                        help=f"Run one stage only. Marketing chain: {STAGES}. Extra: {EXTRA_STAGES}.")
    parser.add_argument(
        "--provider", choices=["groq", "mistral", "gemini", "ollama"], default=None,
        help="Override ALL agents to this provider for this run."
    )
    parser.add_argument(
        "--model", default=None,
        help="Override model name (pair with --provider)."
    )
    parser.add_argument(
        "--list-config", action="store_true",
        help="Print current per-agent provider/model + exit.",
    )
    args = parser.parse_args()

    if args.list_config:
        try:
            from pipeline_config import AGENT_CONFIG
            print("\nCurrent per-agent provider assignments:\n")
            for name, cfg in AGENT_CONFIG.items():
                print(f"  {name:<14} -> {cfg.get('provider', '?'):<10} {cfg.get('model') or 'default'}")
            print()
        except ImportError:
            print("pipeline_config.py missing")
        return 0

    if args.provider:
        # set env override so providers.llm_call routes everything via this provider
        import os
        os.environ["PWS_PROVIDER_OVERRIDE"] = args.provider
        if args.model:
            os.environ["PWS_MODEL_OVERRIDE"] = args.model
        print(f"[pipeline] global override: provider={args.provider} model={args.model or 'default'}")

    if args.only_stage:
        stages = [args.only_stage]
    else:
        start_idx = STAGES.index(args.from_stage)
        stages = STAGES[start_idx:]

    today = date.today().isoformat()
    print(f"[pipeline] {today} — running stages: {', '.join(stages)}")

    # Refresh brand snapshot (live site truth) before any agent runs
    try:
        from tools.website_truth import refresh_if_stale
        snap = refresh_if_stale(force=False)
        print(f"[pipeline] brand snapshot status={snap.get('website_status')} cta={snap.get('cta_primary')!r} colourways={len(snap.get('colourways') or [])}")
    except Exception as e:
        print(f"[pipeline] brand snapshot refresh failed (continuing with cached/seed): {e}")

    for stage in stages:
        rc = _run(stage)
        if rc != 0:
            print(f"\n[pipeline] STOP — stage '{stage}' returned {rc}")
            return rc

    out = Path(__file__).parent / "outputs"
    print(f"\n{'=' * 60}\n[pipeline] DONE\n{'=' * 60}")
    print(f"Today's artefacts in {out}:")
    for f in sorted(out.glob(f"*{today}*.json")):
        print(f"  {f.name}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
