# -*- coding: utf-8 -*-
"""
Тесты для AgentConfig — ленивая конфигурация из env vars.
"""
import os
import pytest

from agents.config import AgentConfig, _resolve


class TestConfigResolve:
    def test_default_value(self):
        # PLATFORM_URL имеет дефолт http://localhost:5000
        # Убеждаемся, что при отсутствии env var используется дефолт
        original = os.environ.pop('PLATFORM_URL', None)
        try:
            result = _resolve('PLATFORM_URL')
            assert result == 'http://localhost:5000'
        finally:
            if original is not None:
                os.environ['PLATFORM_URL'] = original

    def test_env_override(self):
        original = os.environ.get('PLATFORM_URL')
        os.environ['PLATFORM_URL'] = 'http://test:8080'
        try:
            result = _resolve('PLATFORM_URL')
            assert result == 'http://test:8080'
        finally:
            if original is not None:
                os.environ['PLATFORM_URL'] = original
            else:
                os.environ.pop('PLATFORM_URL', None)

    def test_int_type_coercion(self):
        original = os.environ.get('AGENT_POLL_INTERVAL')
        os.environ['AGENT_POLL_INTERVAL'] = '15'
        try:
            result = _resolve('POLL_INTERVAL')
            assert result == 15
            assert isinstance(result, int)
        finally:
            if original is not None:
                os.environ['AGENT_POLL_INTERVAL'] = original
            else:
                os.environ.pop('AGENT_POLL_INTERVAL', None)

    def test_float_type_coercion(self):
        original = os.environ.get('LLM_TEMPERATURE')
        os.environ['LLM_TEMPERATURE'] = '0.7'
        try:
            result = _resolve('TEMPERATURE')
            assert result == 0.7
            assert isinstance(result, float)
        finally:
            if original is not None:
                os.environ['LLM_TEMPERATURE'] = original
            else:
                os.environ.pop('LLM_TEMPERATURE', None)

    def test_unknown_field_raises(self):
        with pytest.raises(AttributeError):
            _resolve('NONEXISTENT_FIELD')


class TestConfigValidation:
    def test_validate_missing_agent_id(self):
        originals = {}
        for key in ['AGENT_ID', 'AGENT_API_KEY']:
            originals[key] = os.environ.pop(key, None)

        try:
            with pytest.raises(ValueError) as exc_info:
                AgentConfig.validate()
            assert 'AGENT_ID' in str(exc_info.value)
        finally:
            for key, val in originals.items():
                if val is not None:
                    os.environ[key] = val

    def test_validate_passes_with_all_set(self):
        originals = {}
        env_vars = {
            'AGENT_ID': 'test-id',
            'AGENT_API_KEY': 'test-key',
            'LLM_PROVIDER': 'cloudru',
            'CLOUDRU_API_KEY': 'test-cloudru-key',
        }
        for key in env_vars:
            originals[key] = os.environ.get(key)
            os.environ[key] = env_vars[key]

        try:
            AgentConfig.validate()  # Should not raise
        finally:
            for key, val in originals.items():
                if val is not None:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)
