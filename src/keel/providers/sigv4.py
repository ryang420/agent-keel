"""SigV4-sign httpx requests with AWS credentials.

Adapted from Hermes (Nous Research hermes-agent, MIT, v2026.9.24) agent/bedrock_adapter.py
`BedrockOpenAISigV4Auth`. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable

import botocore.exceptions
import botocore.session
from botocore.auth import SigV4Auth as _BotoSigV4
from botocore.awsrequest import AWSRequest
import httpx

from keel.providers.base import ModelError

_session = None
_session_lock = threading.Lock()


def _ambient_credentials():
    """The default AWS chain: env vars, AWS_PROFILE, SSO, container or instance role."""
    global _session
    with _session_lock:
        if _session is None:
            _session = botocore.session.get_session()
    credentials = _session.get_credentials()
    return credentials.get_frozen_credentials() if credentials is not None else None


class SigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock",
                 credentials: Callable[[], object | None] | None = None) -> None:
        self.region = region
        self.service = service
        self._credentials = credentials or _ambient_credentials

    def _sign(self, request: httpx.Request) -> dict[str, str]:
        """Resolve credentials and sign; blocking (config, SSO cache and IMDS reads). Returns the headers."""
        try:
            credentials = self._credentials()
            if credentials is None:
                raise ModelError("auth", "No AWS credentials are available to sign the model request.")
            # SigV4 must own these headers.
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in {"authorization", "x-amz-date", "x-amz-security-token"}}
            signed = AWSRequest(method=request.method, url=str(request.url), data=request.content or b"",
                                headers=headers)
            _BotoSigV4(credentials, self.service, self.region).add_auth(signed)
            return dict(signed.headers.items())
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            raise ModelError("auth", f"Could not get AWS credentials to sign the model request: {exc}") from exc

    def auth_flow(self, request: httpx.Request):
        request.headers.update(self._sign(request))
        yield request

    async def async_auth_flow(self, request: httpx.Request):
        await request.aread()
        request.headers.update(await asyncio.to_thread(self._sign, request))
        yield request
