import os
import socket
import paramiko
import contextlib
import subprocess
from rrmngmnt.executor import Executor

SSH_DIR_PATH = os.path.expanduser("~/.ssh")
AUTHORIZED_KEYS = os.path.join(SSH_DIR_PATH, "authorized_keys")
KNOWN_HOSTS = os.path.join(SSH_DIR_PATH, "known_hosts")
ID_RSA_PUB = os.path.join(SSH_DIR_PATH, "id_rsa.pub")
ID_RSA_PRV = os.path.join(SSH_DIR_PATH, "id_rsa")


class RemoteExecutor(Executor):
    """
    Any resource which provides SSH service.

    This class is meant to replace our current utilities.machine.LinuxMachine
    classs. This allows you to lower access to communicate with ssh.
    Like a live interaction, getting rid of True/False results, and
    mixing stdout with stderr.

    You can still use use 'run_cmd' method if you don't care.
    But I would recommed you to work like this:
    """

    TCP_TIMEOUT = 10.0

    class LoggerAdapter(Executor.LoggerAdapter):
        """
        Makes sure that all logs which are done via this class, has
        appropriate prefix. [user@IP/password]
        """
        def process(self, msg, kwargs):
            return (
                "[%s@%s/%s] %s" % (
                    self.extra['self'].user.name,
                    self.extra['self'].address,
                    self.extra['self'].user.password,
                    msg,
                ),
                kwargs,
            )

    class Session(Executor.Session):
        """
        Represents active ssh connection
        """
        def __init__(self, executor, timeout=None, use_pkey=False):
            super(RemoteExecutor.Session, self).__init__(executor)
            if timeout is None:
                timeout = RemoteExecutor.TCP_TIMEOUT
            self._timeout = timeout
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if use_pkey:
                self.pkey = paramiko.RSAKey.from_private_key_file(
                    ID_RSA_PRV
                )
                self._executor.user.password = None
            else:
                self.pkey = None

        def __exit__(self, type_, value, tb):
            if type_ is socket.timeout:
                self._update_timeout_exception(value)
            try:
                self.close()
            except Exception as ex:
                if type_ is None:
                    raise
                else:
                    self._executor.logger.debug(
                        "Can not close ssh session %s", ex,
                    )

        def open(self):
            self._ssh.get_host_keys().clear()
            try:
                self._ssh.connect(
                    self._executor.address,
                    username=self._executor.user.name,
                    password=self._executor.user.password,
                    timeout=self._timeout,
                    pkey=self.pkey
                )
            except (socket.gaierror, socket.herror) as ex:
                args = list(ex.args)
                message = "%s: %s" % (self._executor.address, args[1])
                args[1] = message
                ex.strerror = message
                ex.args = tuple(args)
                raise
            except socket.timeout as ex:
                self._update_timeout_exception(ex)
                raise

        def close(self):
            self._ssh.close()

        def _update_timeout_exception(self, ex, timeout=None):
            if getattr(ex, '_updated', False):
                return
            if timeout is None:
                timeout = self._timeout
            message = "%s: timeout(%s)" % (
                self._executor.address, timeout
            )
            ex.args = (message,)
            ex._updated = True

        def command(self, cmd):
            return RemoteExecutor.Command(cmd, self)

        def run_cmd(self, cmd, input_=None, timeout=None):
            cmd = self.command(cmd)
            return cmd.run(input_, timeout)

        @contextlib.contextmanager
        def open_file(self, path, mode='r', bufsize=-1):
            with contextlib.closing(self._ssh.open_sftp()) as sftp:
                with contextlib.closing(
                    sftp.file(
                        path,
                        mode,
                        bufsize,
                    )
                ) as fh:
                    yield fh

    class Command(Executor.Command):
        """
        This class holds all data related to command execution.
         - the command itself
         - stdout/stderr streams
         - out/err string which were produced by command
         - returncode the exit status of command
        """
        def __init__(self, cmd, session):
            super(RemoteExecutor.Command, self).__init__(
                subprocess.list2cmdline(cmd),
                session,
            )
            self._in = None
            self._out = None
            self._err = None

        def get_rc(self, wait=False):
            if self._rc is None:
                if self._out is not None:
                    if self._out.channel.exit_status_ready() or wait:
                        self._rc = self._out.channel.recv_exit_status()
            return self._rc

        @contextlib.contextmanager
        def execute(self, bufsize=-1, timeout=None, get_pty=False):
            """
            This method allows you to work directly with streams.

            with cmd.execute() as in_, out, err:
                # where in_, out and err are file-like objects
                # where you can read data from these
            """
            try:
                self.logger.debug("Executing: %s", self.cmd)
                self._in, self._out, self._err = self._ss._ssh.exec_command(
                    self.cmd,
                    bufsize=bufsize,
                    timeout=timeout,
                    get_pty=get_pty,
                )
                yield self._in, self._out, self._err
                self.get_rc(True)
            except socket.timeout as ex:
                self._ss._update_timeout_exception(ex, timeout)
                raise
            finally:
                if self._in is not None:
                    self._in.close()
                if self._out is not None:
                    self._out.close()
                if self._err is not None:
                    self._err.close()
                self.logger.debug("Results of command: %s", self.cmd)
                self.logger.debug("  OUT: %s", self.out)
                self.logger.debug("  ERR: %s", self.err)
                self.logger.debug("  RC: %s", self.rc)

        def run(self, input_, timeout=None, get_pty=False):
            with self.execute(
                timeout=timeout, get_pty=get_pty
            ) as (in_, out, err):
                if input_:
                    in_.write(input_)
                    in_.close()
                self.out = out.read()
                self.err = err.read()
            return self.rc, self.out, self.err

    def __init__(self, user, address, use_pkey=False):
        """
        :param user: user
        :type user: instance of User
        :param address: ip / hostname
        :type address: str
        :param use_pkey: use ssh private key in the connection
        :type use_pkey: bool
        """
        super(RemoteExecutor, self).__init__(user)
        self.address = address
        self.use_pkey = use_pkey

    def session(self, timeout=None):
        """
        :param timeout: tcp timeout
        :type timeout: float
        :return: the session
        :rtype: instance of RemoteExecutor.Session
        """
        return RemoteExecutor.Session(self, timeout, self.use_pkey)

    def run_cmd(self, cmd, input_=None, tcp_timeout=None, io_timeout=None):
        """
        :param cmd: command
        :type cmd: list
        :param input_: input data
        :type input_: str
        :param tcp_timeout: tcp timeout
        :type tcp_timeout: float
        :param io_timeout: timeout for data operation (read/write)
        :type io_timeout: float
        :return: rc, out, err
        :rtype: tuple (int, str, str)
        """
        with self.session(tcp_timeout) as session:
            return session.run_cmd(cmd, input_, io_timeout)
