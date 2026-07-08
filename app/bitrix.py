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


def _stringify(value: Any) -> str:
    """Bitrix form-encoded params: strings, ints, bools, JSON for nested."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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
            cookies_dict: dict[str, str] = {}
            # Accepted shapes:
            #   A) {"cookies": {"k":"v",...}, "sessid": "..."}
            #   B) {"cookie": "BITRIX_SM_LOGIN=...; BITRIX_SM_UIDH=...; ...", "sessid": "..."}
            #   C) flat dict {"k":"v",...}
            if isinstance(payload.get("cookies"), dict):
                cookies_dict = {str(k): str(v) for k, v in payload["cookies"].items()}
            elif isinstance(payload.get("cookie"), str):
                for chunk in payload["cookie"].split(";"):
                    chunk = chunk.strip()
                    if not chunk or "=" not in chunk:
                        continue
                    k, _, v = chunk.partition("=")
                    cookies_dict[k.strip()] = v.strip()
            else:
                # Treat flat dict as cookies.
                for k, v in payload.items():
                    if k in ("cookie", "sessid", "created", "source"):
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        cookies_dict[str(k)] = str(v)
            sessid = payload.get("sessid") or cookies_dict.get("BITRIX_SM_SESSID")
            if not sessid:
                raise BitrixError(
                    "BITRIX_SESSION_JSON missing 'sessid' (or BITRIX_SM_SESSID cookie)"
                )
            self._mode = "cookie"
            self._sessid = sessid
            self._cookies = cookies_dict
            logger.info("BitrixClient: cookie mode, sessid=***, cookies=%d", len(cookies_dict))
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
        """Invoke a Bitrix REST method and return the result payload.

        Bitrix on-prem REST accepts params as either JSON or form-encoded.
        Cookie-mode requires form-encoded; webhook-mode accepts JSON but
        some endpoints are picky about how nested `fields` are encoded.

        Strategy:
          - Webhook: POST with JSON body (httpx serialises the dict).
          - Cookie: form-encode, but expand nested dicts (`fields[KEY]=v`)
            because urlencode(dict) would serialise the inner dict as a
            string and Bitrix would complain "Parameter 'fields' must be
            array" (verified 2026-06-27 against bitrix.a2kad.ru).
        """
        params = params or {}
        if self._mode == "webhook":
            url = f"{self._webhook_url}/{method}.json"
            response = self._client.post(url, json=params)
        else:
            url = f"{self.base_url}/rest/{method}.json"
            encoded: list[tuple[str, str]] = []
            for key, value in params.items():
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        encoded.append((f"{key}[{sub_key}]", _stringify(sub_value)))
                elif isinstance(value, list):
                    # list of dicts: items[0][key]=v
                    for idx, item in enumerate(value):
                        if isinstance(item, dict):
                            for sub_key, sub_value in item.items():
                                encoded.append(
                                    (f"{key}[{idx}][{sub_key}]", _stringify(sub_value))
                                )
                        else:
                            encoded.append((f"{key}[]", _stringify(item)))
                else:
                    encoded.append((key, _stringify(value)))
            encoded.append(("sessid", self._sessid))
            body = urlencode(encoded)
            response = self._client.post(
                url,
                content=body,
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

    # --- contacts & companies ---------------------------------------------

    def crm_contact_list(
        self, filter: dict[str, Any], select: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Search contacts by filter (e.g. {"PHONE": "..."} or {"EMAIL": "..."})."""
        params: dict[str, Any] = {"filter": filter}
        if select:
            params["select"] = select
        result = self.call("crm.contact.list", params)
        if isinstance(result, list):
            return result
        return []

    def crm_contact_add(self, fields: dict[str, Any]) -> int:
        result = self.call("crm.contact.add", {"fields": fields})
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise BitrixError(f"Unexpected crm.contact.add response: {result!r}") from exc

    def crm_company_list(
        self, filter: dict[str, Any], select: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Search companies by filter (e.g. {"UF_CRM_INN": "..."} or {"TITLE": "..."})."""
        params: dict[str, Any] = {"filter": filter}
        if select:
            params["select"] = select
        result = self.call("crm.company.list", params)
        if isinstance(result, list):
            return result
        return []

    def crm_company_add(self, fields: dict[str, Any]) -> int:
        result = self.call("crm.company.add", {"fields": fields})
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise BitrixError(f"Unexpected crm.company.add response: {result!r}") from exc

    def crm_company_update(self, company_id: int, fields: dict[str, Any]) -> bool:
        """Update company fields. Returns True on success."""
        result = self.call(
            "crm.company.update",
            {"id": int(company_id), "fields": fields},
        )
        return bool(result)

    # --- deal associations ------------------------------------------------

    def crm_deal_contact_add(self, deal_id: int, contact_id: int) -> bool:
        """Bind contact to deal (primary contact)."""
        result = self.call(
            "crm.deal.contact.add",
            {"id": int(deal_id), "fields": {"CONTACT_ID": int(contact_id)}},
        )
        return bool(result)

    def crm_deal_company_add(self, deal_id: int, company_id: int) -> bool:
        """Bind company to deal."""
        result = self.call(
            "crm.deal.company.add",
            {"id": int(deal_id), "fields": {"COMPANY_ID": int(company_id)}},
        )
        return bool(result)

    def crm_deal_update(self, deal_id: int, fields: dict[str, Any]) -> bool:
        """Update deal fields (e.g. OPPORTUNITY, OPPORTUNITY_CURRENCY_ID)."""
        result = self.call(
            "crm.deal.update",
            {"id": int(deal_id), "fields": fields},
        )
        return bool(result)

    # --- chat / messages --------------------------------------------------

    def im_message_add(self, dialog_id: str, message: str) -> int:
        """Post a message into a chat. On-prem Bitrix has limited im.* availability;
        falls back gracefully when the endpoint is unreachable."""
        try:
            return int(
                self.call(
                    "im.message.add",
                    {"DIALOG_ID": dialog_id, "MESSAGE": message},
                )
            )
        except BitrixError:
            raise

    # --- disk (file upload) -----------------------------------------------

    def disk_folder_get_subfolder_id(self, folder_id: int) -> int | None:
        """Resolve a subfolder by name under a given parent folder, return its id.

        Returns None if no matching subfolder is found. Used to put deal
        files into a per-deal subfolder under a project root.
        """
        result = self.call(
            "disk.folder.getsubfolders",
            {"id": int(folder_id)},
        )
        if isinstance(result, list):
            for f in result:
                if f.get("NAME") == "":
                    return int(f.get("ID"))
        return None

    def disk_folder_add_subfolder(self, parent_id: int, name: str) -> int:
        """Create a subfolder under parent_id, return its id."""
        result = self.call(
            "disk.storage.addfolder",
            {"id": int(parent_id), "data": {"NAME": name}},
        )
        try:
            return int(result.get("ID", result)) if isinstance(result, dict) else int(result)
        except (TypeError, ValueError) as exc:
            raise BitrixError(f"disk.storage.addfolder: unexpected {result!r}") from exc


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
