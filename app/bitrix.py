"""Bitrix24 REST client.

Two auth modes:
  - WEBHOOK:  outbound webhook token, GET/POST  https://<BASE>/rest/<USER>/<TOKEN>/<method>.<ext>
              (e.g. crm.deal.add.json)
  - COOKIE:   cookie-mode using a JSON file dumped from the on-prem browser session.
              POST https://<BASE>/rest/<method>.json with form-encoded body, including
              sessid from the session file. Works on the on-prem 1С-Bitrix portal
              (no API token, no public endpoint needed).

The on-prem Bitrix at bitrix.a2kad.ru is verified to work via cookie-mode in
D:\\11. 2KAD_Soft\\8. 2KAD_bitrix\\skills\\bitrix-reader. We mirror the same auth
shape here so this service can run anywhere (Dokploy / local / CI).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


class BitrixError(RuntimeError):
    """Raised when Bitrix REST returns an error response."""


class BitrixClient:
    """Thin REST client. Auth is decided once at construction time."""

    def __init__(
        self,
        base_url: str,
        webhook_token: str | None = None,
        session_json: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        if webhook_token:
            # webhook_token may be either:
            #   a) raw token ("abc123...")
            #   b) full path fragment ("1/abc123...")
            # Normalize to "USER/TOKEN".
            fragment = webhook_token.strip().strip("/")
            self._mode = "webhook"
            self._webhook_url = f"{self.base_url}/rest/{fragment}"
            logger.info("BitrixClient: webhook mode, %s/***", self._webhook_url.rsplit("/", 1)[0])
        elif session_json:
            try:
                payload = json.loads(session_json)
            except json.JSONDecodeError as exc:
                raise BitrixError(f"BITRIX_SESSION_JSON is not valid JSON: {exc}") from exc
            cookies = payload.get("cookies") or payload
            sessid = payload.get("sessid") or cookies.get("BITRIX_SM_SESSID")
            if not sessid:
                raise BitrixError(
                    "BITRIX_SESSION_JSON missing 'sessid' (or cookies.BITRIX_SM_SESSID)"
                )
            self._mode = "cookie"
            self._sessid = sessid
            cookie_jar: dict[str, str] = {}
            for k, v in cookies.items():
                if k == "BITRIX_SM_SESSID":
                    continue
                cookie_jar[k] = str(v)
            self._cookies = cookie_jar
            logger.info("BitrixClient: cookie mode, sessid=***")
        else:
            raise BitrixError(
                "BitrixClient: provide either BITRIX_WEBHOOK_TOKEN or BITRIX_SESSION_JSON"
            )

        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BitrixClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- core ---------------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a Bitrix REST method and return the result payload."""
        params = params or {}
        if self._mode == "webhook":
            url = f"{self._webhook_url}/{method}.json"
            response = self._client.post(url, json=params)
        else:
            url = f"{self.base_url}/rest/{method}.json"
            body = dict(params)
            body["sessid"] = self._sessid
            response = self._client.post(
                url,
                content=urlencode(body),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                cookies=self._cookies,
            )

        if response.status_code >= 400:
            raise BitrixError(f"Bitrix HTTP {response.status_code}: {response.text[:500]}")

        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise BitrixError(
                f"Bitrix {method}: {data.get('error')} — {data.get('error_description', '')}"
            )
        return data.get("result", data)

    # --- convenience wrappers ----------------------------------------------

    def crm_deal_add(self, fields: dict[str, Any]) -> int:
        result = self.call("crm.deal.add", {"fields": fields})
        # Bitrix returns int deal id.
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise BitrixError(f"Unexpected crm.deal.add response: {result!r}") from exc

    def crm_deal_get(self, deal_id: int) -> dict[str, Any]:
        return self.call(
            "crm.deal.get",
            {"id": deal_id},
        )

    def crm_timeline_comment_add(
        self,
        entity_type: str,
        entity_id: int,
        comment: str,
    ) -> int:
        """Add a comment to the entity's timeline.

        Used because on-prem Bitrix `im.*` API for deal chat is unavailable
        (verified 2026-06-18, see 2kad-bitrix-start-project skill notes).
        """
        return int(
            self.call(
                "crm.timeline.comment.add",
                {
                    "fields": {
                        "ENTITY_ID": entity_id,
                        "ENTITY_TYPE": entity_type,  # e.g. "deal"
                        "COMMENT": comment,
                    }
                },
            )
        )


def from_env() -> BitrixClient:
    """Build a BitrixClient from environment variables."""
    base_url = os.environ.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru")
    token = os.environ.get("BITRIX_WEBHOOK_TOKEN", "").strip() or None
    session = os.environ.get("BITRIX_SESSION_JSON", "").strip() or None
    if not token and not session:
        raise BitrixError(
            "Neither BITRIX_WEBHOOK_TOKEN nor BITRIX_SESSION_JSON is set"
        )
    return BitrixClient(base_url=base_url, webhook_token=token, session_json=session)
