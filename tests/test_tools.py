# -*- coding: utf-8 -*-
"""
Тесты для ToolRegistry — реестр инструментов агентов.
"""
import json
import pytest

from agents.tools import ToolRegistry, SYSTEM_CONTEXT_TOOL_ALLOWLIST, create_platform_tools


def _make_registry_with_tool():
    """Создаёт реестр с одним тестовым инструментом."""
    registry = ToolRegistry()
    registry.register(
        name='add_numbers',
        description='Складывает два числа',
        parameters={
            'properties': {
                'a': {'type': 'integer', 'description': 'Первое число'},
                'b': {'type': 'integer', 'description': 'Второе число'},
            },
            'required': ['a', 'b'],
        },
        handler=lambda a, b: {'result': a + b},
    )
    return registry


class TestToolRegistry:
    def test_register_and_get_schemas(self):
        registry = _make_registry_with_tool()
        schemas = registry.get_tool_schemas()
        assert len(schemas) == 1
        assert schemas[0]['name'] == 'add_numbers'
        assert 'a' in schemas[0]['input_schema']['properties']

    def test_execute_success(self):
        registry = _make_registry_with_tool()
        result = registry.execute('add_numbers', {'a': 2, 'b': 3})
        parsed = json.loads(result)
        assert parsed['result'] == 5
        assert result == '{"result":5}'

    def test_execute_unknown_tool(self):
        registry = _make_registry_with_tool()
        result = registry.execute('nonexistent', {})
        parsed = json.loads(result)
        assert 'error' in parsed
        assert 'Unknown tool' in parsed['error']

    def test_execute_missing_required_args(self):
        registry = _make_registry_with_tool()
        result = registry.execute('add_numbers', {'a': 1})
        parsed = json.loads(result)
        assert 'error' in parsed
        assert 'Missing required' in parsed['error']

    def test_execute_filters_unknown_args(self):
        """LLM может передать лишние аргументы — они должны быть отфильтрованы."""
        registry = ToolRegistry()
        received_args = {}

        def capture_handler(**kwargs):
            received_args.update(kwargs)
            return {'ok': True}

        registry.register(
            name='test_tool',
            description='Test',
            parameters={
                'properties': {
                    'name': {'type': 'string'},
                },
                'required': ['name'],
            },
            handler=capture_handler,
        )
        registry.execute('test_tool', {'name': 'hello', 'hallucinated_arg': 42})
        assert 'name' in received_args
        assert 'hallucinated_arg' not in received_args

    def test_execute_type_coercion_int(self):
        """LLM может передать string вместо int."""
        registry = _make_registry_with_tool()
        result = registry.execute('add_numbers', {'a': '5', 'b': '3'})
        parsed = json.loads(result)
        assert parsed['result'] == 8

    def test_merge(self):
        """merge() объединяет два реестра."""
        r1 = ToolRegistry()
        r1.register('tool_a', 'A', {'properties': {}, 'required': []},
                     handler=lambda: {'a': True})
        r2 = ToolRegistry()
        r2.register('tool_b', 'B', {'properties': {}, 'required': []},
                     handler=lambda: {'b': True})

        r1.merge(r2)
        schemas = r1.get_tool_schemas()
        names = {s['name'] for s in schemas}
        assert names == {'tool_a', 'tool_b'}

    def test_copy_is_independent(self):
        source = _make_registry_with_tool()
        clone = source.copy()

        clone._tools['add_numbers']['input_schema']['required'].append('extra')
        clone.remove('add_numbers')

        assert len(source.get_tool_schemas()) == 1
        assert source.get_tool_schemas()[0]['input_schema']['required'] == ['a', 'b']

    def test_restricted_returns_executable_subset(self):
        source = ToolRegistry()
        source.register('tool_a', 'A', {'properties': {}, 'required': []},
                        handler=lambda: {'tool': 'a'})
        source.register('tool_b', 'B', {'properties': {}, 'required': []},
                        handler=lambda: {'tool': 'b'})

        restricted = source.restricted(['tool_b', 'missing'])

        assert [s['name'] for s in restricted.get_tool_schemas()] == ['tool_b']
        assert json.loads(restricted.execute('tool_b', {})) == {'tool': 'b'}
        restricted.remove('tool_b')
        assert [s['name'] for s in source.get_tool_schemas()] == ['tool_a', 'tool_b']

    def test_execute_handler_exception(self):
        """Ошибка в handler не крашит execute — возвращает JSON с ошибкой."""
        registry = ToolRegistry()
        registry.register(
            name='failing_tool',
            description='Always fails',
            parameters={'properties': {}, 'required': []},
            handler=lambda: 1 / 0,
        )
        result = registry.execute('failing_tool', {})
        parsed = json.loads(result)
        assert 'error' in parsed

    def test_system_context_tools_are_registered_and_allowlisted(self):
        class Platform:
            def get_product_defaults(self, seller_id, subject_id=None):
                return {'seller_id': seller_id, 'subject_id': subject_id}

            def get_api_connection_status(self, seller_id):
                return {'has_key': False, 'seller_id': seller_id}

            def get_api_logs(self, seller_id, limit=20):
                return {'seller_id': seller_id, 'limit': limit}

        registry = create_platform_tools(Platform())
        names = {tool['name'] for tool in registry.get_tool_schemas()}

        assert {'get_product_defaults', 'get_api_connection_status', 'get_api_logs'} <= names
        assert set(SYSTEM_CONTEXT_TOOL_ALLOWLIST) <= names
        assert json.loads(registry.execute('get_api_logs', {
            'seller_id': 7, 'limit': 500,
        })) == {'seller_id': 7, 'limit': 50}
