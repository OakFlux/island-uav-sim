"""Public command-line entry point."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser=argparse.ArgumentParser(description='Weather-constrained island UAV inspection simulation.')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run',help='Run one JSON-configured simulation and audit its event log.')
    run.add_argument('--config',type=Path,required=True)
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--replay',action='store_true',help='Also save browser replay.json.')
    web=commands.add_parser('serve',help='Open the local 2D browser interface.')
    web.add_argument('--host',default='127.0.0.1')
    web.add_argument('--port',default=8000,type=int)
    web.add_argument('--open',action='store_true')
    reproduce=commands.add_parser('reproduce',help='Reproduce the selected full paper case and compare its archived metrics.')
    reproduce.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='serve':
        from .server import serve
        serve(args.host,args.port,args.open)
        return
    from .runner import run_simulation
    if args.command=='reproduce':
        config=Path(__file__).parent/'data'/'paper_case.json'
    else:
        config=args.config
    result=run_simulation(config,args.output)
    if args.command=='run' and args.replay:
        from .replay import make_replay
        (args.output/'replay.json').write_text(json.dumps(make_replay(result),ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
    if args.command=='reproduce':
        from .reproduction import compare_reference
        comparison=compare_reference(result['audit'])
        (args.output/'reference_comparison.json').write_text(json.dumps(comparison,indent=2)+'\n')
        if comparison['status']!='pass':
            raise SystemExit('Selected paper reference comparison failed; see reference_comparison.json.')
        print('Selected paper case reproduced; archived metrics match.')
    print(json.dumps({'status':result['audit'].get('status'),'output':str(args.output)},indent=2))


if __name__=='__main__':
    main()
