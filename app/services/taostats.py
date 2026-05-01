from __future__ import annotations

import logging
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class TaoStatsClient:
    # TaoStats 只在链上事件拿不到真实 TAO 时作为补全来源，避免浪费免费额度。
    BASE_URL = "https://api.taostats.io/api/delegation/v1"
    _last_request_at = 0.0
    _key_cooldowns: dict[str, float] = {}
    _next_key_index = 0

    def __init__(
        self,
        api_key: str,
        api_keys: str = "",
        timeout_seconds: float = 8.0,
        request_interval_seconds: float = 2.0,
        rate_limit_cooldown_seconds: int = 60,
    ) -> None:
        self.api_keys = self._parse_api_keys(api_key, api_keys)
        self.timeout_seconds = timeout_seconds
        self.request_interval_seconds = max(0.0, float(request_interval_seconds))
        self.rate_limit_cooldown_seconds = max(5, int(rate_limit_cooldown_seconds))
        self._block_cache: dict[int, list[dict[str, Any]]] = {}

    def fetch_stake_events(
        self,
        *,
        block_number: int,
        extrinsic_index: int | None,
        netuid: int | None,
    ) -> list[dict[str, Any]]:
        if not self.api_keys:
            return []
        if int(block_number) in self._block_cache:
            rows = self._block_cache[int(block_number)]
            return self._filter_rows(rows, block_number, extrinsic_index)

        params: dict[str, Any] = {
            "action": "undelegate",
            "block_number": int(block_number),
            "limit": 50,
            "page": 1,
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = self._request_with_key_pool(client, params, block_number)
                if response is None:
                    return []
                response.raise_for_status()
        except Exception as exc:
            logger.info("TaoStats 查询失败 block=%s extrinsic=%s error=%s", block_number, extrinsic_index, exc)
            return []

        rows = self._extract_rows(response.json())
        self._block_cache[int(block_number)] = rows
        return self._filter_rows(rows, block_number, extrinsic_index)

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        wait_seconds = self.request_interval_seconds - (now - type(self)._last_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        type(self)._last_request_at = time.monotonic()

    def _request_with_key_pool(
        self,
        client: httpx.Client,
        params: dict[str, Any],
        block_number: int,
    ) -> httpx.Response | None:
        tried = 0
        while tried < len(self.api_keys):
            key = self._next_available_key()
            if key is None:
                logger.info("TaoStats 所有 API Key 都在冷却中，跳过本轮 block=%s", block_number)
                return None
            tried += 1
            self._wait_for_rate_limit()
            response = client.get(self.BASE_URL, params=params, headers=self._auth_headers(key))
            if response.status_code in {401, 403}:
                self._wait_for_rate_limit()
                response = client.get(self.BASE_URL, params=params, headers=self._auth_headers(key, use_bearer=True))
            if response.status_code == 429:
                type(self)._key_cooldowns[key] = time.monotonic() + self.rate_limit_cooldown_seconds
                logger.info(
                    "TaoStats API Key 触发 429，已切换下一个 key，冷却 %s 秒 block=%s key=%s",
                    self.rate_limit_cooldown_seconds,
                    block_number,
                    self._mask_key(key),
                )
                continue
            return response
        logger.info("TaoStats API Key 池本轮全部触发 429，跳过 block=%s", block_number)
        return None

    def _next_available_key(self) -> str | None:
        now = time.monotonic()
        for _ in range(len(self.api_keys)):
            index = type(self)._next_key_index % len(self.api_keys)
            type(self)._next_key_index += 1
            key = self.api_keys[index]
            if type(self)._key_cooldowns.get(key, 0.0) <= now:
                return key
        return None

    def _parse_api_keys(self, api_key: str, api_keys: str) -> list[str]:
        raw_keys: list[str] = []
        raw_keys.extend(part.strip() for part in str(api_keys or "").replace("\n", ",").split(","))
        if api_key:
            raw_keys.append(api_key.strip())
        return [key for key in dict.fromkeys(raw_keys) if key]

    def _mask_key(self, key: str) -> str:
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}...{key[-4:]}"

    def _filter_rows(
        self,
        rows: list[dict[str, Any]],
        block_number: int,
        extrinsic_index: int | None,
    ) -> list[dict[str, Any]]:
        if extrinsic_index is None:
            return rows
        matched = [row for row in rows if self._row_matches_extrinsic(row, block_number, extrinsic_index)]
        if matched:
            return matched
        if len(rows) == 1:
            return rows
        return []

    def _auth_headers(self, key: str, *, use_bearer: bool = False) -> dict[str, str]:
        token = f"Bearer {key}" if use_bearer else key
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
