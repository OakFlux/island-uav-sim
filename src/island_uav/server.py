"""Small local browser/server application, using only the Python standard library."""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlsplit

from .replay import make_replay
from .runner import default_config, run_simulation, validate_config

WEB = Path(__file__).resolve().parent / 'web'
def validate_options(value: object) -> dict:
    """Use the same public configuration contract as the command line."""
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a JSON object.')
    return validate_config(value)


def serve(host: str = '127.0.0.1', port: int = 8000, open_browser: bool = False) -> None:
    lock, cache = Lock(), {}

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'no-cache')
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, status: int, payload: object) -> None:
            self.send_bytes(status,json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode(), 'application/json; charset=utf-8')

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path == '/api/options':
                self.send_json(200, {'default': default_config(), 'networks': {
                    'legacy_24': {'sites':24,'bases':1,'fleets':[2,4,8]},
                    'pingtan_48': {'sites':48,'bases':3,'fleets':[6,12,24]},
                    'pingtan_96': {'sites':96,'bases':3,'fleets':[6,12,24]},
                    'pingtan_192': {'sites':192,'bases':3,'fleets':[6,12,24]}}})
                return
            if path == '/api/default':
                try:
                    with lock:
                        if 'default' not in cache:
                            cache['default'] = make_replay(run_simulation(default_config()))
                        replay = cache['default']
                    self.send_json(200,replay)
                except Exception as exc:
                    self.send_json(500,{'error':str(exc)})
                return
            if path == '/api/map':
                destination = WEB/'map.geojson'
            else:
                destination = (WEB/('index.html' if path=='/' else path.lstrip('/'))).resolve()
            if not destination.is_relative_to(WEB.resolve()) or not destination.is_file():
                self.send_json(404,{'error':'Not found'});return
            mime = mimetypes.guess_type(destination.name)[0] or 'application/octet-stream'
            if destination.suffix=='.geojson':mime='application/geo+json'
            self.send_bytes(200,destination.read_bytes(),mime)

        def do_POST(self) -> None:
            if urlsplit(self.path).path != '/api/run':
                self.send_json(404,{'error':'Not found'});return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16384:
                    self.send_json(413,{'error':'Expected a small JSON configuration.'});return
                options = validate_options(json.loads(self.rfile.read(length)))
                with lock:
                    replay = make_replay(run_simulation(options))
                self.send_json(200,replay)
            except (ValueError,TypeError,json.JSONDecodeError) as exc:
                self.send_json(400,{'error':str(exc)})
            except Exception as exc:
                self.send_json(500,{'error':str(exc)})

        def log_message(self, fmt: str, *args: object) -> None:
            if len(args)>1 and str(args[1]).startswith(('4','5')):
                super().log_message(fmt,*args)

    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    url = f'http://{host}:{server.server_port}'
    print(f'Island UAV simulation: {url}\nPress Ctrl+C to stop.',flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        server.server_close()
