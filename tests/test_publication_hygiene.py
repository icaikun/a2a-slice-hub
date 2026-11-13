"""Public content checks; private tooling/generated artifacts are deliberately excluded."""
from pathlib import Path
import re
import pytest

FORBIDDEN = [
    re.compile(r"[A-Za-z]:[\\/]" + r"Users[\\/][^\s\"')]+", re.IGNORECASE),
    re.compile(r"/(?:Users|home)/[^/\s\"')]+"),
    re.compile(r"\b192[.]168[.]\d{1,3}[.]\d{1,3}\b"),
    re.compile(r"\b10[.]\d{1,3}[.]\d{1,3}[.]\d{1,3}\b"),
    re.compile(r"\b172[.](?:1[6-9]|2\d|3[01])[.]\d{1,3}[.]\d{1,3}\b"),
    re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-" + r"[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.IGNORECASE),
]


def test_public_files_do_not_embed_private_environment_data():
    root = Path(__file__).resolve().parents[1]
    files = []
    for directory in ('src', 'tests', 'docs', 'examples'):
        files.extend(p for p in (root / directory).rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.md', '.yaml', '.yml', '.json', '.txt', '.toml'})
    files.extend(root / name for name in ('README.md', 'SECURITY.md', 'LICENSE', 'pyproject.toml', 'uv.lock'))
    matches = [p.relative_to(root).as_posix() for p in files
               if any(pattern.search(p.read_text(encoding='utf-8')) for pattern in FORBIDDEN)]
    assert not matches, 'Private environment data detected in: ' + ', '.join(matches)


@pytest.mark.parametrize('prefix', ['github_' + 'pat_', *['gh' + c + '_' for c in 'pousr'],
                                   *['xox' + c + '-' for c in 'baprs'], 'Bearer '])
def test_secret_patterns_have_positive_controls(prefix):
    assert any(pattern.search(prefix + 'a' * 32) for pattern in FORBIDDEN)


@pytest.mark.parametrize('prefix', ['/' + 'Users/', '/' + 'home/', 'C:' + '\\' + 'Users' + '\\'])
def test_home_patterns_have_positive_controls(prefix):
    assert any(pattern.search(prefix + 'private-person/project') for pattern in FORBIDDEN)


@pytest.mark.parametrize('name', ['README.md', 'SECURITY.md', 'docs/state_transitions.md', 'docs/architecture_blueprint.md'])
def test_current_documentation_has_no_stale_implementation_claims(name):
    text = (Path(__file__).resolve().parents[1] / name).read_text(encoding='utf-8')
    assert not any(stale in text for stale in ['Round ' + '1 implementation', 'Round ' + '2 implementation',
                                               'PostgreSQL' + ' + Redis'])
