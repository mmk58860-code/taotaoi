from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class TaoStatsClient:
    # TaoStats 只在链上事件拿不到真实 TAO 时作为补全来源，避免浪费免费额度。
    BASE_URL = "https://api.taostats.io/api/delegation/v1"

    def __init__(self, api_key: str, timeout_seconds: float = 8.0) -> None:
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds

    def fetch_stake_events(
        self,
        *,
        block_number: int,
        extrinsic_index: int | None,
        netuid: int | None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            return []

        params: dict[str, Any] = {
            "action": "undelegate",
            "block_number": int(block_number),
            "limit": 50,
            "page": 1,
        }
        if netuid is not None:
            params["netuid"] = int(netuid)

        headers = self._auth_headers()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(self.BASE_URL, params=params, headers=headers)
                if response.status_code in {401, 403} and "Authorization" in headers:
                    response = client.get(self.BASE_URL, params=params, headers=self._auth_headers(use_bearer=True))
                response.raise_for_status()
        except Exception as exc:
            logger.info("TaoStats 查询失败 block=%s extrinsic=%s error=%s", block_number, extrinsic_index, exc)
            return []

        rows = self._extract_rows(response.json())
        if extrinsic_index is None:
            return rows
        matched = [row for row in rows if self._row_matches_extrinsic(row, block_number, extrinsic_index)]
        return matched or rows

    def _auth_headers(self, *, use_bearer: bool = False) -> dict[str, str]:
        token = f"Bearer {self.api_key}" if use_bearer else self.api_key
        return {
            "Authorization": token,
            "accept": "application/json",
        }

    def _extract_rows(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "results", "items", "events"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = self._extract_rows(value)
                if nested:
                    return nested
        return []

    def _row_matches_extrinsic(self, row: dict[str, Any], block_number: int, extrinsic_index: int) -> bool:
        extrinsic_id = str(row.get("extrinsic_id") or row.get("extrinsicId") or "")
        compact_index = str(int(extrinsic_index))
        padded_index = f"{int(extrinsic_index):04d}"
        if extrinsic_id:
            return any(
                marker in extrinsic_id
                for marker in (
                    f"{block_number}-{compact_index}",
                    f"{block_number}-{padded_index}",
                    f"{block_number}:{compact_index}",
                    f"{block_number}:{padded_index}",
                )
            )
        for key in ("extrinsic_index", "extrinsic_idx", "extrinsicIndex"):
            value = row.get(key)
            if str(value) in {compact_index, padded_index}:
                return True
        return False
