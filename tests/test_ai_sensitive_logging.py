"""Privacy-sensitive AI calls never log prompt, response or provider bodies."""

import unittest
from unittest.mock import Mock, patch

import requests

from services.ai_service import AIClient, AIConfig, AIProvider


class SensitiveAILoggingTest(unittest.TestCase):
    def _client(self):
        config = AIConfig(
            provider=AIProvider.CUSTOM,
            api_key="synthetic-ai-key",
            api_base_url="https://ai.invalid/v1",
            model="synthetic-model",
            max_tokens=100,
            log_payloads=False,
            max_retries=1,
        )
        return AIClient(config)

    @staticmethod
    def _logged_text(logger_mock):
        parts = []
        for method in (
            logger_mock.debug,
            logger_mock.info,
            logger_mock.warning,
            logger_mock.error,
        ):
            for call in method.call_args_list:
                parts.extend(str(value) for value in call.args)
        return " ".join(parts)

    def test_success_suppresses_prompt_and_response_content(self):
        client = self._client()
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{
                "message": {"content": "PRIVATE_AI_RESPONSE"},
                "finish_reason": "stop",
            }],
        }
        response.raise_for_status.return_value = None
        client._session.post = Mock(return_value=response)

        with patch("services.ai_service.logger") as logger_mock:
            result = client.chat_completion([
                {"role": "user", "content": "PRIVATE_CUSTOMER_PROMPT"},
            ])

        self.assertEqual(result, "PRIVATE_AI_RESPONSE")
        logged = self._logged_text(logger_mock)
        self.assertNotIn("PRIVATE_CUSTOMER_PROMPT", logged)
        self.assertNotIn("PRIVATE_AI_RESPONSE", logged)

    def test_http_error_suppresses_provider_body_that_echoes_customer_text(self):
        client = self._client()
        response = Mock(
            status_code=400,
            text="PROVIDER_ECHO_PRIVATE_CUSTOMER_PROMPT",
        )
        error = requests.HTTPError("provider rejected request")
        error.response = response
        response.raise_for_status.side_effect = error
        client._session.post = Mock(return_value=response)

        with patch("services.ai_service.logger") as logger_mock:
            result = client.chat_completion([
                {"role": "user", "content": "PRIVATE_CUSTOMER_PROMPT"},
            ])

        self.assertIsNone(result)
        logged = self._logged_text(logger_mock)
        self.assertNotIn("PRIVATE_CUSTOMER_PROMPT", logged)
        self.assertNotIn("PROVIDER_ECHO", logged)
        client._session.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
