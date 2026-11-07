"""標準ライブラリ（urllib）の取得。127.0.0.1 の一時サーバーだけを使い、外のネットワークには出ない。"""
import json, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from queryu import Cache, HTTPStatusError, PoliteFetcher, Response, UrllibClient

BODY = "<html>本文</html>".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        if self.path == "/page":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Last-Modified", "Wed, 01 Oct 2025 00:00:00 GMT")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/page")
            self.end_headers()
        elif self.path == "/down":
            self.send_response(503)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _client():
    # 環境変数のプロキシを経由させない（127.0.0.1 の一時サーバーに直接つなぐ）
    return UrllibClient(headers={"User-Agent": "queryu-test"}, timeout=5,
                        opener=urllib.request.build_opener(urllib.request.ProxyHandler({})))


def test_status_body_and_case_insensitive_headers(server):
    r = _client().get(server + "/page")
    assert r.status_code == 200 and r.content == BODY
    assert r.headers.get("etag") == '"v1"' and r.headers.get("last-modified") == "Wed, 01 Oct 2025 00:00:00 GMT"
    assert _Handler.seen[-1][1]["User-Agent"] == "queryu-test"


def test_304_404_and_5xx_are_responses_not_exceptions(server):
    c = _client()
    assert c.get(server + "/page", headers={"If-None-Match": '"v1"'}).status_code == 304
    assert c.get(server + "/missing").status_code == 404
    down = c.get(server + "/down")
    assert down.status_code == 503
    with pytest.raises(HTTPStatusError):
        down.raise_for_status()


def test_redirect_is_followed(server):
    r = _client().get(server + "/moved")
    assert r.status_code == 200 and r.url.endswith("/page") and r.content == BODY


def test_polite_fetcher_with_real_urllib_conditional_get_and_cache(server, tmp_path):
    cache = Cache(tmp_path / "cache")
    f = PoliteFetcher(cache, wait=0, client=_client())
    assert f.get("k", server + "/page", "2026").encode("utf-8") == BODY
    assert f.get("k", server + "/page", "2026", live=True).encode("utf-8") == BODY  # 304 → 保存済みの本文
    assert f.get("gone", server + "/missing", "2026") == ""  # 404 は「空」として記録
    with pytest.raises(HTTPStatusError):
        f.get("down", server + "/down", "2026")
    statuses = [json.loads(line)["status"] for line in (tmp_path / "cache" / "manifest.jsonl").read_text().splitlines()]
    assert statuses == [200, 304, 404]  # 5xx は保存しない
    assert _Handler.seen[1][1].get("If-None-Match") == '"v1"'
    assert _Handler.seen[1][1].get("If-Modified-Since") == "Wed, 01 Oct 2025 00:00:00 GMT"


def test_response_text_and_headers_mapping():
    r = Response(200, text="あ", headers={"ETag": '"x"'})
    assert r.content == "あ".encode("utf-8") and r.headers.get("etag") == '"x"'
    r.raise_for_status()
