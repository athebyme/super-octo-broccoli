# -*- coding: utf-8 -*-
"""Tenant-scope checks for the seller-facing agent task routes."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.agents import register_agents_routes


def _user(*, seller_id=None, is_admin=False):
    seller = SimpleNamespace(id=seller_id) if seller_id is not None else None
    return SimpleNamespace(
        id=1,
        seller=seller,
        is_admin=is_admin,
        is_authenticated=True,
        is_active=True,
    )


class AgentTaskRouteSecurityTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='agent-route-test')
        register_agents_routes(self.app)
        self.client = self.app.test_client()

    def _user_patches(self, user):
        return (
            patch('routes.agents.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    def test_non_admin_without_seller_is_denied_before_task_lookup(self):
        user = _user()
        endpoints = (
            ('get', '/agents/api/tasks'),
            ('get', '/agents/api/stats'),
            ('get', '/agents/tasks/foreign-task'),
            ('get', '/agents/api/tasks/foreign-task/changes'),
            ('post', '/agents/tasks/foreign-task/cancel'),
            ('post', '/agents/tasks/foreign-task/rollback'),
            ('post', '/agents/api/tasks/foreign-task/cancel'),
            ('post', '/agents/api/tasks/foreign-task/rollback'),
        )

        user_patch, login_patch = self._user_patches(user)
        with user_patch, login_patch, \
                patch('routes.agents.agent_service.get_task') as get_task, \
                patch('routes.agents.agent_service.list_tasks') as list_tasks, \
                patch('routes.agents.agent_service.cancel_task') as cancel_task, \
                patch('routes.agents.agent_harness.rollback_task_tree') as rollback:
            for method, path in endpoints:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, 403)

            get_task.assert_not_called()
            list_tasks.assert_not_called()
            cancel_task.assert_not_called()
            rollback.assert_not_called()

    def test_non_admin_is_scoped_and_cannot_mutate_foreign_task(self):
        user = _user(seller_id=7)
        foreign_task = SimpleNamespace(id='foreign-task', seller_id=8)
        protected_endpoints = (
            ('get', '/agents/tasks/foreign-task'),
            ('get', '/agents/api/tasks/foreign-task/changes'),
            ('post', '/agents/tasks/foreign-task/cancel'),
            ('post', '/agents/tasks/foreign-task/rollback'),
        )

        user_patch, login_patch = self._user_patches(user)
        with user_patch, login_patch, \
                patch(
                    'routes.agents.agent_service.get_task',
                    return_value=foreign_task,
                ), \
                patch(
                    'routes.agents.agent_service.list_tasks',
                    return_value=([], 0),
                ) as list_tasks, \
                patch(
                    'routes.agents.agent_service.get_agent_stats',
                    return_value={},
                ) as get_stats, \
                patch('routes.agents.agent_service.cancel_task') as cancel_task, \
                patch('routes.agents.agent_harness.rollback_task_tree') as rollback:
            task_list = self.client.get('/agents/api/tasks')
            stats = self.client.get('/agents/api/stats')
            self.assertEqual(task_list.status_code, 200)
            self.assertEqual(stats.status_code, 200)
            self.assertEqual(list_tasks.call_args.kwargs['seller_id'], 7)
            self.assertEqual(get_stats.call_args.kwargs['seller_id'], 7)

            for method, path in protected_endpoints:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, 403)

            cancel_task.assert_not_called()
            rollback.assert_not_called()

    def test_admin_global_task_list_is_explicit(self):
        user = _user(is_admin=True)
        user_patch, login_patch = self._user_patches(user)
        with user_patch, login_patch, patch(
            'routes.agents.agent_service.list_tasks', return_value=([], 0),
        ) as list_tasks:
            response = self.client.get('/agents/api/tasks')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(list_tasks.call_args.kwargs['seller_id'])


if __name__ == '__main__':
    unittest.main()
