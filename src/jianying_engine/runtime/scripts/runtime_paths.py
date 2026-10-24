"""Portable runtime resources and external state paths; never reads credentials."""
import os
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(__file__).absolute().parents[1]


def resource_skill_root():
    """Locate the owning Skill for both historical and integrated resource layouts."""
    if RAW_ROOT.name == 'jianying-editor':
        return RAW_ROOT
    if (RAW_ROOT.name == 'jianying' and RAW_ROOT.parent.name == 'backends'
            and RAW_ROOT.parent.parent.name == 'editing-workbench'):
        return RAW_ROOT.parent.parent
    return None


def external_directory(value, label):
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{label} must be an absolute directory')
    resolved = path.resolve()
    protected = [RUNTIME_ROOT.parent]
    skill = resource_skill_root()
    if skill is not None:
        protected.append(skill.parent)
    protected.extend(Path.home() / name / 'skills' for name in ('.codex', '.dsh', '.agents'))
    if os.environ.get('CODEX_HOME'):
        protected.append(Path(os.environ['CODEX_HOME']) / 'skills')
    # Source checkouts can keep thin Skills beside the engine; wheel installs have no such sibling.
    protected.extend(parent / 'skills' for parent in RUNTIME_ROOT.parents if (parent / 'skills').is_dir())
    if any(resolved.is_relative_to(root.resolve()) or path.absolute().is_relative_to(root.absolute())
           for root in protected):
        raise ValueError(f'{label} must be outside the engine package and Skills directories')
    return resolved


def stock_config():
    explicit = os.environ.get('JIANYING_STOCK_CONFIG')
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError('JIANYING_STOCK_CONFIG must name an existing absolute file')
        return path.resolve()
    default = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'JianyingEngine' / 'config.env'
    # A resource-path caller retains its configuration after consolidation as well.
    # __file__ preserves the lexical internal directory when launched through its junction.
    legacy = RAW_ROOT / 'config.env'
    if resource_skill_root() is not None and legacy.is_file():
        return legacy
    return default
