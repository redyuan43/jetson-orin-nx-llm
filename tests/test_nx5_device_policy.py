"""CPU owner-contract tests. Native processes and central HTTP are simulated."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_nx5_service import module
from test_nx5_managed_comfy import activity, barrier, TEXT


POLICY = 'op_' + 'd' * 32
EXECUTOR = 'device-policy:nx5-igpu-group'


def demand(mode='comfy', **changes):
    return dict(operation_id=POLICY, profile_id='image-v1' if mode == 'comfy' else 'text-v1',
                profile_version='image-rev' if mode == 'comfy' else 'model-rev', mode=mode,
                activation='controller' if mode == 'comfy' else 'resident', policy_revision=1,
                state='requested', executor_id=EXECUTOR, **{}) | changes


class DevicePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'release.json').write_text(json.dumps({'files': {}, 'release': 'cpu',
            'commit': 'cpu', 'source_commit': 'cpu'}))
        (self.root/'configs').mkdir()
        (self.root/'configs/nx5-minicpm-service.json').write_text(json.dumps({'revision':'model-rev'}))
        location = patch.object(module, '__file__', str(self.root/'scripts/nx5/service.py'))
        location.start()
        self.addCleanup(location.stop)
        self.config = {'root':str(self.root), 'state':str(self.root/'jobs'), 'managed_comfy':True,
            'policy_profiles':{'llm':{'profile_id':'text-v1','version':'model-rev'},
                               'comfy':{'profile_id':'image-v1','version':'image-rev'}}}
        self.service = module.Service(self.config)
        def close_logs():
            self.service.resident.guard_stop.set()
            if self.service.resident.log:
                self.service.resident.log.close()
        self.addCleanup(close_logs)
        self.mode = self.service.mode
        self.central = Mock()
        self.central.group = '/internal/v1/resource-groups/nx5-igpu-group'
        self.mode.central = self.central
        self.mode.activity = Mock(return_value=dict(activity('off'), policy_demand=None))
        self.mode.transition = Mock(return_value=None)
        self.mode.unit = Mock(return_value='inactive')
        self.mode.queue = Mock(return_value=0)
        self.mode.action = Mock()
        self.mode.report = Mock()

    def loaded(self):
        child = Mock(pid=456, stdin=io.StringIO())
        child.poll.return_value = None
        self.service.resident.child = child
        self.service.resident.ready = True
        self.service.resident.model_revision = 'model-rev'
        self.service.resident.record = {'operation_id':TEXT,'status':'held'}
        self.service.resident.central = self.central
        return child

    def policy_activity(self, mode='llm', policy=None, **changes):
        return dict(activity(mode, leases=[{'executor_id':'nx5-text','operation_id':TEXT}]),
                    policy_demand=policy or demand()) | changes

    def worker_done(self):
        worker = getattr(self.mode, 'policy_worker', None)
        if worker:
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'policy loader must not wait with the service lock')

    def test_policy_comfy_yields_only_original_resident_lease_before_barrier(self):
        child = self.loaded()
        self.mode.activity.return_value = self.policy_activity()
        self.mode.tick()
        self.assertEqual(self.service.resident.record['status'], 'released')
        child.wait.assert_called_once()
        self.assertEqual(self.central.release.call_args.args[0]['operation_id'], TEXT)
        self.mode.action.assert_not_called()

    def test_policy_demand_defers_while_text_busy_then_yields(self):
        child = self.loaded()
        self.mode.activity.return_value = self.policy_activity()
        self.service.resident.active = True
        self.mode.tick()
        child.wait.assert_not_called()
        self.service.resident.active = False
        self.service.jobs['in-flight'] = {'status':'running'}
        self.mode.tick()
        child.wait.assert_not_called()
        self.service.jobs['in-flight']['status'] = 'succeeded'
        self.mode.tick()
        self.assertEqual(self.service.resident.record['status'], 'released')

    def test_invalid_profile_version_target_identity_or_state_never_yields(self):
        child = self.loaded()
        invalid = [{'profile_version':'wrong'}, {'profile_id':'unapproved'}, {'mode':'unsupported'},
                   {'activation':'resident'}, {'executor_id':'device-policy:nx4-igpu-group'},
                   {'state':'anything'}, {'policy_revision':True}, {'operation_id':'unstable'}]
        for change in invalid:
            with self.subTest(change=change):
                self.mode.activity.return_value = self.policy_activity(policy=demand(**change))
                with self.assertRaisesRegex(RuntimeError, 'policy_demand'):
                    self.mode.tick()
        child.wait.assert_not_called()
        self.central.release.assert_not_called()

    def test_off_yields_only_original_resident_then_acknowledges_original_barrier(self):
        child = self.loaded()
        policy = demand(mode='llm')
        policy.update(mode='off',activation='controller')
        self.mode.activity.return_value = self.policy_activity(policy=policy)
        self.mode.tick()
        child.wait.assert_called_once()
        self.assertEqual(self.central.release.call_args.args[0]['operation_id'],TEXT)
        child.poll.return_value = 0
        original = barrier('off',POLICY,EXECUTOR)
        self.mode.transition.return_value = original
        self.mode.activity.return_value = dict(activity('off'),policy_demand=policy)
        self.mode.tick()
        self.mode.action.assert_not_called()
        self.assertEqual(self.mode.report.call_args.args,({'mode':'off','state':'ready','active':0},original))
        self.assertEqual(self.mode.policy_state['phase'],'applied')

    def test_off_can_use_comfy_default_profile_and_stop_comfy_only_once(self):
        policy = demand() | dict(mode='off')
        original = barrier('off',POLICY,EXECUTOR)
        self.mode.activity.return_value = dict(activity('comfy'),policy_demand=policy)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['active','inactive','inactive']
        self.mode.tick()
        self.mode.tick()
        self.mode.action.assert_called_once_with('stop')
        self.assertEqual(self.mode.policy_state['phase'],'applied')

    def test_off_does_not_yield_for_stale_waiting_or_foreign_lease(self):
        child = self.loaded()
        policy = demand() | dict(mode='off')
        for changes in [dict(fresh=False),dict(pending=[{'executor_id':'nx5-text','status':'waiting'}]),
                dict(leases=[{'executor_id':'foreign','operation_id':TEXT}])]:
            with self.subTest(changes=changes):
                self.mode.activity.return_value = self.policy_activity(policy=policy,**changes)
                self.mode.tick()
        child.wait.assert_not_called()
        self.central.release.assert_not_called()

    def test_off_busy_comfy_and_unknown_stop_never_dispatch_or_replay(self):
        policy = demand() | dict(mode='off')
        self.mode.activity.return_value = dict(activity('comfy'),policy_demand=policy)
        self.mode.transition.return_value = barrier('off',POLICY,EXECUTOR)
        self.mode.unit.return_value = 'active'
        self.mode.queue.return_value = 1
        self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.queue.return_value = 0
        self.mode.action.side_effect = TimeoutError('uncertain stop')
        with self.assertRaises(TimeoutError):
            self.mode.tick()
        with self.assertRaisesRegex(RuntimeError,'lifecycle_requires_reconciliation'):
            self.mode.tick()
        self.mode.action.assert_called_once_with('stop')

    def test_off_requires_exact_known_default_profile_and_closes_text_admission(self):
        for changes in [dict(profile_id='foreign'), dict(profile_version='wrong'),
                        dict(activation='resident')]:
            self.mode.activity.return_value = dict(activity('off'),
                policy_demand=demand() | dict(mode='off') | changes)
            with self.assertRaisesRegex(RuntimeError,'policy_demand_not_approved'):
                self.mode.tick()
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand() | dict(mode='off'))
        with self.assertRaisesRegex(RuntimeError,'device_mode_waiting'):
            self.mode.gate_text()
        self.mode.action.assert_not_called()

    def test_original_off_barrier_completes_before_later_waiting_media(self):
        policy=demand() | dict(mode='off')
        original=barrier('off',POLICY,EXECUTOR)
        row=dict(activity('comfy',media=True),policy_demand=policy)
        self.mode.activity.return_value=row
        self.mode.transition.return_value=original
        self.mode.unit.side_effect=['active','inactive','inactive']
        self.mode.tick()
        self.mode.tick()
        self.mode.action.assert_called_once_with('stop')
        self.assertEqual(self.mode.report.call_args.args,({'mode':'off','state':'ready','active':0},original))
        self.assertEqual(self.mode.policy_state['phase'],'applied')

    def test_original_off_barrier_does_not_stop_for_already_preparing_media(self):
        policy=demand() | dict(mode='off')
        row=dict(activity('comfy',media=True),policy_demand=policy)
        row['pending'][0]['status']='preparing'
        self.mode.activity.return_value=row
        self.mode.transition.return_value=barrier('off',POLICY,EXECUTOR)
        self.mode.unit.return_value='active'
        self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.report.assert_not_called()

    def test_new_off_effect_waits_for_active_or_unknown_group_but_known_effect_can_report(self):
        policy=demand() | dict(mode='off')
        original=barrier('off',POLICY,EXECUTOR)
        self.mode.transition.return_value=original
        self.mode.unit.return_value='active'
        for changes in [dict(active=1),dict(state='unknown',active=1)]:
            self.mode.activity.return_value=dict(activity('comfy'),policy_demand=policy) | changes
            self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.save(dict(original,phase='actions_complete'))
        self.mode.save_policy({'demand':policy,'phase':'transitioning'})
        self.mode.unit.return_value='inactive'
        self.mode.tick()
        self.assertEqual(self.mode.report.call_args.args,({'mode':'off','state':'ready','active':0},original))

    def test_original_off_barrier_allows_local_waiting_including_its_reservation_loader(self):
        policy=demand() | dict(mode='off')
        original=barrier('off',POLICY,EXECUTOR)
        self.mode.activity.return_value=dict(activity('comfy'),policy_demand=policy)
        self.mode.transition.return_value=original
        self.service.jobs['late']={'status':'waiting','id':TEXT}
        for active in (False,True):
            with self.subTest(reservation_loader_active=active):
                self.mode.save({})
                self.mode.save_policy({})
                self.service.resident.active=active
                self.service.resident.record={'operation_id':TEXT,'status':'reserving'}
                self.mode.unit.side_effect=['active','inactive']
                self.mode.tick()
                self.assertEqual(self.mode.report.call_args.args,({'mode':'off','state':'ready','active':0},original))
                self.assertEqual(self.service.jobs['late'],{'status':'waiting','id':TEXT})
                self.assertEqual(self.service.resident.record,{'operation_id':TEXT,'status':'reserving'})
        self.assertEqual(self.mode.action.call_args_list,[unittest.mock.call('stop')]*2)

    def test_original_off_barrier_still_rejects_running_or_held_local_work(self):
        policy=demand() | dict(mode='off')
        self.mode.activity.return_value=dict(activity('comfy'),policy_demand=policy)
        self.mode.transition.return_value=barrier('off',POLICY,EXECUTOR)
        self.mode.unit.return_value='active'
        self.service.resident.active=True
        for status,lease in [('running','reserving'),('waiting','held')]:
            self.service.jobs['late']={'status':status,'id':TEXT}
            self.service.resident.record={'operation_id':TEXT,'status':lease}
            self.mode.tick()
        self.mode.action.assert_not_called()

    def test_unknown_central_demand_does_not_start_a_new_load_or_yield(self):
        child = self.loaded()
        self.mode.activity.return_value = self.policy_activity(policy=demand(state='unknown'))
        self.mode.tick()
        child.wait.assert_not_called()
        self.mode.action.assert_not_called()

    def test_policy_comfy_barrier_requires_exact_original_demand(self):
        original = barrier('comfy', POLICY, EXECUTOR)
        self.mode.activity.return_value = dict(activity('off'), policy_demand=demand())
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['inactive','active']
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.action.assert_called_once_with('start')
        self.assertEqual(self.mode.intent['operation_id'], POLICY)
        self.assertEqual(self.mode.intent['phase'], 'completed')

    def test_foreign_policy_barrier_cannot_start_comfy(self):
        self.mode.activity.return_value = dict(activity('off'), policy_demand=demand())
        for wrong in [barrier('comfy', TEXT, EXECUTOR),
                      barrier('comfy', POLICY, 'device-policy:nx4-igpu-group'),
                      barrier('llm', POLICY, EXECUTOR)]:
            with self.subTest(transition=wrong):
                self.mode.transition.return_value = wrong
                with self.assertRaisesRegex(RuntimeError, 'unsupported_transition'):
                    self.mode.tick()
        self.mode.action.assert_not_called()

    def test_changed_policy_contract_before_start_cannot_execute(self):
        self.mode.activity.side_effect = [dict(activity('off'), policy_demand=demand()),
            dict(activity('off'), policy_demand=demand(profile_version='wrong'))]
        self.mode.transition.return_value = barrier('comfy', POLICY, EXECUTOR)
        with patch('resident.require_idle'):
            with self.assertRaisesRegex(RuntimeError, 'policy_demand'):
                self.mode.tick()
        self.mode.action.assert_not_called()

    def test_llm_policy_loads_once_outside_service_lock_without_policy_barrier(self):
        self.mode.activity.return_value = dict(activity('off'), policy_demand=demand('llm'), leases=[])
        entered, finish = threading.Event(), threading.Event()
        def load():
            with self.service.lock:
                entered.set()
            if not finish.wait(2):
                raise TimeoutError('test loader was not released')
            self.loaded()
        with patch.object(self.service.resident,'ensure_loaded',side_effect=load) as loader:
            self.mode.tick()
            self.assertTrue(entered.wait(1), 'loader must run without tick retaining its admission lock')
            self.mode.tick()
            finish.set()
            self.worker_done()
            self.mode.tick()
            self.service.resident.child.poll.return_value = 0
            self.mode.tick()
            self.worker_done()
        loader.assert_called_once()
        self.mode.action.assert_not_called()
        self.assertFalse(any('mode-transition' in str(c) for c in self.central.api.call_args_list))

    def test_policy_load_uses_existing_original_reservation_candidate(self):
        self.mode.activity.return_value = dict(activity('off'), policy_demand=demand('llm'), leases=[])
        resident = self.service.resident
        requests = []
        def api(method,path,body=None,controller=False):
            requests.append((method,path,body))
            if path.endswith('/reservations'):
                return {'status':'held','executor_id':'nx5-text','operation_id':body['operation_id'],
                        'request_digest':body['request_digest']}
            return {'status':'held','operation_id':resident.record['operation_id'],'request_digest':'lease-digest'}
        self.central.api.side_effect = api
        child = Mock(pid=456,stdin=io.StringIO(),stdout=io.StringIO('{"type":"ready"}\n'))
        child.poll.return_value = None
        with patch.object(resident,'launch_config',return_value=(['cpu-test'],{},
                {'revision':'model-rev','parameters':{'min_available_mib':0}})), \
             patch('resident.subprocess.Popen',return_value=child), patch('resident.require_idle'), \
             patch.object(resident,'watch'):
            self.mode.tick()
            self.worker_done()
            with patch.object(self.mode,'report',wraps=type(self.mode).report.__get__(self.mode)):
                self.mode.tick()
        self.assertTrue(resident.ready)
        posts = [r for r in requests if r[0]=='POST' and r[1]=='/internal/v1/reservations']
        self.assertEqual(len(posts),1)
        candidate = posts[0][2]['candidates'][0]
        self.assertEqual(candidate['backend_id'],'nx5-ssd-minicpm')
        self.assertEqual(candidate['mapping_version'],'model-rev')
        self.assertNotEqual(posts[0][2]['operation_id'], POLICY)
        observation = next(r[2] for r in requests if r[1].endswith('/observation'))
        self.assertEqual(observation['model_loaded'],True)
        self.assertEqual(observation['profile_version'],'model-rev')

    def test_own_hold_without_native_never_reports_model_loaded(self):
        self.service.resident.record = {'operation_id':TEXT,'status':'held'}
        self.mode.activity.return_value = dict(activity('llm',leases=[
            {'executor_id':'nx5-text','operation_id':TEXT}]),policy_demand=None)
        with patch.object(self.mode,'report',wraps=type(self.mode).report.__get__(self.mode)):
            self.mode.tick()
        self.assertIs(self.central.api.call_args.args[2].get('model_loaded'),False)

    def test_stale_or_foreign_lease_defers_policy_load(self):
        with patch.object(self.service.resident,'ensure_loaded') as loader:
            for changes in [dict(fresh=False),dict(leases=[{'executor_id':'other','operation_id':TEXT}])]:
                self.mode.activity.return_value = dict(activity('off'), policy_demand=demand('llm')) | changes
                self.mode.tick()
                self.worker_done()
        loader.assert_not_called()

    def test_unknown_resident_or_restart_never_replays_policy_load(self):
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand('llm'))
        self.service.resident.blocked = True
        with patch.object(self.service.resident,'ensure_loaded') as loader:
            with self.assertRaisesRegex(RuntimeError,'reconciliation'):
                self.mode.tick()
        loader.assert_not_called()
        self.service.resident.blocked = False
        (self.root/'policy-demand.json').write_text(json.dumps({'phase':'loading','demand':demand('llm')}))
        restarted = module.Service(self.config)
        self.assertEqual(restarted.mode.health()['policy_state'],'unknown')
        with patch.object(restarted.resident,'ensure_loaded') as loader:
            with self.assertRaisesRegex(RuntimeError,'reconciliation'):
                restarted.mode.tick()
        loader.assert_not_called()

    def test_configured_llm_version_must_match_verified_runtime_profile(self):
        self.config['policy_profiles']['llm']['version'] = 'not-the-actual-model-revision'
        with self.assertRaisesRegex(ValueError,'policy_profile'):
            module.Service(self.config)

    def test_cancelled_demand_before_loader_runs_never_loads_model(self):
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand('llm'))
        with patch.object(self.service.resident,'ensure_loaded') as loader:
            # Hold admission until the demand is cancelled, so the new worker
            # cannot start from tick's now-obsolete snapshot.
            with self.service.lock:
                self.mode.tick()
                self.mode.activity.return_value = dict(activity('off'),policy_demand=None)
            self.worker_done()
        loader.assert_not_called()

    def test_failed_policy_load_is_unknown_in_health_and_blocks_new_jobs(self):
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand('llm'))
        with patch.object(self.service.resident,'ensure_loaded',side_effect=TimeoutError('unknown load')):
            self.mode.tick()
            self.worker_done()
        self.assertEqual(self.service.health()['state'],'unknown')
        with patch.object(self.service,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'reconciliation'):
                self.service.submit({'request_id':'next','profile':'minicpm-64k','prompt':'hello'})
        run.assert_not_called()

    def test_unconfigured_service_ignores_new_policy_lifecycle(self):
        config = dict(self.config)
        config.pop('policy_profiles')
        unconfigured = module.Service(config)
        unconfigured.mode.activity = Mock(return_value=dict(activity('off'),policy_demand=demand('llm')))
        unconfigured.mode.transition = Mock(return_value=None)
        unconfigured.mode.unit = Mock(return_value='inactive')
        unconfigured.mode.report = Mock()
        with patch.object(unconfigured.resident,'ensure_loaded') as loader:
            unconfigured.mode.tick()
        loader.assert_not_called()
        self.assertNotIn('model_loaded',unconfigured.mode.report.call_args.args[0])

    def test_no_policy_does_not_add_a_freshness_gate_to_cold_text_admission(self):
        self.mode.activity.return_value = dict(activity('off'),fresh=False,policy_demand=None)
        with patch.object(self.service,'run'):
            accepted = self.service.submit({'request_id':'legacy-cold','profile':'minicpm-64k','prompt':'hello'})
        self.assertEqual(accepted['status'],'waiting')

    def test_unload_refuses_background_load_without_waiting_on_its_model_lock(self):
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand('llm'))
        reserved, finish, unloaded = threading.Event(), threading.Event(), threading.Event()
        result = []
        def load():
            with self.service.resident.close_lock:
                reserved.set()
                if not finish.wait(2):
                    raise TimeoutError('test reservation was not released')
                self.loaded()
        def unload():
            try:result.append(self.service.unload())
            except RuntimeError as exc:result.append(str(exc))
            finally:unloaded.set()
        with patch.object(self.service.resident,'ensure_loaded',side_effect=load):
            self.mode.tick()
            self.assertTrue(reserved.wait(1))
            request = threading.Thread(target=unload)
            request.start()
            try:
                self.assertTrue(unloaded.wait(0.2), 'unload must not retain admission while waiting on the loader')
            finally:
                finish.set()
                request.join(2)
                self.worker_done()
        self.assertEqual(result,['device_policy_loading'])

    def test_recorded_policy_effect_recovers_unknown_central_without_restarting(self):
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand())
        self.mode.transition.return_value = barrier('comfy',POLICY,EXECUTOR)
        self.mode.unit.side_effect = ['inactive','active','active']
        self.mode.queue.side_effect = [ConnectionError('queue not ready'),0]
        with patch('resident.require_idle'):
            with self.assertRaises(ConnectionError):
                self.mode.tick()
            self.mode.activity.return_value = dict(activity('comfy'),policy_demand=demand(state='unknown'))
            self.mode.tick()
        self.mode.action.assert_called_once_with('start')
        self.assertEqual(self.mode.intent['phase'],'completed')

    def test_completed_policy_keeps_reporting_until_slower_central_poll_observes_it(self):
        original = barrier('comfy',POLICY,EXECUTOR)
        self.mode.activity.return_value = dict(activity('off'),policy_demand=demand())
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['inactive','active']
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.unit.side_effect = None
        self.mode.unit.return_value = 'active'
        self.mode.activity.return_value = dict(activity('unknown'),state='unknown',active=1,
                                              policy_demand=demand(state='unknown'))
        # The owner polls every 3 seconds; the central worker may need 15 seconds
        # to consume an accepted observation and remove the original barrier.
        for elapsed in range(3,19,3):
            with self.subTest(elapsed=elapsed):
                self.mode.tick()
                self.assertEqual(self.mode.report.call_args.args,
                    ({'mode':'comfy','state':'ready','active':0},original))
                self.assertEqual(self.mode.intent['phase'],'completed')
                self.assertEqual(self.mode.policy_state['phase'],'applied')
        self.mode.action.assert_called_once_with('start')
        self.assertEqual(self.mode.report.call_count,7)

    def test_recorded_completed_effect_never_repeats_a_lifecycle_command(self):
        original = barrier('comfy',POLICY,EXECUTOR)
        self.mode.transition.return_value = original
        for phase in ['actions_complete','completed']:
            for state in ['requested','unknown']:
                with self.subTest(phase=phase,state=state):
                    self.mode.save(dict(original,phase=phase))
                    self.mode.save_policy({'demand':demand(),'phase':'applied'})
                    self.mode.activity.return_value = dict(activity('unknown'),state='unknown',active=1,
                                                          policy_demand=demand(state=state))
                    with patch('resident.require_idle'):
                        with self.assertRaisesRegex(RuntimeError,'lifecycle_effect_unconfirmed'):
                            self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.report.assert_not_called()

    def test_completed_policy_cannot_adopt_an_unknown_new_operation(self):
        original = barrier('comfy',POLICY,EXECUTOR)
        self.mode.save(dict(original,phase='completed'))
        self.mode.save_policy({'demand':demand(),'phase':'applied'})
        other = 'op_' + 'e' * 32
        self.mode.transition.return_value = barrier('comfy',other,EXECUTOR)
        self.mode.activity.return_value = dict(activity('unknown'),state='unknown',active=1,
                                              policy_demand=demand(state='unknown',operation_id=other))
        with self.assertRaisesRegex(RuntimeError,'unsupported_transition'):
            self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.report.assert_not_called()

    def test_unknown_local_lifecycle_cannot_use_completed_policy_recovery(self):
        original = barrier('comfy',POLICY,EXECUTOR)
        self.mode.save(dict(original,phase='unknown'))
        self.mode.save_policy({'demand':demand(),'phase':'applied'})
        self.mode.transition.return_value = original
        self.mode.activity.return_value = dict(activity('unknown'),state='unknown',active=1,
                                              policy_demand=demand(state='unknown'))
        with self.assertRaisesRegex(RuntimeError,'lifecycle_requires_reconciliation'):
            self.mode.tick()
        self.mode.action.assert_not_called()
        self.mode.report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
