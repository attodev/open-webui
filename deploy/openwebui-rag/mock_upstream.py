# OpenAI 호환 가짜 업스트림 (테스트용). /v1/models, /v1/chat/completions(stream 포함), /v1/embeddings
# 실제 공급자 키 없이 new-api → 업스트림 경로와 과금 로그를 확인할 때 쓴다. 표준 라이브러리만 사용한다.
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-gpt"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 조용히
        pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/v1/embeddings"):
            inp = body.get("input")
            count = len(inp) if isinstance(inp, list) else 1
            return self._json(200, {
                "object": "list", "model": MODEL,
                "data": [{"object": "embedding", "index": i, "embedding": [0.1] * 8} for i in range(count)],
                "usage": {"prompt_tokens": 5 * count, "total_tokens": 5 * count},
            })
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        last = (body.get("messages") or [{}])[-1].get("content", "")
        text = "Mock reply to: " + (last if isinstance(last, str) else "[content]")[:60]
        rid = "chatcmpl-" + uuid.uuid4().hex[:12]
        now = int(time.time())
        usage = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for piece in [text[i:i + 8] for i in range(0, len(text), 8)]:
                chunk = {"id": rid, "object": "chat.completion.chunk", "created": now, "model": MODEL,
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.02)
            final = {"id": rid, "object": "chat.completion.chunk", "created": now, "model": MODEL,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}
            self.wfile.write(("data: " + json.dumps(final) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json(200, {"id": rid, "object": "chat.completion", "created": now, "model": MODEL,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                         "usage": usage})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
