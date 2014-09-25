from contextlib import contextmanager
import logging
import datetime
import time
from textwrap import dedent
import os
from teuthology.orchestra import run
from teuthology.orchestra.run import CommandFailedError, ConnectionLostError

log = logging.getLogger(__name__)


class CephFSMount(object):
    def __init__(self, test_dir, client_id, client_remote):
        """
        :param test_dir: Global teuthology test dir
        :param client_id: Client ID, the 'foo' in client.foo
        :param client_remote: Remote instance for the host where client will run
        """

        self.test_dir = test_dir
        self.client_id = client_id
        self.client_remote = client_remote

        self.mountpoint = os.path.join(self.test_dir, 'mnt.{id}'.format(id=self.client_id))
        self.test_files = ['a', 'b', 'c']

        self.background_procs = []

    def is_mounted(self):
        raise NotImplementedError()

    def mount(self):
        raise NotImplementedError()

    def umount(self):
        raise NotImplementedError()

    def umount_wait(self, force=False):
        raise NotImplementedError()

    def kill_cleanup(self):
        raise NotImplementedError()

    def kill(self):
        raise NotImplementedError()

    def cleanup(self):
        raise NotImplementedError()

    def wait_until_mounted(self):
        raise NotImplementedError()

    @contextmanager
    def mounted(self):
        """
        A context manager, from an initially unmounted state, to mount
        this, yield, and then unmount and clean up.
        """
        self.mount()
        self.wait_until_mounted()
        try:
            yield
        finally:
            self.umount_wait()

    def create_files(self):
        assert(self.is_mounted())

        for suffix in self.test_files:
            log.info("Creating file {0}".format(suffix))
            self.client_remote.run(args=[
                'sudo', 'touch', os.path.join(self.mountpoint, suffix)
            ])

    def check_files(self):
        assert(self.is_mounted())

        for suffix in self.test_files:
            log.info("Checking file {0}".format(suffix))
            r = self.client_remote.run(args=[
                'sudo', 'ls', os.path.join(self.mountpoint, suffix)
            ], check_status=False)
            if r.exitstatus != 0:
                raise RuntimeError("Expected file {0} not found".format(suffix))

    def create_destroy(self):
        assert(self.is_mounted())

        filename = "{0} {1}".format(datetime.datetime.now(), self.client_id)
        log.debug("Creating test file {0}".format(filename))
        self.client_remote.run(args=[
            'sudo', 'touch', os.path.join(self.mountpoint, filename)
        ])
        log.debug("Deleting test file {0}".format(filename))
        self.client_remote.run(args=[
            'sudo', 'rm', '-f', os.path.join(self.mountpoint, filename)
        ])

    def _run_python(self, pyscript):
        return self.client_remote.run(args=[
            'sudo', 'adjust-ulimits', 'daemon-helper', 'kill', 'python', '-c', pyscript
        ], wait=False, stdin=run.PIPE)

    def run_shell(self, args):
        args = ["cd", self.mountpoint, run.Raw('&&')] + args
        return self.client_remote.run(args=args)

    def open_background(self, basename="background_file"):
        """
        Open a file for writing, then block such that the client
        will hold a capability
        """
        assert(self.is_mounted())

        path = os.path.join(self.mountpoint, basename)

        pyscript = dedent("""
            import time

            f = open("{path}", 'w')
            f.write('content')
            f.flush()
            f.write('content2')
            while True:
                time.sleep(1)
            """).format(path=path)

        rproc = self._run_python(pyscript)
        self.background_procs.append(rproc)
        return rproc

    def wait_for_visible(self, basename="background_file", timeout=30):
        i = 0
        while i < timeout:
            r = self.client_remote.run(args=[
                'sudo', 'ls', os.path.join(self.mountpoint, basename)
            ], check_status=False)
            if r.exitstatus == 0:
                log.debug("File {0} became visible from {1} after {2}s".format(
                    basename, self.client_id, i))
                return
            else:
                time.sleep(1)
                i += 1

        raise RuntimeError("Timed out after {0}s waiting for {1} to become visible from {2}".format(
            i, basename, self.client_id))

    def write_background(self, basename="background_file"):
        """
        Open a file for writing, complete as soon as you can
        :param basename:
        :return:
        """
        assert(self.is_mounted())

        path = os.path.join(self.mountpoint, basename)

        pyscript = dedent("""
            import time

            f = open("{path}", 'w')
            f.write('content')
            f.close()
            """).format(path=path)

        rproc = self._run_python(pyscript)
        self.background_procs.append(rproc)
        return rproc

    def open_n_background(self, fs_path, count):
        """
        Open N files for writing, hold them open in a background process

        :param fs_path: Path relative to CephFS root, e.g. "foo/bar"
        :return: a RemoteProcess
        """
        assert(self.is_mounted())

        abs_path = os.path.join(self.mountpoint, fs_path)

        pyscript = dedent("""
            import sys
            import time
            import os

            n = {count}
            abs_path = "{abs_path}"

            if not os.path.exists(os.path.dirname(abs_path)):
                os.makedirs(os.path.dirname(abs_path))

            handles = []
            for i in range(0, n):
                fname = "{{0}}_{{1}}".format(abs_path, i)
                handles.append(open(fname, 'w'))

            while True:
                time.sleep(1)
            """).format(abs_path=abs_path, count=count)

        rproc = self._run_python(pyscript)
        self.background_procs.append(rproc)
        return rproc

    def teardown(self):
        for p in self.background_procs:
            log.info("Terminating background process")
            if p.stdin:
                p.stdin.close()
                try:
                    p.wait()
                except (CommandFailedError, ConnectionLostError):
                    pass

    def get_global_id(self):
        raise NotImplementedError()
