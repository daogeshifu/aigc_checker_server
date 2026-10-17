"""Verify actual SDK HTTP requests against a mock transport, never a paid service."""
import json
import os
import unittest
from unittest.mock import patch

import httpx

from services import azure_client, checker


AZURE_SETTINGS = {
    'AZURE_API_KEY': 'fixture-azure-key',
    'AZURE_ENDPOINT': 'https://azure-fixture.cognitiveservices.azure.com/',
    'AZURE_API_MODE': 'v1',
    'AZURE_API_VERSION': '2025-12-11',  # An old model-date setting must not enter v1 requests.
}


class AzureConfigurationTests(unittest.TestCase):
    def test_v1_accepts_resource_root_and_php_style_base_without_duplicate_path(self):
        expected = 'https://azure-fixture.cognitiveservices.azure.com/openai/v1/'
        for endpoint in [AZURE_SETTINGS['AZURE_ENDPOINT'], expected, expected.rstrip('/'),
                         'https://azure-fixture.cognitiveservices.azure.com/openai']:
            self.assertEqual(azure_client.azure_base_url(endpoint), expected)

    def test_rejects_query_version_and_complete_request_url_as_v1_base(self):
        for endpoint in ['http://example.invalid', 'https://user:pass@example.invalid',
                         'https://example.invalid/openai/v1/chat/completions',
                         'https://example.invalid/?api-version=2025-12-11']:
            with self.assertRaises(ValueError):
                azure_client.azure_base_url(endpoint)

    def test_legacy_options_preserved_but_temperature_omitted_with_reasoning(self):
        with patch.dict(os.environ, {'AZURE_API_MODE': 'legacy'}, clear=True):
            self.assertEqual(azure_client.request_options('remote_review')['temperature'], 0.2)
            with patch.dict(os.environ, {'AZURE_REVIEW_REASONING_EFFORT': 'low'}):
                self.assertNotIn('temperature', azure_client.request_options('remote_review'))
        with patch.dict(os.environ, {'AZURE_REVIEW_REASONING_EFFORT': 'invalid'}, clear=True):
            with self.assertRaises(ValueError):
                azure_client.request_options('remote_review')

    def test_missing_configuration_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(azure_client.create_azure_client())


class AzureRequestTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, settings, body=None, purpose='remote_review', status=200):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(status, json=body or {
                'id': 'fixture', 'object': 'chat.completion', 'created': 1, 'model': 'underlying-model',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"ai_score":42}'},
                             'finish_reason': 'stop'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            from openai import AsyncOpenAI, AsyncAzureOpenAI
            constructors = (AsyncOpenAI, AsyncAzureOpenAI)
            def v1(**kwargs):
                return constructors[0](**kwargs, http_client=http, max_retries=0)
            def legacy(**kwargs):
                return constructors[1](**kwargs, http_client=http, max_retries=0)
            with patch.dict(os.environ, settings, clear=True), \
                 patch.object(checker, '_azure_client', None), \
                 patch('openai.AsyncOpenAI', side_effect=v1), \
                 patch('openai.AsyncAzureOpenAI', side_effect=legacy):
                try:
                    result = await checker._call_azure_json('Output JSON', 'A text sample',
                                                            'custom-review-deployment', purpose)
                finally:
                    await checker.close_azure_client()
        return result, requests

    async def test_v1_matches_php_route_bearer_auth_and_has_no_version_or_temperature(self):
        result, requests = await self.exercise(AZURE_SETTINGS)
        request = requests[0]
        self.assertEqual(str(request.url), AZURE_SETTINGS['AZURE_ENDPOINT'] + 'openai/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer fixture-azure-key')
        self.assertEqual(request.extensions['timeout']['read'], 600)
        self.assertEqual(request.extensions['timeout']['connect'], 10)
        self.assertNotIn('api-key', request.headers)
        body = json.loads(request.content)
        self.assertEqual(body['model'], 'custom-review-deployment')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertFalse(body['stream'])
        self.assertNotIn('temperature', body)
        self.assertEqual(result['ai_score'], 42)

    async def test_php_environment_names_and_gpt6_reasoning_do_not_change_deployment(self):
        result, requests = await self.exercise({
            'AZURE_OPENAI_ENDPOINT': AZURE_SETTINGS['AZURE_ENDPOINT'] + 'openai/v1/',
            'AZURE_OPENAI_API_KEY': 'fixture-php-key',
            'AZURE_OPENAI_TIMEOUT': '600', 'AZURE_OPENAI_CONNECT_TIMEOUT': '10',
            'AZURE_REVIEW_REASONING_EFFORT': 'low',
        })
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].headers['authorization'], 'Bearer fixture-php-key')
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertNotIn('temperature', body)
        self.assertEqual(body['model'], 'custom-review-deployment')

    async def test_legacy_mode_keeps_dated_route(self):
        result, requests = await self.exercise({**AZURE_SETTINGS, 'AZURE_API_MODE': 'legacy',
                                                'AZURE_API_VERSION': '2025-04-01-preview'})
        request = requests[0]
        self.assertIn('/openai/deployments/custom-review-deployment/chat/completions', str(request.url))
        self.assertEqual(request.url.params['api-version'], '2025-04-01-preview')
        self.assertEqual(request.headers['api-key'], 'fixture-azure-key')

    async def test_azure_refusal_or_truncation_is_not_a_successful_review(self):
        for content, finish in [(None, 'stop'), ('', 'stop'), ('{"ai_score":42}', 'length')]:
            body = {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                                 'finish_reason': finish}]}
            with self.subTest(content=content, finish=finish), self.assertRaises(ValueError):
                await self.exercise(AZURE_SETTINGS, body)
