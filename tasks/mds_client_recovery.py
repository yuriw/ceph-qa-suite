
"""
Teuthology task for exercising CephFS client recovery
"""

import contextlib
import logging
import time
import unittest
from teuthology.orchestra import run

from teuthology.orchestra.run import CommandFailedError, ConnectionLostError
from teuthology.task import interactive

from tasks.cephfs.cephfs_test_case import CephFSTestCase, run_tests
from tasks.cephfs.filesystem import Filesystem
from tasks.cephfs.fuse_mount import FuseMount


log = logging.getLogger(__name__)


# Arbitrary timeouts for operations involving restarting
# an MDS or waiting for it to come up
MDS_RESTART_GRACE = 60


class TestClientRecovery(CephFSTestCase):
    # Environment references
    mount_a = None
    mount_b = None
    mds_session_timeout = None
    mds_reconnect_timeout = None
    ms_max_backoff = None

    def setUp(self):
        self.fs.clear_firewall()
        self.fs.mds_restart()
        self.fs.wait_for_daemons()
        if not self.mount_a.is_mounted():
            self.mount_a.mount()
            self.mount_a.wait_until_mounted()

        if not self.mount_b.is_mounted():
            self.mount_b.mount()
            self.mount_b.wait_until_mounted()

        self.mount_a.run_shell(["sudo", "rm", "-rf", run.Raw("*")])

    def tearDown(self):
        self.fs.clear_firewall()
        self.mount_a.teardown()
        self.mount_b.teardown()

    def test_basic(self):
        # Check that two clients come up healthy and see each others' files
        # =====================================================
        self.mount_a.create_files()
        self.mount_a.check_files()
        self.mount_a.umount_wait()

        self.mount_b.check_files()

        self.mount_a.mount()
        self.mount_a.wait_until_mounted()

        # Check that the admin socket interface is correctly reporting
        # two sessions
        # =====================================================
        ls_data = self._session_list()
        self.assert_session_count(2, ls_data)

        self.assertSetEqual(
            set([l['id'] for l in ls_data]),
            {self.mount_a.get_global_id(), self.mount_b.get_global_id()}
        )

    def test_restart(self):
        # Check that after an MDS restart both clients reconnect and continue
        # to handle I/O
        # =====================================================
        self.fs.mds_stop()
        self.fs.mds_fail()
        self.fs.mds_restart()
        self.fs.wait_for_state('up:active', timeout=MDS_RESTART_GRACE)

        self.mount_a.create_destroy()
        self.mount_b.create_destroy()

    def test_reconnect_timeout(self):
        # Reconnect timeout
        # =================
        # Check that if I stop an MDS and a client goes away, the MDS waits
        # for the reconnect period
        self.fs.mds_stop()
        self.fs.mds_fail()

        mount_a_client_id = self.mount_a.get_global_id()
        self.mount_a.umount_wait(force=True)

        self.fs.mds_restart()

        self.fs.wait_for_state('up:reconnect', reject='up:active', timeout=MDS_RESTART_GRACE)

        ls_data = self._session_list()
        self.assert_session_count(2, ls_data)

        # The session for the dead client should have the 'reconnect' flag set
        self.assertTrue(self.get_session(mount_a_client_id)['reconnecting'])

        # Wait for the reconnect state to clear, this should take the
        # reconnect timeout period.
        in_reconnect_for = self.fs.wait_for_state('up:active', timeout=self.mds_reconnect_timeout * 2)
        # Check that the period we waited to enter active is within a factor
        # of two of the reconnect timeout.
        self.assertGreater(in_reconnect_for, self.mds_reconnect_timeout / 2,
                           "Should have been in reconnect phase for {0} but only took {1}".format(
                               self.mds_reconnect_timeout, in_reconnect_for
                           ))

        self.assert_session_count(1)

        # Check that the client that timed out during reconnect can
        # mount again and do I/O
        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        self.mount_a.create_destroy()

        self.assert_session_count(2)

    def test_reconnect_eviction(self):
        # Eviction during reconnect
        # =========================
        self.fs.mds_stop()
        self.fs.mds_fail()

        mount_a_client_id = self.mount_a.get_global_id()
        self.mount_a.umount_wait(force=True)

        self.fs.mds_restart()

        # Enter reconnect phase
        self.fs.wait_for_state('up:reconnect', reject='up:active', timeout=MDS_RESTART_GRACE)
        self.assert_session_count(2)

        # Evict the stuck client
        self.fs.mds_asok(['session', 'evict', "%s" % mount_a_client_id])
        self.assert_session_count(1)

        # Observe that we proceed to active phase without waiting full reconnect timeout
        evict_til_active = self.fs.wait_for_state('up:active', timeout=MDS_RESTART_GRACE)
        # Once we evict the troublemaker, the reconnect phase should complete
        # in well under the reconnect timeout.
        self.assertLess(evict_til_active, self.mds_reconnect_timeout * 0.5,
                        "reconnect did not complete soon enough after eviction, took {0}".format(
                            evict_til_active
                        ))

        # Bring the client back
        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        self.mount_a.create_destroy()

    def test_stale_caps(self):
        # Capability release from stale session
        # =====================================
        cap_holder = self.mount_a.open_background()

        # Wait for the file to be visible from another client, indicating
        # that mount_a has completed its network ops
        self.mount_b.wait_for_visible()

        # Simulate client death
        self.mount_a.kill()

        try:
            # Now, after mds_session_timeout seconds, the waiter should
            # complete their operation when the MDS marks the holder's
            # session stale.
            cap_waiter = self.mount_b.write_background()
            a = time.time()
            cap_waiter.wait()
            b = time.time()

            # Should have succeeded
            self.assertEqual(cap_waiter.exitstatus, 0)

            cap_waited = b - a
            log.info("cap_waiter waited {0}s".format(cap_waited))
            self.assertTrue(self.mds_session_timeout / 2.0 <= cap_waited <= self.mds_session_timeout * 2.0,
                            "Capability handover took {0}, expected approx {1}".format(
                                cap_waited, self.mds_session_timeout
                            ))

            cap_holder.stdin.close()
            try:
                cap_holder.wait()
            except (CommandFailedError, ConnectionLostError):
                # We killed it (and possibly its node), so it raises an error
                pass
        finally:
            # teardown() doesn't quite handle this case cleanly, so help it out
            self.mount_a.kill_cleanup()

        self.mount_a.mount()
        self.mount_a.wait_until_mounted()

    def test_evicted_caps(self):
        # Eviction while holding a capability
        # ===================================

        # Take out a write capability on a file on client A,
        # and then immediately kill it.
        cap_holder = self.mount_a.open_background()
        mount_a_client_id = self.mount_a.get_global_id()

        # Wait for the file to be visible from another client, indicating
        # that mount_a has completed its network ops
        self.mount_b.wait_for_visible()

        # Simulate client death
        self.mount_a.kill()

        try:
            # The waiter should get stuck waiting for the capability
            # held on the MDS by the now-dead client A
            cap_waiter = self.mount_b.write_background()
            time.sleep(5)
            self.assertFalse(cap_waiter.finished)

            self.fs.mds_asok(['session', 'evict', "%s" % mount_a_client_id])
            # Now, because I evicted the old holder of the capability, it should
            # immediately get handed over to the waiter
            a = time.time()
            cap_waiter.wait()
            b = time.time()
            cap_waited = b - a
            log.info("cap_waiter waited {0}s".format(cap_waited))
            # This is the check that it happened 'now' rather than waiting
            # for the session timeout
            self.assertLess(cap_waited, self.mds_session_timeout / 2.0,
                            "Capability handover took {0}, expected less than {1}".format(
                                cap_waited, self.mds_session_timeout / 2.0
                            ))

            cap_holder.stdin.close()
            try:
                cap_holder.wait()
            except (CommandFailedError, ConnectionLostError):
                # We killed it (and possibly its node), so it raises an error
                pass
        finally:
            self.mount_a.kill_cleanup()

        self.mount_a.mount()
        self.mount_a.wait_until_mounted()

    def test_network_death(self):
        """
        Simulate software freeze or temporary network failure.

        Check that the client blocks I/O during failure, and completes
        I/O after failure.
        """

        # We only need one client
        self.mount_b.umount_wait()

        # Initially our one client session should be visible
        client_id = self.mount_a.get_global_id()
        ls_data = self._session_list()
        self.assert_session_count(1, ls_data)
        self.assertEqual(ls_data[0]['id'], client_id)
        self.assert_session_state(client_id, "open")

        # ...and capable of doing I/O without blocking
        self.mount_a.create_files()

        # ...but if we turn off the network
        self.fs.set_clients_block(True)

        # ...and try and start an I/O
        write_blocked = self.mount_a.write_background()

        # ...then it should block
        self.assertFalse(write_blocked.finished)
        self.assert_session_state(client_id, "open")
        time.sleep(self.mds_session_timeout * 1.5)  # Long enough for MDS to consider session stale
        self.assertFalse(write_blocked.finished)
        self.assert_session_state(client_id, "stale")

        # ...until we re-enable I/O
        self.fs.set_clients_block(False)

        # ...when it should complete promptly
        a = time.time()
        write_blocked.wait()
        b = time.time()
        recovery_time = b - a
        log.info("recovery time: {0}".format(recovery_time))
        self.assertLess(recovery_time, self.ms_max_backoff * 2)
        self.assert_session_state(client_id, "open")


class LogStream(object):
    def __init__(self):
        self.buffer = ""

    def write(self, data):
        self.buffer += data
        if "\n" in self.buffer:
            lines = self.buffer.split("\n")
            for line in lines[:-1]:
                log.info(line)
            self.buffer = lines[-1]

    def flush(self):
        pass


class InteractiveFailureResult(unittest.TextTestResult):
    """
    Specialization that implements interactive-on-error style
    behavior.
    """
    ctx = None

    def addFailure(self, test, err):
        log.error(self._exc_info_to_string(err, test))
        log.error("Failure in test '{0}', going interactive".format(
            self.getDescription(test)
        ))
        interactive.task(ctx=self.ctx, config=None)

    def addError(self, test, err):
        log.error(self._exc_info_to_string(err, test))
        log.error("Error in test '{0}', going interactive".format(
            self.getDescription(test)
        ))
        interactive.task(ctx=self.ctx, config=None)


@contextlib.contextmanager
def task(ctx, config):
    """
    Execute CephFS client recovery test suite.

    Requires:
    - An outer ceph_fuse task with at least two clients
    - That the clients are on a separate host to the MDS
    """
    fs = Filesystem(ctx, config)

    # Pick out the clients we will use from the configuration
    # =======================================================
    if len(ctx.mounts) < 2:
        raise RuntimeError("Need at least two clients")
    mount_a = ctx.mounts.values()[0]
    mount_b = ctx.mounts.values()[1]

    if not isinstance(mount_a, FuseMount) or not isinstance(mount_b, FuseMount):
        # kclient kill() power cycles nodes, so requires clients to each be on
        # their own node
        if mount_a.client_remote.hostname == mount_b.client_remote.hostname:
            raise RuntimeError("kclient clients must be on separate nodes")

    # Check we have at least one remote client for use with network-dependent tests
    # =============================================================================
    if mount_a.client_remote.hostname in fs.get_mds_hostnames():
        raise RuntimeError("Require first client to on separate server from MDSs")

    # Stash references on ctx so that we can easily debug in interactive mode
    # =======================================================================
    ctx.filesystem = fs
    ctx.mount_a = mount_a
    ctx.mount_b = mount_b

    run_tests(ctx, config, TestClientRecovery, {
        "mds_reconnect_timeout": int(fs.mds_asok(
            ['config', 'get', 'mds_reconnect_timeout']
        )['mds_reconnect_timeout']),
        "mds_session_timeout": int(fs.mds_asok(
            ['config', 'get', 'mds_session_timeout']
        )['mds_session_timeout']),
        "ms_max_backoff": int(fs.mds_asok(
            ['config', 'get', 'ms_max_backoff']
        )['ms_max_backoff']),
        "fs": fs,
        "mount_a": mount_a,
        "mount_b": mount_b
    })

    # Continue to any downstream tasks
    # ================================
    yield
