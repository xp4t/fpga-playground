"""Probe test prerequisites; production always fails closed without isolation."""
import shutil
import subprocess


def sandbox_available():
    if not shutil.which('bwrap'):
        return False
    result = subprocess.run(['bwrap', '--unshare-all', '--ro-bind', '/', '/', '--', '/bin/true'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    return result.returncode == 0


SANDBOX_AVAILABLE = sandbox_available()
