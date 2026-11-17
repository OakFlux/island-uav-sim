"""Command-line access to demonstrations, replay and selected reference cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Wind-constrained island UAV inspection simulation.")
    commands = parser.add_subparsers(dest="command",required=True)
    run = commands.add_parser("run",help="Run the default demo or a JSON configuration.")
    run.add_argument("--config",type=Path)
    run.add_argument("--output",type=Path,required=True)
    run.add_argument("--replay",action="store_true",help="Also save browser replay.json.")
    web = commands.add_parser("serve",help="Open the local 2D browser interface.")
    web.add_argument("--host",default="127.0.0.1")
    web.add_argument("--port",default=8000,type=int)
    web.add_argument("--open",action="store_true")
    reproduce = commands.add_parser("reproduce",help="Re-run selected archived historical reference inputs.")
    reproduce.add_argument("--case",choices=("compact","network","all"),default="all")
    reproduce.add_argument("--output",type=Path,required=True)
    audit = commands.add_parser("audit",help="Recalculate accounts from saved output and check file integrity.")
    audit.add_argument("folder",type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        from .server import serve
        serve(args.host,args.port,args.open)
        return
    if args.command == "audit":
        from .runner import audit_saved_run
        report = audit_saved_run(args.folder)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if report["status"] != "pass":
            raise SystemExit(1)
        return
    if args.command == "reproduce":
        from .reproduction import run_reference
        selected = ("compact","network") if args.case == "all" else (args.case,)
        reports = []
        for case in selected:
            result = run_reference(case,args.output/case)
            reports.append(dict(case=case,status=result["reference_comparison"]["status"],
                                source_run_id=result["reference_comparison"]["source_run_id"]))
        print(json.dumps(dict(scope="Selected historical cases only",cases=reports),indent=2))
        if any(report["status"] != "pass" for report in reports):
            raise SystemExit(1)
        return
    from .runner import run_simulation
    result = run_simulation(args.config,args.output)
    if args.replay:
        from .replay import make_replay
        (args.output/"replay.json").write_text(json.dumps(make_replay(result),ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n",encoding="utf-8")
    print(json.dumps(dict(status=result["audit"]["status"],output=str(args.output),
                          purpose=result["metadata"]["run_purpose"],metrics=result["physical"]["metrics"],
                          delivery_metrics=result["delivery"]["metrics"]),indent=2))
    if result["audit"]["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
