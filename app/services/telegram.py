from __future__ import annotations

import httpx


class TelegramNotifier:
    async def send_message(self, token: str, chat_id: str, text: str) -> bool:
        # 如果没有配置 TG 参数，就直接跳过发送。
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return True
