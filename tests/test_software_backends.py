from copy import deepcopy
import unittest

from jianying_engine import api, software_backends as backends


class SoftwareBackendTests(unittest.TestCase):
    def test_catalog_references_real_whitelist_and_guides(self):
        entries = backends.catalog()
        self.assertEqual({item['id'] for item in entries}, {'jianying', 'premiere', 'after-effects', 'resolve'})
        self.assertEqual(len(entries), len({item['id'] for item in entries}))
        required = {'id', 'name', 'short_name', 'roles', 'admitted_roles', 'status', 'status_label',
                    'summary', 'limitations', 'guides', 'operations', 'single_supported'}
        for item in entries:
            self.assertTrue(required <= set(item))
            self.assertTrue(set(item['operations']) <= set(api.operations()), item['id'])
            self.assertTrue(set(item['guides']) <= set(api.paths()['guides']), item['id'])

    def test_returned_metadata_and_defaults_are_independent(self):
        entries = backends.catalog()
        entries[0]['roles'].append('unknown')
        self.assertNotIn('unknown', backends.catalog()[0]['roles'])
        backends.roles()[0]['label'] = 'changed'
        self.assertEqual(backends.roles()[0]['label'], '剪辑')
        selection = backends.default_selection()
        selection['roles']['editing'].clear()
        self.assertEqual(backends.default_selection()['roles']['editing'], ['resolve'])

    def test_new_default_and_unbound_legacy_are_distinct(self):
        self.assertEqual(backends.normalize_selection(backends.default_selection()), backends.default_selection())
        with self.assertRaises(ValueError):
            backends.normalize_selection(None)
        legacy = backends.selection_prompt(None)
        self.assertIn('沿用此会话已有工程', legacy)
        self.assertIn('不应用新会话默认组合', legacy)
        self.assertNotIn('DaVinci', legacy)

    def test_effects_need_no_editing_role(self):
        value = {'mode': 'workflow', 'roles': {'effects': ['after-effects']}}
        normalized = backends.normalize_selection(value)
        self.assertEqual(normalized['roles']['editing'], [])
        self.assertEqual(normalized['roles']['effects'], ['after-effects'])
        self.assertNotIn('DaVinci Resolve', backends.selection_prompt(value))

    def test_product_roles_do_not_grant_unadmitted_functions(self):
        by_id = {item['id']: item for item in backends.catalog()}
        self.assertEqual(by_id['jianying']['roles'], ['editing'])
        self.assertEqual(by_id['after-effects']['roles'], ['effects'])
        self.assertEqual(by_id['resolve']['roles'], ['editing', 'effects', 'color', 'audio'])
        self.assertEqual(by_id['premiere']['roles'], ['editing', 'effects', 'color', 'audio'])
        for backend, item in by_id.items():
            for role in item['roles']:
                value = {'mode': 'workflow', 'roles': {role: [backend]}}
                if role in item['admitted_roles']:
                    self.assertEqual(backends.normalize_selection(value)['roles'][role], [backend])
                else:
                    with self.subTest(backend=backend, role=role), self.assertRaisesRegex(ValueError, '尚未准入'):
                        backends.normalize_selection(value)

    def test_role_availability_combines_local_installation_and_admission(self):
        detected = {item['id']: {'selectable': True, 'installed': True, 'label': '本机可用',
                                 'reason': '已安装'} for item in backends.catalog()}
        by_id = {item['id']: item for item in backends.catalog(detected)}
        self.assertTrue(by_id['resolve']['role_availability']['editing']['selectable'])
        self.assertTrue(by_id['after-effects']['role_availability']['effects']['selectable'])
        self.assertFalse(by_id['resolve']['role_availability']['color']['selectable'])
        self.assertEqual(by_id['resolve']['role_availability']['color']['label'], '尚未适配')
        self.assertTrue(all(not state['selectable'] for state in by_id['premiere']['role_availability'].values()))
        detected['resolve'].update(selectable=False, installed=False, label='未安装', reason='应用已卸载')
        by_id = {item['id']: item for item in backends.catalog(detected)}
        for role in by_id['resolve']['roles']:
            self.assertFalse(by_id['resolve']['role_availability'][role]['selectable'])
            self.assertEqual(by_id['resolve']['role_availability'][role]['reason'], '应用已卸载')
        with self.assertRaisesRegex(ValueError, '应用已卸载'):
            backends.require_available({'mode': 'single', 'single_backend': 'resolve'}, list(by_id.values()))

    def test_single_discards_stale_valid_roles_without_mutation(self):
        value = backends.default_selection()
        value.update(mode='single', single_backend='after-effects')
        before = deepcopy(value)
        normalized = backends.normalize_selection(value)
        self.assertEqual(value, before)
        self.assertTrue(all(not values for values in normalized['roles'].values()))
        self.assertEqual(normalized['single_backend'], 'after-effects')
        prompt = backends.selection_prompt(normalized)
        self.assertIn('无需主剪辑链', prompt)
        self.assertIn('adobe-workflow validate/prepare/run-ae/verify', prompt)
        self.assertNotIn('resolve-workflow', prompt)
        self.assertNotIn('Premiere', prompt)
        self.assertNotIn('shot-exchange', prompt)
        self.assertIn('run-ae --ae-root', prompt)
        self.assertNotIn('adobe-workflow doctor', prompt)

    def test_all_selectable_single_backends_have_bounded_context(self):
        for item in backends.catalog():
            if item['single_supported']:
                value = {'mode': 'single', 'single_backend': item['id']}
                self.assertEqual(backends.normalize_selection(value)['single_backend'], item['id'])
                self.assertIn(item['name'], backends.selection_prompt(value))
        audio = backends.selection_prompt({'mode': 'single', 'single_backend': 'jianying'})
        self.assertNotIn('resolve-workflow', audio)
        self.assertIn('工作台内部工具', audio)

    def test_removed_software_is_not_a_disabled_or_planned_candidate(self):
        for backend in ('blender', 'kdenlive', 'local-audio'):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                backends.normalize_selection({'mode': 'single', 'single_backend': backend})

    def test_invalid_inputs_are_rejected(self):
        invalid = [None, True, [], 'resolve', {}, {'mode': 'unexpected'},
                   {'mode': 'workflow', 'unknown': True},
                   {'mode': 'workflow', 'roles': []},
                   {'mode': 'workflow', 'roles': {'unknown': []}},
                   {'mode': 'workflow', 'roles': {'editing': 'resolve'}},
                   {'mode': 'workflow', 'roles': {'editing': [True]}},
                   {'mode': 'workflow', 'roles': {'editing': ['resolve', 'jianying']}},
                   {'mode': 'workflow', 'roles': {'effects': ['blender', 'blender']}},
                   {'mode': 'workflow', 'roles': {'audio': ['missing']}},
                   {'mode': 'workflow', 'roles': {'color': ['resolve']}},
                   {'mode': 'workflow', 'roles': {'editing': ['after-effects']}},
                   {'mode': 'workflow', 'roles': {'effects': ['premiere']}},
                   {'mode': 'workflow', 'roles': {'editing': ['premiere']}},
                   {'mode': 'workflow', 'roles': {}},
                   {'mode': 'workflow', 'roles': {'audio': ['local-audio']}, 'single_backend': 'blender'},
                   {'mode': 'single'}, {'mode': 'single', 'single_backend': []},
                   {'mode': 'single', 'single_backend': True},
                   {'mode': 'single', 'single_backend': 'unknown'},
                   {'mode': 'single', 'single_backend': 'premiere'},
                   {'mode': 'single', 'single_backend': 'after-effects', 'roles': {'color': ['resolve']}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                backends.normalize_selection(value)


if __name__ == '__main__':
    unittest.main()
