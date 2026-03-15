"""SSH helper for executing commands on remote OpenWRT routers."""

import atexit
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Shared temporary directory for SSH control sockets.
_control_dir = tempfile.mkdtemp(prefix='openwrt-ssh-')
atexit.register(lambda: subprocess.run(
    ['rm', '-rf', _control_dir], capture_output=True,
))


class SSHConnection:
    """Execute commands on a remote OpenWRT router via SSH.

    Uses SSH ControlMaster multiplexing so that only the first
    connection requires authentication; subsequent commands reuse the
    already-authenticated transport.
    """

    def __init__(self, host, user='root', port=22, password=None):
        self.host = host
        self.user = user
        self.port = port
        self.password = password
        self._control_path = os.path.join(
            _control_dir, f'{user}@{host}:{port}',
        )

    # -- internal helpers ---------------------------------------------------

    def _ssh_base(self):
        """Return the common ``ssh`` argument list with multiplexing opts."""
        return [
            'ssh',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'ConnectTimeout=10',
            '-o', f'ControlPath={self._control_path}',
            '-p', str(self.port),
        ]

    def _ensure_master(self):
        """Start a ControlMaster connection if one is not already running."""
        # Check whether a master is already alive.
        probe = subprocess.run(
            self._ssh_base() + ['-O', 'check', f'{self.user}@{self.host}'],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            return  # master already running

        logger.debug("Opening SSH ControlMaster to %s", self.host)
        # -M  = become ControlMaster
        # -N  = no remote command
        # -f  = go to background after authentication
        cmd = self._ssh_base() + [
            '-M', '-N', '-f',
            '-o', 'ControlPersist=600',
            f'{self.user}@{self.host}',
        ]
        env = None
        if self.password:
            cmd = ['sshpass', '-e'] + cmd
            env = {**os.environ, 'SSHPASS': self.password}
        subprocess.run(
            cmd,
            check=True,
            timeout=60,           # allow time for interactive password
            env=env,
        )

    def close(self):
        """Tear down the ControlMaster connection (if any)."""
        subprocess.run(
            self._ssh_base() + ['-O', 'exit', f'{self.user}@{self.host}'],
            capture_output=True, text=True,
        )

    # -- public API ---------------------------------------------------------

    def run(self, command, check=True):
        """Run *command* over SSH and return stdout (stripped).

        Raises RuntimeError when *check* is True and the remote command
        exits with a non-zero status.
        """
        self._ensure_master()
        logger.debug("SSH %s: %s", self.host, command)
        result = subprocess.run(
            self._ssh_base() + [
                f'{self.user}@{self.host}',
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed on {self.host}:\n"
                f"  cmd:    {command}\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def test_connection(self):
        """Return True if the router is reachable over SSH."""
        try:
            self.run('echo ok')
            return True
        except Exception as e:
            logger.warning("Failed to connect to %s: %s", self.host, e)
            return False
