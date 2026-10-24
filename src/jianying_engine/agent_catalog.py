"""Local agent discovery. Scan paths only; never install, authenticate, or run a turn."""
import hashlib
import json
import os
from pathlib import Path
import shutil

PROFILES = {
    'codex': ('Codex', 'codex', 'codex', None, []),
    'dsh': ('DeepSeek Harness', 'acp', 'dsh', None, ['--profile', 'acp']),
    'opencode': ('OpenCode', 'acp', 'opencode', 'opencode-ai', ['acp']),
    'gemini': ('Gemini CLI', 'acp', 'gemini', '@google/gemini-cli', ['--acp']),
    'claude-acp': ('Claude Code · ACP 适配器', 'acp', 'claude-agent-acp', '@agentclientprotocol/claude-agent-acp', []),
    'custom-acp': ('自定义 ACP Agent', 'acp', None, None, []),
}


def executable(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('请选择有效的 Agent 可执行文件。')
    found = shutil.which(value) or value
    path = Path(found).expanduser().resolve()
    if not path.is_file() or (os.name == 'nt' and path.suffix.lower() != '.exe'):
        raise ValueError('请选择 .exe；Node Agent 请选 node.exe，并在参数中填入脚本路径。不执行 shell 或批处理。')
    return str(path)


def command_fingerprint(protocol, command):
    return hashlib.sha256(json.dumps([protocol, command], ensure_ascii=False).encode()).hexdigest()


class AgentCatalog:
    def __init__(self, directory):
        self.file = Path(directory) / 'agents.json'
        self.saved, self.selected, self.error = {}, 'codex', None
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding='utf-8'))
                if not isinstance(data.get('profiles'), dict) or data.get('selected', 'codex') not in PROFILES:
                    raise ValueError('Invalid agent preferences')
                self.saved, self.selected = data['profiles'], data.get('selected', 'codex')
            except (ValueError, TypeError, AttributeError):
                self.error = 'Agent 配置无法读取，原文件保留；请修复 agents.json 后重启。'
        self.refresh()

    def discover(self, identity):
        _, protocol, cli, package, arguments = PROFILES[identity]
        if identity == 'dsh':
            base = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DeepSeek Harness'
            binary = base / 'DeepSeek Harness.exe'
            if binary.is_file() and (base / 'resources/app.asar').is_file():
                return [str(binary), '--expose-internals', '--require', str(Path(__file__).with_name('dsh_stdio.cjs')),
                        str(base / 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'), *arguments]
        found = shutil.which(cli) if cli else None
        if found and (os.name != 'nt' or Path(found).suffix.lower() == '.exe'):
            return [str(Path(found).resolve()), *arguments]
        if identity == 'codex':
            candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/OpenAI/Codex/bin/codex.exe'
            return [str(candidate)] if candidate.is_file() else []
        if package:
            roots = [Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingEngine/agents/node_modules',
                     Path(os.environ.get('APPDATA', '')) / 'npm/node_modules']
            if found:
                base = Path(found).parent
                roots.insert(0, base.parent if base.name == '.bin' else base / 'node_modules')
            for root in roots:
                folder = root / package
                # OpenCode's npm package ships a Node launcher that selects its platform binary.
                try:
                    metadata = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
                    binary = metadata.get('bin', {})
                    relative = binary.get(cli) if isinstance(binary, dict) else binary
                    script = (folder / relative).resolve() if isinstance(relative, str) else None
                    node = shutil.which('node')
                    if script and script.is_relative_to(folder.resolve()) and script.is_file():
                        if script.suffix.lower() == '.exe':
                            return [str(script), *arguments]
                        if node:
                            return [str(Path(node).resolve()), str(script), *arguments]
                except (OSError, ValueError, TypeError):
                    continue
        return []

    def refresh(self):
        self.entries = []
        for identity, (name, protocol, _, _, defaults) in PROFILES.items():
            command = self.saved.get(identity, {}).get('command') if isinstance(self.saved.get(identity), dict) else None
            command = command if isinstance(command, list) and command and all(isinstance(x, str) for x in command) else self.discover(identity)
            available = bool(command and Path(command[0]).is_file())
            self.entries.append({'id': identity, 'name': name, 'protocol': protocol, 'available': available,
                                 'command': command or [], 'default_args': defaults,
                                 'status': '已找到，连接后验证' if available else '未找到 CLI，可指定路径'})
        return self.entries

    def resolve(self, identity=None, cli_path=None, args=None):
        if args is not None and (not isinstance(args, list) or any(not isinstance(x, str) for x in args)):
            raise ValueError('启动参数必须是 JSON 文本数组。')
        identity = identity or self.selected
        if identity not in PROFILES:
            raise ValueError('未知 Agent 类型。')
        if self.error:
            raise ValueError(self.error)
        entry = dict(next(e for e in self.entries if e['id'] == identity))
        command = list(entry['command'])
        if cli_path:
            command = [executable(cli_path), *(PROFILES[identity][4] if args is None else args)]
        elif args is not None:
            if not command:
                raise ValueError('请先指定 Agent 可执行文件。')
            command = [command[0], *args]
        if not command:
            raise ValueError(entry['name'] + ' 未安装或未检测到，请先安装或指定可执行文件。')
        if len(command) > 33 or any(not isinstance(x, str) or '\0' in x or len(x) > 8192 for x in command):
            raise ValueError('启动参数必须为最多32项的文本数组。')
        command[0] = executable(command[0])
        if identity == 'codex' and len(command) != 1:
            raise ValueError('Codex 使用 app-server 专用协议，不接受自定义启动参数。')
        environment = {'ELECTRON_RUN_AS_NODE': '1'} if identity == 'dsh' else {}
        entry.update(command=command, environment=environment, fingerprint=command_fingerprint(entry['protocol'], command))
        return entry

    def remember(self, entry):
        if self.error:
            raise ValueError(self.error)
        self.saved[entry['id']] = {'command': entry['command']}
        self.selected = entry['id']
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'selected': self.selected, 'profiles': self.saved}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.file)
        self.refresh()
