"""Backup table coverage and bounded retention over operational traces; no platform access."""
import os
import time
import unittest
from unittest import mock

import main
import retention
import store
import test_model_review as fixture
from sandbox import KernelError, maintenance_access


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ModelReviewTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.f.h
        self.attempt_id = self.f.attempt['attempt_id']

    def call(self, tool, payload=None):
        with maintenance_access():
            return self.h.call(tool, payload if payload is not None else {})

    def backup(self):
        with maintenance_access():
            return main.dispatch('runtime.database.backup', {'confirmed_global': True},
                                 self.h.context['context_token'])

    def event(self, age_seconds, tool='test.trace'):
        with store.runtime_db() as c:
            c.execute('INSERT INTO runtime_events(scope_id,tool,status,occurred_at) VALUES (?,?,?,?)',
                      (self.h.context['scope_id'], tool, 'ok', time.time() - age_seconds))

    def count(self, sql, args=()):
        with store.runtime_db() as c:
            return c.execute(sql, args).fetchone()[0]

    def drop(self, table):
        with store.runtime_db() as c:
            c.execute('DROP TABLE IF EXISTS ' + table)

    def test_every_runtime_table_is_covered_by_the_backup_allow_list(self):
        audit = self.call('runtime.database.coverage')
        self.assertEqual(audit['uncovered'], [])
        self.assertEqual(audit['missing'], [])
        self.assertIn('runtime_send_attempts', audit['covered'])
        self.assertIn('runtime_model_calls', audit['covered'])
        self.assertNotIn('runtime_events', audit['uncovered'])

    def test_an_unregistered_table_blocks_a_backup_instead_of_a_partial_copy(self):
        with store.runtime_db() as c:
            c.execute('CREATE TABLE runtime_unregistered (id INTEGER PRIMARY KEY)')
        self.addCleanup(self.drop, 'runtime_unregistered')
        self.assertIn('runtime_unregistered', self.call('runtime.database.coverage')['uncovered'])
        with self.assertRaises(KernelError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'BACKUP_TABLE_COVERAGE_GAP')

    def test_plan_counts_expired_traces_without_deleting_anything(self):
        self.event(40 * 86400)
        self.event(366 * 86400)
        self.event(1)
        before = self.count('SELECT count(*) FROM runtime_events')
        plan = self.call('runtime.maintenance.retention.plan', {'days': 30})
        self.assertEqual(plan['deletable']['runtime_events'], 2)
        self.assertEqual(plan['total_deletable'], 2)
        self.assertEqual(plan['policy_source'], 'explicit')
        self.assertTrue(plan['irreversible'])
        self.assertFalse(plan['applied'])
        self.assertEqual(plan['backup_coverage']['uncovered'], [])
        self.assertEqual(self.count('SELECT count(*) FROM runtime_events WHERE occurred_at < ?',
                                    (time.time() - 30 * 86400,)), 2)

    def test_apply_deletes_only_expired_traces_and_is_idempotent(self):
        self.event(40 * 86400)
        self.event(366 * 86400)
        self.event(1)
        result = self.call('runtime.maintenance.retention.apply', {'days': 30, 'confirmed': True})
        self.assertEqual(result['deleted']['runtime_events'], 2)
        self.assertEqual(result['total_deleted'], 2)
        self.assertTrue(result['applied'])
        self.assertEqual(self.count('SELECT count(*) FROM runtime_events WHERE occurred_at < ?',
                                    (time.time() - 30 * 86400,)), 0)
        again = self.call('runtime.maintenance.retention.apply', {'days': 30, 'confirmed': True})
        self.assertEqual(again['total_deleted'], 0)

    def test_unfinished_model_calls_are_never_pruned(self):
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_model_calls SET created_at=? WHERE attempt_id=?',
                      (time.time() - 40 * 86400, self.attempt_id))
        plan = self.call('runtime.maintenance.retention.plan', {'days': 30})
        self.assertEqual(plan['kept_unfinished_model_calls'], 1)
        self.assertEqual(plan['deletable']['runtime_model_calls'], 0)
        result = self.call('runtime.maintenance.retention.apply', {'days': 30, 'confirmed': True})
        self.assertEqual(result['deleted']['runtime_model_calls'], 0)
        self.assertEqual(self.count('SELECT count(*) FROM runtime_model_calls WHERE attempt_id=?',
                                    (self.attempt_id,)), 1)

    def test_terminal_model_calls_expire_while_task_state_is_kept(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_model_calls SET state='not_sent',created_at=? WHERE attempt_id=?",
                      (time.time() - 40 * 86400, self.attempt_id))
        tasks_before = self.count('SELECT count(*) FROM runtime_tasks')
        result = self.call('runtime.maintenance.retention.apply', {'days': 30, 'confirmed': True})
        self.assertEqual(result['deleted']['runtime_model_calls'], 1)
        self.assertEqual(self.count('SELECT count(*) FROM runtime_model_calls WHERE attempt_id=?',
                                    (self.attempt_id,)), 0)
        self.assertGreaterEqual(self.count('SELECT count(*) FROM runtime_tasks'), tasks_before)

    def test_apply_requires_explicit_confirmation(self):
        with self.assertRaises(KernelError) as caught:
            self.call('runtime.maintenance.retention.apply', {'days': 30})
        self.assertEqual(caught.exception.code, 'RETENTION_CONFIRMATION_REQUIRED')
        with self.assertRaises(KernelError) as caught:
            self.call('runtime.maintenance.retention.apply', {'days': 30, 'confirmed': 'yes'})
        self.assertEqual(caught.exception.code, 'RETENTION_CONFIRMATION_REQUIRED')

    def test_days_are_bounded_and_reject_non_integers(self):
        for bad in (0, 366, -1, True, '30', 30.5):
            with self.assertRaises(KernelError) as caught:
                self.call('runtime.maintenance.retention.plan', {'days': bad})
            self.assertEqual(caught.exception.code, 'INVALID_RETENTION_DAYS')
        with self.assertRaises(KernelError) as caught:
            self.call('runtime.maintenance.retention.plan', {'days': 30, 'extra': 1})
        self.assertEqual(caught.exception.code, 'INVALID_RETENTION_DAYS')

    def test_environment_supplies_the_default_policy(self):
        with mock.patch.dict(os.environ, {'JOBKIT_RETENTION_DAYS': '7'}):
            plan = self.call('runtime.maintenance.retention.plan')
        self.assertEqual(plan['days'], 7)
        self.assertEqual(plan['policy_source'], 'environment:JOBKIT_RETENTION_DAYS')
        with mock.patch.dict(os.environ, {'JOBKIT_RETENTION_DAYS': 'not-a-number'}):
            with self.assertRaises(KernelError) as caught:
                retention.days()
        self.assertEqual(caught.exception.code, 'INVALID_RETENTION_DAYS')

    def test_retention_is_refused_outside_local_maintenance(self):
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.maintenance.retention.plan', {})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.database.coverage', {})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')


if __name__ == '__main__':
    unittest.main()
