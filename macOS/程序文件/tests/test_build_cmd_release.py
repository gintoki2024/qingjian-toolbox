"""The portable inventory and notices describe its selected runtime only."""
from email.message import Message
from pathlib import Path
from unittest.mock import patch
import json

from build_cmd import export_runtime_licenses


class InstalledDistribution:
    def __init__(self, folder):
        self.folder = folder
        self.version = '2.0'
        self.metadata = Message()
        self.metadata['Name'] = 'Selected-Library'
        self.metadata['Home-page'] = 'https://example.invalid/project'
        self.metadata['Project-URL'] = 'Source, https://example.invalid/source'
        self.files = [Path('selected-2.0.dist-info/licenses/LICENSE'),
                      Path('selected-2.0.dist-info/METADATA'),
                      Path('../outside.dist-info/LICENSE')]

    def locate_file(self, relative):
        return self.folder / relative


def test_release_notices_replace_old_versions_and_preserve_native_licenses(tmp_path):
    project, program, site = (tmp_path / name for name in ('project', 'program', 'site'))
    project.mkdir()
    licenses = program / 'licenses'
    licenses.mkdir(parents=True)
    (licenses / '组件版本.json').write_text('[{"name":"Selected-Library","version":"1.0"}]', encoding='utf-8')
    (licenses / 'native-notice.txt').write_text('upstream notice', encoding='utf-8')
    (project / '第三方组件说明.txt').write_text('current notices', encoding='utf-8')
    license_source = site / 'selected-2.0.dist-info/licenses/LICENSE'
    license_source.parent.mkdir(parents=True)
    license_source.write_text('new library license', encoding='utf-8')
    outside = tmp_path / 'outside.dist-info/LICENSE'
    outside.parent.mkdir()
    outside.write_text('unselected outside file', encoding='utf-8')
    dist = InstalledDistribution(site)
    with patch('build_cmd.runtime_dependencies', return_value={'selected-library': dist}):
        result = export_runtime_licenses(project, program)
    assert result == json.loads((licenses / '组件版本.json').read_text(encoding='utf-8'))
    assert [(row['name'], row['version']) for row in result] == [('Selected-Library', '2.0')]
    assert (licenses / 'selected-library/selected-2.0.dist-info/licenses/LICENSE').read_text(encoding='utf-8') == 'new library license'
    assert (licenses / 'native-notice.txt').read_text(encoding='utf-8') == 'upstream notice'
    assert not (licenses / 'outside.dist-info').exists()
    assert not (licenses / 'selected-library/outside.dist-info').exists()
    assert 'source_base' not in json.dumps(result)


def test_release_keeps_vendored_calendar_origin(tmp_path):
    project, program = tmp_path / 'project', tmp_path / 'program'
    calendar = project / 'lunar_python'
    calendar.mkdir(parents=True)
    program.mkdir()
    origin = {'name': 'calendar-example', 'version': '1.0', 'upstream': 'https://example.invalid/calendar',
              'license': 'MIT', 'sha256': 'a' * 64}
    (calendar / '来源.json').write_text(json.dumps(origin), encoding='utf-8')
    (calendar / 'LICENSE.txt').write_text('calendar notice', encoding='utf-8')
    with patch('build_cmd.runtime_dependencies', return_value={}):
        inventory = export_runtime_licenses(project, program)
    assert inventory[0]['vendored_unmodified']
    assert inventory[0]['source_sha256'] == origin['sha256']
    assert (program / 'licenses/lunar_python/LICENSE.txt').read_text(encoding='utf-8') == 'calendar notice'
