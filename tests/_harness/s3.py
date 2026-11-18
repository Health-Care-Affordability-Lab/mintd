"""An S3 server for tests that need real dvc, or a mintd subprocess, to talk to S3.

moto's server app behind a small WSGI layer that refuses ListObjectVersions
with 403, as the lab's `mintd` key is refused, and records every object PUT
and every successful object GET with the bytes it served. A story reads those
records to prove what a command uploaded or downloaded, which the command's
own output can only claim.
"""

from __future__ import annotations

import dataclasses
import threading
import urllib.request
from typing import Any
from urllib.parse import parse_qs

import boto3
import pytest

BUCKET = "journey-bucket"


@dataclasses.dataclass
class S3Server:
    endpoint: str
    client: Any
    #: the path (`/<bucket>/<key>`) of each object PUT, in order
    puts: list[str]
    #: `(path, bytes served)` of each object GET that succeeded, in order
    gets: list[tuple[str, int]]


@pytest.fixture
def s3_server():
    from moto.moto_server.werkzeug_app import DomainDispatcherApplication, create_backend_app
    from werkzeug.serving import make_server

    app = DomainDispatcherApplication(create_backend_app)
    puts: list[str] = []
    gets: list[tuple[str, int]] = []

    def guarded(environ, start_response):
        method, query = environ["REQUEST_METHOD"], parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        path = environ.get("PATH_INFO", "/")
        if method == "GET" and "versions" in query:
            start_response("403 Forbidden", [("Content-Type", "application/xml")])
            return [b'<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>no</Message></Error>']
        is_object = path.strip("/").count("/") >= 1
        if method == "PUT" and is_object:
            puts.append(path)
        if method == "GET" and is_object:
            status: list[str] = []

            def recording(code, headers, exc_info=None):
                status.append(code)
                return start_response(code, headers, exc_info)

            response = app(environ, recording)
            try:
                body = b"".join(response)
            finally:
                getattr(response, "close", lambda: None)()
            if status[0].startswith("2"):  # a 404 or an injected 500 served no object
                gets.append((path, len(body)))
            return [body]
        return app(environ, start_response)

    server = make_server("127.0.0.1", 0, guarded, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    # moto's backends are process-wide: start each test from an empty server.
    urllib.request.urlopen(urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")).close()
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    puts.clear()
    try:
        yield S3Server(endpoint, s3, puts, gets)
    finally:
        server.shutdown()
