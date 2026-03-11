"""SSH helper for executing commands on remote OpenWRT routers."""

import logging
import subprocess

logger = logging.getLogger(__name__)


class SSHConnection:
    """Execute commands on a remote OpenWRT router via SSH."""

    def __init__(self, host, user='root', port=22):
        self.host = host
        self.user = user
        self.port = port

    def run(self, command, check=True):
        """Run *command* over SSH and return stdout (stripped).

        Raises RuntimeError when *check* is True and the remote command
        exits with a non-zero status.
        """
        logger.debug("SSH %s: %s", self.host, command)
        result = subprocess.run(
            [
                'ssh',
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ConnectTimeout=10',
                '-p', str(self.port),
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
