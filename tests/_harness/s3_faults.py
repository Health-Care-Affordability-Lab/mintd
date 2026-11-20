"""A real S3 fault for an in-process moto client: the 5xx botocore mislabels.

botocore 1.43.0 wraps a GetObject error body in ``StreamingChecksumBody`` when the
response carries an ``x-amz-checksum-*`` header, then calls ``.strip()`` on it, so
the real client raises ``AttributeError`` instead of a ``ClientError``. Serving
that response from a ``before-send`` hook runs botocore's own code, which a fake
client could not.
"""

from __future__ import annotations

import base64
import zlib

from botocore.awsrequest import AWSResponse


class _RawBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, *a, **k):
        yield self._body


def serve_5xx(client, times: int, status: int = 503) -> list[int]:
    """Answer the first ``times`` GetObject calls with a 5xx that carries an
    ``x-amz-checksum-*`` header, as real S3 sent; later GETs fall through to
    moto. Returns a list that grows by one per GET sent."""
    sent: list[int] = []
    body = b"<Error><Code>SlowDown</Code></Error>"
    crc = base64.b64encode(zlib.crc32(body).to_bytes(4, "big")).decode()

    def hook(request, **_kw):
        sent.append(1)
        if len(sent) <= times:
            return AWSResponse(request.url, status, {"x-amz-checksum-crc32": crc}, _RawBody(body))
        return None

    client.meta.events.register_first("before-send.s3.GetObject", hook)
    return sent
