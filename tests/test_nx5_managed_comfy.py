"""CPU protocol tests: no device, model or production service is contacted."""
import json
import io
import configparser
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_nx5_service import module


MEDIA = 'op_' + 'a' * 32
TEXT = 'op_' + 'b' * 32


def activity(mode='llm', *, media=False, transition=None, leases=None, pending=None):
    return {'group_id': 'nx5-igpu-group', 'resources': ['nx5-igpu'],
            'mode': mode, 'state': 'ready', 'active': 0, 'fresh': True,
            'leases': leases or [], 'pending': [{'executor_id': 'nx6-image-executor',
                'operation_id': MEDIA, 'status': 'waiting',
                'resources': [{'resource_id': 'nx5-igpu', 'units': 1}]}] if media else (pending or []),
            'transition': transition}


def barrier(mode='comfy', operation=MEDIA, executor='nx6-image-executor'):
    return {'group_id': 'nx5-igpu-group', 'resources': ['nx5-igpu'],
            'executor_id': executor, 'operation_id': operation,
            'target_mode': mode, 'lease_barrier': True}


class ManagedComfyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'release.json').write_text(json.dumps({'files': {}, 'release': 'cpu',
            'commit': 'cpu', 'source_commit': 'cpu'}))
        self.location = patch.object(module, '__file__', str(self.root/'scripts/nx5/service.py'))
        self.location.start()
        self.addCleanup(self.location.stop)
        self.config = {'root': str(self.root), 'state': str(self.root/'jobs'), 'managed_comfy': True}
        self.service = module.Service(self.config)
        self.mode = getattr(self.service, 'mode', None)
        self.assertIsNotNone(self.mode, 'managed_comfy must install the NX5 lifecycle owner')
        self.central = Mock()
        self.mode.central = self.central
        self.central.group = '/internal/v1/resource-groups/nx5-igpu-group'
        self.mode.activity = Mock(return_value=activity())
        self.mode.transition = Mock(return_value=None)
        self.mode.unit = Mock(return_value='inactive')
        self.mode.flint_unit = Mock(return_value='inactive')
        self.mode.flint_gate_unit = Mock(return_value='inactive')
        self.mode.queue = Mock(return_value=0)
        self.mode.action = Mock()
        self.mode.flint_action = Mock()
        self.mode.wait_flint_ready = Mock(return_value={'ready':True,'active':0,'slots':1})
        self.mode.report = Mock()

    def load_resident(self):
        child = Mock(pid=456)
        child.poll.return_value = None
        self.service.resident.child = child
        self.service.resident.ready = True
        self.service.resident.record = {'operation_id': TEXT, 'status': 'held'}
        self.service.resident.central = self.central
        return child

    def test_idle_waiting_media_releases_original_resident_before_start(self):
        self.load_resident()
        self.mode.activity.return_value = activity(media=True)
        self.mode.tick()
        self.assertEqual(self.service.resident.record['status'], 'released')
        self.assertEqual(self.central.release.call_args.args[0]['operation_id'], TEXT)
        self.assertEqual(self.mode.intent['media_operation_id'], MEDIA)
        self.mode.action.assert_not_called()  # waiting is not a lifecycle barrier

    def test_busy_text_is_not_unloaded_for_media(self):
        child = self.load_resident()
        self.service.jobs['current'] = {'status': 'running'}
        self.mode.activity.return_value = activity(media=True)
        self.mode.tick()
        child.wait.assert_not_called()
        self.central.release.assert_not_called()

    def test_new_text_cannot_extend_resident_past_waiting_media(self):
        self.load_resident()
        self.mode.activity.return_value = activity(media=True)
        with patch.object(self.service, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'device_mode_waiting'):
                self.service.submit({'request_id': 'new', 'profile': 'minicpm-64k',
                                     'prompt': 'hello', 'max_tokens': 8})
        run.assert_not_called()
        self.assertFalse(self.service.jobs)

    def test_cancelled_waiting_media_reopens_admission_without_unload(self):
        child = self.load_resident()
        self.mode.intent = {'phase': 'yielding', 'media_operation_id': MEDIA}
        self.mode.activity.return_value = activity()
        self.mode.tick()
        self.assertEqual(self.mode.intent['phase'], 'completed')
        child.wait.assert_not_called()

    def test_wrong_group_or_stale_observation_cannot_yield(self):
        child = self.load_resident()
        for change in [{'resources': ['nx4-igpu']}, {'group_id': 'nx4-igpu-group'}]:
            with self.subTest(change=change):
                self.mode.activity.return_value = dict(activity(media=True), **change)
                with self.assertRaises(RuntimeError):
                    self.mode.tick()
        child.wait.assert_not_called()

    def test_changed_barrier_never_starts_comfy(self):
        original = barrier()
        self.mode.activity.return_value = activity(mode='off', transition=original)
        self.mode.transition.side_effect = [original, barrier(operation='op_'+'c'*32)]
        with self.assertRaisesRegex(RuntimeError, 'transition_changed'):
            self.mode.tick()
        self.mode.action.assert_not_called()

    def test_matching_media_barrier_starts_comfy_once(self):
        original = barrier()
        self.mode.activity.return_value = activity(mode='off', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['inactive', 'active', 'active']
        with patch('resident.require_idle'):
            self.mode.tick()
            self.mode.tick()
        self.mode.action.assert_called_once_with('start')
        self.assertEqual(self.mode.intent['phase'], 'completed')
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'comfy','state':'ready','active':0})

    def test_busy_comfy_is_not_stopped_for_text(self):
        original = barrier('llm', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id': TEXT, 'status': 'reserving'}
        self.mode.activity.return_value = activity('comfy', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'active'
        self.mode.queue.return_value = 1
        self.mode.tick()
        self.mode.action.assert_not_called()

    def test_foreign_text_transition_cannot_stop_comfy(self):
        original = barrier('llm', TEXT, 'ai-text-pool')
        self.service.resident.record = {'operation_id': TEXT, 'status': 'reserving'}
        self.mode.activity.return_value = activity('comfy', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'active'
        with self.assertRaisesRegex(RuntimeError, 'unsupported_transition'):
            self.mode.tick()
        self.mode.action.assert_not_called()

    def test_lost_start_response_is_not_replayed(self):
        original = barrier()
        self.mode.activity.return_value = activity('off', transition=original)
        self.mode.transition.return_value = original
        self.mode.action.side_effect = TimeoutError('unknown lifecycle result')
        with patch('resident.require_idle'):
            with self.assertRaises(TimeoutError):
                self.mode.tick()
            with self.assertRaisesRegex(RuntimeError, 'lifecycle_requires_reconciliation'):
                self.mode.tick()
        self.mode.action.assert_called_once_with('start')

    def test_restart_preserves_unknown_lifecycle_without_replay(self):
        self.mode.save({'phase':'dispatching','operation_id':MEDIA,'executor_id':'nx6-image-executor',
                        'target_mode':'comfy'})
        restarted = module.Service(self.config)
        self.assertEqual(restarted.mode.intent['phase'], 'unknown')
        with patch.object(restarted, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                restarted.submit({'request_id':'new','profile':'minicpm-64k','prompt':'hi'})
        run.assert_not_called()

    def test_partial_comfy_queue_is_unknown(self):
        with patch.object(self.mode, 'queue', wraps=type(self.mode).queue.__get__(self.mode)), \
             patch.object(self.mode, 'request', return_value={'queue_running': []}):
            with self.assertRaisesRegex(RuntimeError, 'incomplete_comfy_activity'):
                self.mode.queue()

    def test_stale_central_group_is_refreshed_without_yielding(self):
        child = self.load_resident()
        self.mode.activity.return_value = dict(activity(media=True), fresh=False)
        self.mode.tick()
        child.wait.assert_not_called()
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'llm','state':'ready','active':0})

    def test_loading_observation_is_not_misreported_as_ready(self):
        with patch.object(self.mode, 'report', wraps=type(self.mode).report.__get__(self.mode)):
            self.mode.report({'mode':'llm','state':'loading','active':1})
        self.assertEqual(self.central.api.call_args.args[2],
                         {'mode':'llm','state':'loading','active':1,'operation_id':None})

    def test_flint_reservation_uses_flint_backend_and_mode(self):
        sent = []
        def api(method, path, body=None, controller=False):
            sent.append((method, path, body))
            if method == 'POST':
                return {'status':'held','operation_id':body['operation_id'],
                        'executor_id':'nx5-text','request_digest':body['request_digest']}
            return {'status':'held','operation_id':path.rsplit('/', 1)[-1],
                    'request_digest':sent[0][2]['request_digest']}
        self.central.api.side_effect = api
        save = Mock()
        self.mode.acquire({'model':'flint','resident_session':'session'}, 'profile-v1', save,
                          backend_id='nx5-flint', mode='flint')
        reservation = next(body for method, path, body in sent if method == 'POST')
        self.assertEqual({k:v for k,v in reservation['candidates'][0].items() if k != 'probed_at'}, {
            'backend_id':'nx5-flint','mapping_version':'profile-v1',
            'resources':[{'resource_id':'nx5-igpu','units':1}], 'mode':'flint',
            'probe_status':'ready'
        })

    def test_waiting_flint_gate_work_is_confirmed_by_its_profile_identity(self):
        self.mode.flint_gate_unit.return_value = 'active'
        self.mode.policy_profiles = {'flint': {'profile_id':'nx5-flint','version':'a' * 64}}
        self.mode.flint_gate_activity = Mock(return_value={
            'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'flint',
            'profile_id':'nx5-flint','profile_digest':'a'*64,'active':0,'unknown':0,'waiting':1,
            'ingress_closed':True,'enabled':True,'public_enabled':False,'qualification_only':True,
            'owner_admission':False,'observed_at':time.time()})
        pending = self.mode.demand_for_mode('flint')
        self.assertEqual(pending['profile_id'], 'nx5-flint')
        self.assertEqual(pending['waiting'], 1)
        self.mode.flint_gate_activity.assert_called_once_with()

    def test_unknown_or_stale_flint_gate_activity_never_yields_minicpm(self):
        self.mode.flint_gate_unit.return_value = 'active'
        self.mode.policy_profiles = {'flint': {'profile_id':'nx5-flint','version':'a' * 64}}
        self.mode.flint_gate_activity = Mock()
        baseline = {'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'flint',
            'profile_id':'nx5-flint','profile_digest':'a'*64,'active':0,'unknown':0,'waiting':1,
            'ingress_closed':True,'enabled':True,'public_enabled':False,'qualification_only':True,
            'owner_admission':False,'observed_at':time.time()}
        for changes in ({'profile_id':'other'}, {'unknown':1}, {'active':1},
                        {'profile_digest':'b'*64}, {'observed_at':time.time()-61},
                        {'resources':['nx6-igpu']}):
            with self.subTest(changes=changes):
                self.mode.flint_gate_activity.return_value = baseline | changes
                if changes in ({'unknown':1}, {'active':1}):
                    self.assertIsNone(self.mode.demand_for_mode('flint'))
                elif changes == {'profile_digest':'b'*64}:
                    with self.assertRaisesRegex(RuntimeError, 'flint_gate_profile_mismatch'):
                        self.mode.demand_for_mode('flint')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'flint_gate_activity_unknown'):
                        self.mode.demand_for_mode('flint')

    def test_busy_flint_slot_prevents_comfy_switch(self):
        original = barrier('comfy')
        self.mode.activity.return_value = activity('flint', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'inactive'
        self.mode.flint_unit.return_value = 'active'
        self.mode.flint_runtime = Mock(return_value={'ready':True,'active':1,'slots':1})
        self.mode.tick()
        self.mode.flint_action.assert_not_called()

    def test_flint_to_llm_switch_stops_idle_flint_before_reporting_target(self):
        original = barrier('llm', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'backend_id':'nx5-ssd-minicpm',
            'mode':'llm','status':'reserving'}
        self.mode.activity.return_value = activity('flint', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'inactive'
        self.mode.flint_unit = Mock(side_effect=['active','inactive'])
        self.mode.flint_runtime = Mock(return_value={'ready':True,'active':0,'slots':1})
        self.mode.flint_action = Mock()

        with patch('resident.require_idle'):
            self.mode.tick()

        self.mode.flint_action.assert_called_once_with('stop')
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'llm','state':'ready','active':0})

    def test_busy_flint_blocks_off_transition_without_stopping_or_reporting(self):
        original = barrier('off', 'device-policy:nx5-igpu-group', 'device-policy:nx5-igpu-group')
        self.mode.activity.return_value = activity('flint', transition=original)
        self.mode.transition.return_value = original
        self.mode.policy_demand = Mock(return_value={
            'mode':'off','state':'requested','operation_id':original['operation_id'],
            'profile_id':'nx5-flint','profile_version':'flint-v1','activation':'resident',
            'policy_revision':2,'executor_id':'device-policy:nx5-igpu-group'})
        self.mode.policy_comfy_transition = Mock(return_value=True)
        self.mode.flint_unit.return_value = 'active'
        self.mode.flint_runtime = Mock(return_value={'ready':True,'active':1,'slots':1})
        self.mode.flint_action = Mock()

        self.mode.tick()

        self.mode.flint_action.assert_not_called()
        self.mode.report.assert_not_called()
        self.mode.action.assert_not_called()

    def test_text_transition_waits_for_foreign_lease_even_with_idle_comfy(self):
        original = barrier('llm', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'status':'reserving'}
        self.mode.activity.return_value = activity('comfy', transition=original,
            leases=[{'executor_id':'nx6-image-executor','operation_id':MEDIA}])
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'active'
        self.mode.tick()
        self.mode.action.assert_not_called()

    def test_matching_original_text_barrier_stops_idle_comfy(self):
        original = barrier('llm', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'status':'reserving'}
        self.mode.activity.return_value = activity('comfy', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['active', 'inactive']
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.action.assert_called_once_with('stop')
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'llm','state':'ready','active':0})

    def test_matching_flint_transition_starts_only_the_flint_owner(self):
        original = barrier('flint', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'backend_id':'nx5-flint',
            'mode':'flint','status':'reserving'}
        self.mode.activity.return_value = activity('off', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.return_value = 'inactive'
        self.mode.flint_unit = Mock(side_effect=['inactive', 'active'])
        self.mode.flint_action = Mock()
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.flint_action.assert_called_once_with('start')
        self.mode.action.assert_not_called()
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'flint','state':'ready','active':0})

    def test_initial_flint_gate_operation_is_admitted_with_new_operation_id(self):
        operation = 'op_' + 'f' * 32
        original = barrier('flint', operation, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'backend_id':'nx5-ssd-minicpm',
            'mode':'llm','status':'released'}
        self.mode.policy_profiles = {'flint': {'profile_id':'nx5-flint','version':'a' * 64}}
        self.mode.activity.return_value = activity('off', transition=original, pending=[{
            'executor_id':'nx5-text','operation_id':operation,'status':'preparing',
            'resources':[{'resource_id':'nx5-igpu','units':1}]}])
        self.mode.transition.return_value = original
        self.mode.flint_gate_unit.return_value = 'active'
        self.mode.flint_gate_activity = Mock(return_value={
            'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'flint',
            'profile_id':'nx5-flint','profile_digest':'a' * 64,'active':0,'unknown':0,'waiting':1,
            'enabled':True,'public_enabled':False,'qualification_only':True,'owner_admission':False,
            'ingress_closed':True,'observed_at':time.time()})
        self.mode.unit.return_value = 'inactive'
        self.mode.flint_unit = Mock(side_effect=['inactive','active'])

        with patch('resident.require_idle'):
            self.mode.tick()

        self.mode.flint_action.assert_called_once_with('start')
        self.mode.action.assert_not_called()
        self.assertEqual(self.mode.report.call_args.args[0], {'mode':'flint','state':'ready','active':0})

    def test_flint_transition_drains_comfy_before_starting_flint(self):
        original = barrier('flint', TEXT, 'nx5-text')
        self.service.resident.record = {'operation_id':TEXT,'backend_id':'nx5-flint',
            'mode':'flint','status':'reserving'}
        self.mode.activity.return_value = activity('comfy', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['active', 'inactive']
        self.mode.flint_unit.side_effect = ['inactive', 'active']
        self.mode.wait_flint_ready = Mock(return_value={'ready':True,'active':0,'slots':1})
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.action.assert_called_once_with('stop')
        self.mode.flint_action.assert_called_once_with('start')

    def test_comfy_transition_stops_idle_flint_before_start(self):
        original = barrier('comfy')
        self.mode.activity.return_value = activity('flint', transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['inactive', 'active']
        self.mode.flint_unit.side_effect = ['active', 'inactive', 'inactive']
        self.mode.flint_runtime = Mock(return_value={'ready':True,'active':0,'slots':1})
        with patch('resident.require_idle'):
            self.mode.tick()
        self.mode.flint_action.assert_called_once_with('stop')
        self.mode.action.assert_called_once_with('start')

    def test_lost_reservation_response_queries_original_without_another_submit(self):
        saved = {}
        requests = []
        def api(method, path, body=None, controller=False):
            requests.append((method,path))
            if method == 'POST' and path == '/internal/v1/reservations':
                raise TimeoutError('response lost after accepted')
            if path.startswith('/internal/v1/reservations/'):
                return {'status':'held','operation_id':saved['operation_id'], 'executor_id':'nx5-text', 'request_digest':saved['request_digest']}
            return {'status':'held','operation_id':saved['operation_id'],'request_digest':'lease-digest'}
        self.central.api.side_effect = api
        result = self.mode.acquire({'request':'text'}, 'v1', lambda r:saved.update(r))
        self.assertEqual(result['lease_digest'], 'lease-digest')
        self.assertEqual(requests, [('POST','/internal/v1/reservations'),
            ('GET','/internal/v1/reservations/'+saved['operation_id']),
            ('GET','/internal/v1/leases/'+saved['operation_id'])])

    def test_queued_text_claims_only_the_existing_original_reservation(self):
        saved, requests = {}, []
        def api(method,path,body=None,controller=False):
            requests.append((method,path))
            if path.endswith('/leases/'+saved.get('operation_id','missing')):
                return {'status':'held','operation_id':saved['operation_id'],'request_digest':'lease-digest'}
            return {'status':'held' if path.endswith('/claim') else 'waiting', 'operation_id':saved['operation_id'], 'executor_id':'nx5-text', 'request_digest':saved['request_digest']}
        self.central.api.side_effect = api
        with patch.object(self.mode.stop, 'wait', return_value=False):
            result = self.mode.acquire({'request':'text'}, 'v1', lambda r:saved.update(r))
        path = '/internal/v1/reservations/'+saved['operation_id']
        self.assertEqual(result['status'], 'held')
        self.assertEqual(requests[:3], [('POST','/internal/v1/reservations'),('GET',path),('POST',path+'/claim')])

    def test_unconfirmed_resident_release_keeps_original_and_never_starts_comfy(self):
        self.load_resident()
        self.mode.activity.return_value = activity(media=True)
        self.central.release.side_effect = TimeoutError('release response lost')
        with self.assertRaisesRegex(RuntimeError, 'resident_requires_reconciliation'):
            self.mode.tick()
        self.assertTrue(self.service.resident.blocked)
        self.assertEqual(self.service.resident.record['operation_id'], TEXT)
        self.assertEqual(self.service.resident.record['status'], 'unknown')
        self.mode.action.assert_not_called()

    def test_duplicate_job_remains_queryable_after_lifecycle_becomes_unknown(self):
        with patch.object(self.service, 'run'):
            original = self.service.submit({'request_id':'same','profile':'minicpm-64k','prompt':'hello'})
            self.mode.save({'phase':'unknown','operation_id':MEDIA})
            self.assertEqual(self.service.submit({'request_id':'same','profile':'minicpm-64k','prompt':'hello'})['id'], original['id'])

    def test_confirmed_comfy_effect_recovers_after_ready_report_response_was_lost(self):
        self.mode.save(dict(barrier(), phase='actions_complete'))
        self.mode.activity.return_value = activity('comfy')
        self.mode.unit.return_value = 'active'
        self.mode.tick()
        self.assertEqual(self.mode.intent['phase'], 'completed')
        self.mode.action.assert_not_called()

    def test_unknown_lifecycle_is_not_published_as_ready(self):
        self.mode.save({'phase':'unknown','operation_id':MEDIA})
        self.assertEqual(self.service.health()['state'], 'unknown')

    def test_reservation_identity_conflict_is_rejected_before_model_can_start(self):
        for changed in [{'operation_id':'op_'+'f'*32}, {'request_digest':'f'*64}, {'executor_id':'other'}]:
            with self.subTest(changed=changed):
                saved = {}
                def api(method,path,body=None,controller=False):
                    return dict(status='held', operation_id=saved['operation_id'], executor_id='nx5-text',
                                request_digest=saved['request_digest'], **{}) | changed
                self.central.api.side_effect = api
                with self.assertRaisesRegex(RuntimeError, 'reservation_identity_mismatch'):
                    self.mode.acquire({'request':'text'},'v1',lambda r:saved.update(r))

    def test_claim_conflict_is_rejected_without_replacing_original_operation(self):
        saved, requests = {}, []
        def api(method,path,body=None,controller=False):
            requests.append((method,path))
            return {'status':'held' if path.endswith('/claim') else 'waiting',
                    'operation_id':saved['operation_id'], 'executor_id':'nx5-text',
                    'request_digest':'f'*64 if path.endswith('/claim') else saved['request_digest']}
        self.central.api.side_effect = api
        with patch.object(self.mode.stop,'wait',return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'reservation_identity_mismatch'):
                self.mode.acquire({'request':'text'},'v1',lambda r:saved.update(r))
        self.assertEqual(len([r for r in requests if r == ('POST','/internal/v1/reservations')]),1)

    def test_comfy_health_recovers_without_repeating_successful_start(self):
        original = barrier()
        self.mode.activity.return_value = activity('off',transition=original)
        self.mode.transition.return_value = original
        self.mode.unit.side_effect = ['inactive','active','active']
        self.mode.queue.side_effect = [ConnectionError('still loading'),0]
        with patch('resident.require_idle'):
            with self.assertRaises(ConnectionError):
                self.mode.tick()
            self.assertEqual(self.mode.intent['phase'],'actions_complete')
            self.mode.tick()
        self.mode.action.assert_called_once_with('start')
        self.assertEqual(self.mode.intent['phase'],'completed')

    def test_admission_cannot_race_into_idle_yield(self):
        child = self.load_resident()
        self.mode.activity.return_value = activity(media=True)
        unloading, finish_unload, admitted = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def wait_for_exit(*_args,**_kwargs):
            unloading.set()
            if not finish_unload.wait(2):
                raise TimeoutError('test did not release model exit')
        child.wait.side_effect = wait_for_exit
        def tick():
            try:self.mode.tick()
            except Exception as exc:failures.append(exc)
        def admit():
            try:self.service.submit({'request_id':'racing','profile':'minicpm-64k','prompt':'hello'})
            except RuntimeError as exc:failures.append(str(exc))
            finally:admitted.set()
        with patch.object(self.service,'run') as run:
            owner = threading.Thread(target=tick)
            owner.start()
            self.assertTrue(unloading.wait(2))
            request = threading.Thread(target=admit)
            request.start()
            self.assertFalse(admitted.wait(0.05))
            finish_unload.set()
            owner.join(2);request.join(2)
            self.assertFalse(owner.is_alive() or request.is_alive())
        self.assertEqual(failures,['device_mode_waiting'])
        run.assert_not_called()

    def test_waiting_ensure_loaded_does_not_block_controller_from_stopping_comfy(self):
        resident = self.service.resident
        reserved, ready = threading.Event(), threading.Event()
        result, errors = [], []
        state = {'unit':'active','ready':False}
        self.service.jobs['queued'] = {'status':'waiting'}
        self.mode.unit.side_effect = lambda:state['unit']
        def transition():
            return barrier('llm',resident.record['operation_id'],'nx5-text')
        self.mode.transition.side_effect = transition
        self.mode.activity.side_effect = lambda:activity('comfy',transition=transition())
        def action(verb):state['unit']='inactive'
        self.mode.action.side_effect = action
        def report(observation,original=None):
            if observation['mode']=='llm':state['ready']=True;ready.set()
        self.mode.report.side_effect = report
        def api(method,path,body=None,controller=False):
            operation = resident.record['operation_id']
            if path.startswith('/internal/v1/leases/'):
                return {'status':'held','operation_id':operation,'request_digest':'lease-digest'}
            reserved.set()
            return {'status':'held' if state['ready'] else 'preparing','operation_id':operation,
                    'executor_id':'nx5-text','request_digest':resident.record['request_digest']}
        self.central.api.side_effect = api
        child = Mock(pid=456,stdin=io.StringIO(),stdout=io.StringIO('{"type":"ready"}\n'))
        child.poll.return_value=None
        def load():
            try:result.append(resident.ensure_loaded())
            except Exception as exc:errors.append(exc)
        with patch.object(resident,'launch_config',return_value=(['cpu-test'],{}, {'revision':'v1','parameters':{'min_available_mib':0}})), \
             patch('resident.subprocess.Popen',return_value=child), patch('resident.require_idle'), \
             patch.object(self.mode.stop,'wait',side_effect=lambda timeout:ready.wait(2) and False):
            loader=threading.Thread(target=load)
            loader.start()
            self.assertTrue(reserved.wait(2))
            # ensure_loaded holds close_lock while waiting on central admission.
            self.assertFalse(resident.close_lock.acquire(blocking=False))
            self.mode.tick()
            loader.join(2)
            resident.guard_stop.set()
            self.assertFalse(loader.is_alive())
        self.assertFalse(errors)
        self.assertEqual(result,[True])
        resident.close()
        self.mode.action.assert_called_once_with('stop')

    def test_waiting_does_not_consume_generation_budget_and_wall_includes_load(self):
        resident = self.service.resident
        resident.child = Mock(pid=456,stdin=io.StringIO())
        final = {'type':'final','usage':{}}
        with patch.object(resident,'ensure_loaded',return_value=True), \
             patch.object(resident,'next_event',return_value=final) as next_event, \
             patch('resident.memory_available',return_value=1500), \
             patch('resident.time.monotonic',side_effect=[10,20,30]):
            _,metrics=resident.generate({},8,self.root/'result.out',lambda pid:None)
        self.assertEqual(next_event.call_args.args[0],2420)
        self.assertEqual(metrics['process_wall_s'],20)

    def test_service_unit_allows_managed_comfy_but_keeps_legacy_owners_excluded(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).resolve().parents[1]/'deploy/nx5/nx5-ssd-llm.service')
        conflicts = set(unit['Unit']['Conflicts'].split())
        self.assertNotIn('siyuan-image-comfy.service',conflicts,
                         'Starting managed Comfy must not stop its SSD lifecycle owner')
        self.assertTrue({'siyuan-fleet-control.service','siyuan-text-ingress.service',
                         'siyuan-text-compat-bridge.service','nx5-ornith-35b-a3b-iq2s.service',
                         'qwen38-kvmem.service'} <= conflicts)


if __name__ == '__main__':
    unittest.main()
