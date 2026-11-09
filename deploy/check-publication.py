"""Fail closed on common credential/artifact leaks; never print matched values."""
import re
import subprocess
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root).decode().split('\0')
patterns = {
    'OpenAI key': re.compile(r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}'),
    'GitHub token': re.compile(r'gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'AWS key': re.compile(r'AKIA[A-Z0-9]{16}'),
    'JWT literal': re.compile(r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'),
}
failures = []
def inspect(name, data):
    text = data.decode('utf8', errors='ignore')
    for label, pattern in patterns.items():
        for match in pattern.finditer(text):
            if 'synthetic' not in match.group().lower():
                failures.append((name, label))
                break

for name in sorted(set(files)):
    if not name: continue
    path = root / name
    if not path.is_file(): continue
    parts = set(path.relative_to(root).parts)
    if parts.intersection({'.local-preview', 'outputs', 'output', 'node_modules', '.venv', 'dist-local', 'secrets'}):
        failures.append((name, 'private/runtime directory'))
    if path.name.startswith('.env') and path.name != '.env.example':
        failures.append((name, 'private environment file'))
    if path.suffix in {'.sqlite', '.db', '.key'} or '.sqlite-' in path.name:
        failures.append((name, 'database/private key file'))
    inspect(name, path.read_bytes())
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.endswith('/'): continue
                if member.startswith('.env') or any(part in {'secrets', '.local-preview'} for part in member.split('/')):
                    failures.append((name + ':' + member, 'private archive member'))
                inspect(name + ':' + member, archive.read(member))
for name, label in sorted(set(failures)):
    print(f'{label}: {name}')
if failures: raise SystemExit(1)
print('Publication scan PASS: no matching raw credentials, local DBs or private runtime artifacts.')
