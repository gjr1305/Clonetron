"""
Clonetron portable test.

Checks that this laptop lets Python run from a copied folder and show a page in
the browser, which is how the Clonetron analysis app will work. Uses only
Python's built-in modules, so nothing needs installing.
"""
import http.server
import os
import platform
import socketserver
import sys
import tempfile
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


def can_write_here():
    try:
        with tempfile.NamedTemporaryFile(dir=HERE, delete=True):
            return "yes"
    except Exception as e:
        return f"no ({e})"


PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><title>Clonetron test</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;line-height:1.5}}
h1{{color:#2e7d32}} td{{padding:4px 12px 4px 0}} code{{background:#eee;padding:2px 4px}}</style></head>
<body><h1>It works.</h1>
<p>Python ran from a copied folder and opened this page. Clonetron should run on this laptop.</p>
<table>
<tr><td>Python</td><td><code>{sys.version.split()[0]}</code></td></tr>
<tr><td>Python file</td><td><code>{sys.executable}</code></td></tr>
<tr><td>Windows / OS</td><td><code>{platform.platform()}</code></td></tr>
<tr><td>Folder</td><td><code>{HERE}</code></td></tr>
<tr><td>Can save files in this folder</td><td><code>{can_write_here()}</code></td></tr>
</table>
<p>Take a screenshot of this page and send it to Geofrey, then close this tab
and the black window.</p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        print("Clonetron test running at", url)
        print("If the browser doesn't open by itself, copy that address into it.")
        print("Close this window when you're done.")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        srv.serve_forever()


if __name__ == "__main__":
    main()
