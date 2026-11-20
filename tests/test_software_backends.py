from copy import deepcopy
import unittest

from openedit_engine import api, software_backends as backends


class SoftwareBackendTests(unittest.TestCase):
    def test_catalog_references_real_whitelist_and_guides(self):
        entries = backends.catalog()
        self.assertEqual({item['id'] for item in entries}, {'filmcraft', 'effectcraft', 'ffmpeg'})
        self.assertEqual(len(entries), len({item['id'] for item in entries}))
        required = {'id', 'name', 'short_name', 'license', 'repository', 'roles', 'admitted_roles',
                    'status', 'status_label', 'summary', 'limitations', 'guides', 'operations', 'single_supported'}
        for item in entries:
            self.assertTrue(required <= set(item))
            self.assertTrue(set(item['operations']) <= set(api.operations()), item['id'])
            for g in item['guides']:
                self.assertIn(g, api.paths()['guides'], f'{item["id"]} missing guide {g}')
                self.assertTrue(api.read_guide(g).strip(), f'{item["id"]} empty guide {g}')

    def test_returned_metadata_and_defaults_are_independent(self):
        entries = backends.catalog()
        entries[0]['roles'].append('unknown')
        self.assertNotIn('unknown', backends.catalog()[0]['roles'])
        backends.roles()[0]['label'] = 'changed'
        self.assertEqual(backends.roles()[0]['label'], '剪辑与时间轴')
        selection = backends.default_selection()
        selection['roles']['editing'].clear()
        self.assertEqual(backends.default_selection()['roles']['editing'], ['filmcraft'])

    def test_default_selection_and_workflow_normalization(self):
        default = backends.default_selection()
        self.assertEqual(backends.normalize_selection(default), default)
        self.assertEqual(default['mode'], 'workflow')
        self.assertEqual(default['roles']['editing'], ['filmcraft'])
        self.assertEqual(default['roles']['effects'], ['effectcraft'])
        self.assertEqual(default['roles']['media_prep'], ['ffmpeg'])
        with self.assertRaises(ValueError):
            backends.normalize_selection(None)
        prompt_default = backends.selection_prompt(None)
        self.assertIn('FilmCraft', prompt_default)
        self.assertIn('EffectCraft', prompt_default)

    def test_effects_only_workflow_and_roles(self):
        value = {'mode': 'workflow', 'roles': {'effects': ['effectcraft']}}
        normalized = backends.normalize_selection(value)
        self.assertEqual(normalized['roles']['editing'], [])
        self.assertEqual(normalized['roles']['effects'], ['effectcraft'])
        self.assertNotIn('FilmCraft', backends.selection_prompt(value))

    def test_role_availability_combines_local_installation(self):
        detected = {item['id']: {'selectable': True, 'installed': True, 'label': '可用',
                                 'reason': '已安装'} for item in backends.catalog()}
        by_id = {item['id']: item for item in backends.catalog(detected)}
        self.assertTrue(by_id['filmcraft']['role_availability']['editing']['selectable'])
        self.assertTrue(by_id['effectcraft']['role_availability']['effects']['selectable'])
        detected['effectcraft'].update(selectable=False, installed=False, label='未安装', reason='CLI缺失')
        by_id = {item['id']: item for item in backends.catalog(detected)}
        self.assertFalse(by_id['effectcraft']['role_availability']['effects']['selectable'])
        self.assertEqual(by_id['effectcraft']['role_availability']['effects']['reason'], 'CLI缺失')
        with self.assertRaisesRegex(ValueError, 'CLI缺失'):
            backends.require_available({'mode': 'single', 'single_backend': 'effectcraft'}, list(by_id.values()))

    def test_single_discards_stale_roles_without_mutation(self):
        value = backends.default_selection()
        value.update(mode='single', single_backend='effectcraft')
        before = deepcopy(value)
        normalized = backends.normalize_selection(value)
        self.assertEqual(value, before)
        self.assertTrue(all(not values for values in normalized['roles'].values()))
        self.assertEqual(normalized['single_backend'], 'effectcraft')
        self.assertIn('EffectCraft', backends.selection_prompt(normalized))

    def test_removed_proprietary_software_is_rejected(self):
        for backend in ('jianying', 'resolve', 'premiere', 'after-effects', 'blender'):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                backends.normalize_selection({'mode': 'single', 'single_backend': backend})

    def test_invalid_inputs_are_rejected(self):
        invalid = [None, True, [], 'filmcraft', {}, {'mode': 'unexpected'},
                   {'mode': 'workflow', 'unknown': True},
                   {'mode': 'workflow', 'roles': []},
                   {'mode': 'workflow', 'roles': {'unknown': []}},
                   {'mode': 'workflow', 'roles': {'editing': 'filmcraft'}},
                   {'mode': 'workflow', 'roles': {'editing': [True]}},
                   {'mode': 'workflow', 'roles': {'editing': ['filmcraft', 'effectcraft']}},
                   {'mode': 'workflow', 'roles': {'effects': ['effectcraft', 'effectcraft']}},
                   {'mode': 'workflow', 'roles': {'color': ['effectcraft']}},
                   {'mode': 'workflow', 'roles': {'editing': ['effectcraft']}},
                   {'mode': 'workflow', 'roles': {}},
                   {'mode': 'single'},
                   {'mode': 'single', 'single_backend': True},
                   {'mode': 'single', 'single_backend': 'unknown'},
                   {'mode': 'single', 'single_backend': 'effectcraft', 'roles': {'color': ['effectcraft']}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                backends.normalize_selection(value)


if __name__ == '__main__':
    unittest.main()
