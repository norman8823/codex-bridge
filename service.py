"""User LaunchAgent control. No shell startup files or credentials in the plist."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = 'com.norman.codex-bridge'


def target():
    return f'gui/{os.getuid()}/{LABEL}'


def plist_path():
    return Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def ctl(*args, check=True):
    p = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=45)
    if check and p.returncode:
        raise RuntimeError(f'launchctl {args[0]} failed (exit {p.returncode}); inspect service status')
    return p


def loaded():
    return ctl('print', target(), check=False).returncode == 0


def install(root):
    if sys.platform != 'darwin':
        raise RuntimeError('LaunchAgent installation requires macOS')
    for name in ('gh', 'codex'):
        if not shutil.which(name):
            raise RuntimeError(name + ' missing from PATH; install from your authenticated terminal')
    path = plist_path()
    if path.exists():
        current = plistlib.loads(path.read_bytes())
        if current.get('ProgramArguments', [None, None])[1:2] != [str(root / 'bridge.py')]:
            raise RuntimeError('Existing LaunchAgent belongs to another installation; refusing overwrite')
    if loaded():
        raise RuntimeError('Stop the installed service before reinstalling')
    paths = list(dict.fromkeys([str(Path(sys.executable).parent),
                               str(Path(shutil.which('gh')).parent),
                               str(Path(shutil.which('codex')).parent),
                               '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
    env = {'HOME': str(Path.home()), 'PATH': ':'.join(paths), 'PYTHONUNBUFFERED': '1'}
    for key in ('GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    data = dict(Label=LABEL, ProgramArguments=[sys.executable, str(root / 'bridge.py'), 'listen'],
                WorkingDirectory=str(root), EnvironmentVariables=env, RunAtLoad=True,
                KeepAlive=True, ThrottleInterval=10, ExitTimeOut=180,
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null', Umask=63)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Configuration is small and replaceable; existing bridge state is untouched.
    temp = path.with_suffix('.plist.tmp')
    with temp.open('wb') as out:
        plistlib.dump(data, out)
        out.flush()
        os.fsync(out.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)
    start()
    return path


def start():
    path = plist_path()
    if not path.exists():
        raise RuntimeError('LaunchAgent not installed; run codex-bridge install')
    ctl('enable', target())
    if not loaded():
        ctl('bootstrap', f'gui/{os.getuid()}', str(path))


def stop(root):
    runtime = root / '.runtime'
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    (runtime / 'stop-request').touch(mode=0o600)
    # Persist the explicit stop across logins, then unload. SIGTERM lets the
    # listener finish its current queue/receipt before ExitTimeOut expires.
    if plist_path().exists():
        ctl('disable', target())
        if loaded():
            ctl('bootout', target())


def uninstall(root):
    path = plist_path()
    if path.exists():
        data = plistlib.loads(path.read_bytes())
        if data.get('ProgramArguments', [])[1:2] != [str(root / 'bridge.py')]:
            raise RuntimeError('Refusing to remove another installation')
        stop(root)
        path.unlink()
    print('LaunchAgent removed; routes, configuration and receipts preserved')
