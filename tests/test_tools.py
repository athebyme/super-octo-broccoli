# -*- coding: utf-8 -*-
"""
Тесты для ToolRegistry — реестр инструментов агентов.
"""
import json
import pytest

from agents.tools import ToolRegistry


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
