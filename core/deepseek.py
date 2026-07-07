"""
DeepSeek — AI 诊断失败原因
"""
import logging
import time
from datetime import datetime
from typing import Optional
import requests

logger = logging.getLogger("deepseek")


class DeepSeekClient:

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        daily_budget_yuan: float = 1.0,
        max_retry_per_error: int = 2,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._daily_budget_yuan = daily_budget_yuan
        self._max_retry_per_error = max_retry_per_error
        self._today_date = datetime.now().date()
        self._today_cost = 0.0
        self.enabled = bool(api_key)

    def diagnose(
        self,
        tool_name: str,
        task_name: str,
        error_message: str,
        exit_code: Optional[int] = None,
    ) -> str:
        if not self.enabled:
            return ""
        self._reset_budget_if_new_day()
        if self._today_cost >= self._daily_budget_yuan:
            logger.info("Daily AI budget exhausted")
            return ""

        truncated = error_message[:800] if error_message else "no output"

        prompt = (
            f"Tool [{tool_name}] Task [{task_name}] failed. "
            f"Exit code: {exit_code}. Error:\n{truncated}\n\n"
            f"Give 1-2 sentence diagnosis in Chinese and a fix suggestion. No extra text."
        )

        for attempt in range(self._max_retry_per_error):
            try:
                response = requests.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": "Brief automation debug assistant."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 200,
                        "temperature": 0.3,
                    },
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage", {})
                self._today_cost += usage.get("total_tokens", 0) / 1000000 * 1.0
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.warning(f"DeepSeek call failed (attempt {attempt+1}): {e}")
                if attempt < self._max_retry_per_error - 1:
                    time.sleep(2)
        return ""

    def _reset_budget_if_new_day(self):
        today = datetime.now().date()
        if today != self._today_date:
            self._today_date = today
            self._today_cost = 0.0
