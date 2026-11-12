"""The few aw-server REST calls the watcher needs, with the standard library only."""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional


class Client:
    def __init__(self, host: str = "localhost", port: int = 5600, timeout: float = 10):
        self.base = f"http://{host}:{port}/api/0"
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 304:  # the bucket already exists
                return None
            raise
        return json.loads(payload) if payload else None

    def info(self) -> dict:
        return self._call("GET", "/info")

    def ensure_bucket(self, bucket_id: str, bucket_type: str, hostname: str, client: str) -> None:
        if bucket_id in self._call("GET", "/buckets/"):
            return
        body = {"client": client, "type": bucket_type, "hostname": hostname}
        self._call("POST", f"/buckets/{urllib.parse.quote(bucket_id)}", body)

    def insert(self, bucket_id: str, events: List[dict]) -> None:
        self._call("POST", f"/buckets/{urllib.parse.quote(bucket_id)}/events", events)
