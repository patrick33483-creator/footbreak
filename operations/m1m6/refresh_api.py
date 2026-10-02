"""Loopback-only refresh trigger; nginx supplies the existing authenticated UI."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self,code,obj):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers();self.wfile.write(data)

    def do_POST(self):
        if self.path!="/refresh":
            return self.reply(404,{"error":"not_found"})
        # Cross-site forms cannot supply this header, and no CORS is enabled.
        if self.headers.get("X-Crown-Refresh")!="1" or not self.headers.get("X-Authenticated-User"):
            return self.reply(403,{"error":"authentication_required"})
        origin=self.headers.get("Origin")
        if origin and urlparse(origin).netloc!=self.headers.get("Host"):
            return self.reply(403,{"error":"origin_mismatch"})
        active=subprocess.run(["systemctl","is-active","crown-strategy-search.service"],capture_output=True,text=True)
        busy=active.stdout.strip() in ("active","activating","reloading")
        if not busy:
            result=subprocess.run(["systemctl","start","--no-block","crown-strategy-search.service"],
                                  capture_output=True,timeout=10)
            if result.returncode:
                return self.reply(503,{"error":"start_failed"})
        self.reply(202,{"accepted":True,"busy":busy})


if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",8786),Handler).serve_forever()
