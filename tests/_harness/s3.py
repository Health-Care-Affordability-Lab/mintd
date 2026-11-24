"""An S3 server for tests that need real dvc, or a mintd subprocess, to talk to S3.

moto's server app behind a small WSGI layer that refuses ListObjectVersions
with 403, as the lab's `mintd` key is refused, and records every object PUT
and every successful object GET with the bytes it served. A story reads those
records to prove what a command uploaded or downloaded, which the command's
own output can only claim. A story can also fail the GETs of named keys, with
the 5xx real S3 and Wasabi sent.
"""

from __future__ import annotations

import base64
import dataclasses
import threading
import zlib
from http import HTTPStatus
import urllib.request
from typing import Any
from urllib.parse import parse_qs

import boto3
import pytest

BUCKET = "journey-bucket"
_ERROR_CODES = {500: "InternalError", 503: "SlowDown"}


@dataclasses.dataclass
class S3Server:
    endpoint: str
    client: Any
    #: the path (`/<bucket>/<key>`) of each object PUT, in order
    puts: list[str]
    #: `(path, bytes served)` of each object GET that succeeded, in order
    gets: list[tuple[str, int]]
    #: Set to k to stop an upload partway: once `puts` holds k paths, each
    #: later object PUT sets `holding`, waits for `release`, then fails with
    #: 503 and stores nothing. It is not recorded in `puts`.
    hold_puts_after: int | None = None
    holding: threading.Event = dataclasses.field(default_factory=threading.Event)
    release: threading.Event = dataclasses.field(default_factory=threading.Event)
    #: `{path /<bucket>/<key>: (status, n)}`: the next n object GETs of that
    #: path answer `status` (500 or 503), every GET until removed when n is
    #: None. The error body carries an `x-amz-checksum-crc32` header, as real S3
    #: and Wasabi sent: botocore then wraps it in StreamingChecksumBody and
    #: raises AttributeError instead of the 5xx (the failure #68 fixed). HEAD
    #: still answers. Each failed GET is recorded in `failed_gets`, not `gets`.
    fail_gets: dict[str, tuple[int, int | None]] = dataclasses.field(default_factory=dict)
    failed_gets: list[str] = dataclasses.field(default_factory=list)


@pytest.fixture
def s3_server():
    from moto.moto_server.werkzeug_app import DomainDispatcherApplication, create_backend_app
    from werkzeug.serving import make_server

    app = DomainDispatcherApplication(create_backend_app)
    puts: list[str] = []
    gets: list[tuple[str, int]] = []
    recorder = S3Server("", None, puts, gets)  # endpoint and client once the server is up
    put_lock = threading.Lock()

    def guarded(environ, start_response):
        method, query = environ["REQUEST_METHOD"], parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        path = environ.get("PATH_INFO", "/")
        if method == "GET" and "versions" in query:
            start_response("403 Forbidden", [("Content-Type", "application/xml")])
            return [b'<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>no</Message></Error>']
        is_object = path.strip("/").count("/") >= 1
        if method == "PUT" and is_object:
            with put_lock:
                held = recorder.hold_puts_after is not None and len(puts) >= recorder.hold_puts_after
                if not held:
                    puts.append(path)
            if held:
                recorder.holding.set()
                recorder.release.wait(timeout=60)
                start_response("503 Slow Down", [("Content-Type", "application/xml")])
                return [b'<?xml version="1.0"?><Error><Code>SlowDown</Code><Message>held</Message></Error>']
        if method == "GET" and is_object and path in recorder.fail_gets:
            with put_lock:
                code, left = recorder.fail_gets.get(path, (0, 0))
                failing = left is None or left > 0
                if failing:
                    if left is not None:
                        recorder.fail_gets[path] = (code, left - 1)
                    recorder.failed_gets.append(path)
            if failing:
                body = (f'<?xml version="1.0"?><Error><Code>{_ERROR_CODES.get(code, "InternalError")}</Code>'
                        f"<Message>injected</Message></Error>").encode()
                crc = base64.b64encode(zlib.crc32(body).to_bytes(4, "big")).decode()
                start_response(f"{code} {HTTPStatus(code).phrase}",
                               [("Content-Type", "application/xml"), ("x-amz-checksum-crc32", crc)])
                return [body]
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
    recorder.endpoint, recorder.client = endpoint, s3
    try:
        yield recorder
    finally:
        recorder.release.set()
        server.shutdown()
