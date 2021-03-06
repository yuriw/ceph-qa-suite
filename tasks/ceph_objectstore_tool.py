"""
ceph_objectstore_tool - Simple test of ceph_objectstore_tool utility
"""
from cStringIO import StringIO
from subprocess import call
import contextlib
import logging
import ceph_manager
from teuthology import misc as teuthology
import time
import os
import string
from teuthology.orchestra import run
import sys
import tempfile
import json
from util.rados import (rados, create_replicated_pool)
# from util.rados import (rados, create_ec_pool,
#                               create_replicated_pool,
#                               create_cache_pool)

log = logging.getLogger(__name__)

# Should get cluster name "ceph" from somewhere
# and normal path from osd_data and osd_journal in conf
FSPATH = "/var/lib/ceph/osd/ceph-{id}"
JPATH = "/var/lib/ceph/osd/ceph-{id}/journal"


def get_pool_id(ctx, name):
    return ctx.manager.raw_cluster_cmd('osd', 'pool', 'stats', name).split()[3]


def cod_setup_local_data(log, ctx, NUM_OBJECTS, DATADIR, REP_NAME, DATALINECOUNT):

    objects = range(1, NUM_OBJECTS + 1)
    for i in objects:
        NAME = REP_NAME + "{num}".format(num=i)
        LOCALNAME=os.path.join(DATADIR, NAME)

        dataline = range(DATALINECOUNT)
        fd = open(LOCALNAME, "w")
        data = "This is the replicated data for " + NAME + "\n"
        for _ in dataline:
            fd.write(data)
        fd.close()


def cod_setup_remote_data(log, ctx, remote, NUM_OBJECTS, DATADIR, REP_NAME, DATALINECOUNT):

    objects = range(1, NUM_OBJECTS + 1)
    for i in objects:
        NAME = REP_NAME + "{num}".format(num=i)
        DDNAME = os.path.join(DATADIR, NAME)

        remote.run(args=['rm', '-f', DDNAME ])

        dataline = range(DATALINECOUNT)
        data = "This is the replicated data for " + NAME + "\n"
        DATA = ""
        for _ in dataline:
            DATA += data
        teuthology.write_file(remote, DDNAME, DATA)


# def rados(ctx, remote, cmd, wait=True, check_status=False):
def cod_setup(log, ctx, remote, NUM_OBJECTS, DATADIR, REP_NAME, DATALINECOUNT, REP_POOL, db):
    ERRORS = 0
    log.info("Creating {objs} objects in replicated pool".format(objs=NUM_OBJECTS))
    nullfd = open(os.devnull, "w")

    objects = range(1, NUM_OBJECTS + 1)
    for i in objects:
        NAME = REP_NAME + "{num}".format(num=i)
        DDNAME = os.path.join(DATADIR, NAME)

        proc = rados(ctx, remote, ['-p', REP_POOL, 'put', NAME, DDNAME], wait=False)
        # proc = remote.run(args=['rados', '-p', REP_POOL, 'put', NAME, DDNAME])
        ret = proc.wait()
        if ret != 0:
            log.critical("Rados put failed with status {ret}".format(ret=r[0].exitstatus))
            sys.exit(1)

        db[NAME] = {}

        keys = range(i)
        db[NAME]["xattr"] = {}
        for k in keys:
            if k == 0:
                continue
            mykey = "key{i}-{k}".format(i=i, k=k)
            myval = "val{i}-{k}".format(i=i, k=k)
            proc = remote.run(args=['rados', '-p', REP_POOL, 'setxattr', NAME, mykey, myval])
            ret = proc.wait()
            if ret != 0:
                log.error("setxattr failed with {ret}".format(ret=ret))
                ERRORS += 1
            db[NAME]["xattr"][mykey] = myval

        # Create omap header in all objects but REPobject1
        if i != 1:
            myhdr = "hdr{i}".format(i=i)
            proc = remote.run(args=['rados', '-p', REP_POOL, 'setomapheader', NAME, myhdr])
            ret = proc.wait()
            if ret != 0:
                log.critical("setomapheader failed with {ret}".format(ret=ret))
                ERRORS += 1
            db[NAME]["omapheader"] = myhdr

        db[NAME]["omap"] = {}
        for k in keys:
            if k == 0:
                continue
            mykey = "okey{i}-{k}".format(i=i, k=k)
            myval = "oval{i}-{k}".format(i=i, k=k)
            proc = remote.run(args=['rados', '-p', REP_POOL, 'setomapval', NAME, mykey, myval])
            ret = proc.wait()
            if ret != 0:
                log.critical("setomapval failed with {ret}".format(ret=ret))
            db[NAME]["omap"][mykey] = myval

    nullfd.close()
    return ERRORS


def get_lines(filename):
    tmpfd = open(filename, "r")
    line = True
    lines = []
    while line:
        line = tmpfd.readline().rstrip('\n')
        if line:
            lines += [line]
    tmpfd.close()
    os.unlink(filename)
    return lines


@contextlib.contextmanager
def task(ctx, config):
    """
    Run ceph_objectstore_tool test

    The config should be as follows::

        ceph_objectstore_tool:
          objects: <number of objects>
    """

    if config is None:
        config = {}
    assert isinstance(config, dict), \
        'ceph_objectstore_tool task only accepts a dict for configuration'
    TEUTHDIR = teuthology.get_testdir(ctx)

    # clients = config['clients']
    # assert len(clients) > 0,
    #    'ceph_objectstore_tool task needs at least 1 client'

    REP_POOL = "rep_pool"
    REP_NAME = "REPobject"
    # EC_POOL = "ec_pool"
    # EC_NAME = "ECobject"
    NUM_OBJECTS = config.get('objects', 10)
    ERRORS = 0
    DATADIR = os.path.join(TEUTHDIR, "data")
    # Put a test dir below the data dir
    # TESTDIR = os.path.join(DATADIR, "test")
    DATALINECOUNT = 10000
    # PROFNAME = "testecprofile"

    log.info('Beginning ceph_objectstore_tool...')
    log.info("objects: {num}".format(num=NUM_OBJECTS))

    log.debug(config)
    log.debug(ctx)
    clients = ctx.cluster.only(teuthology.is_type('client'))
    assert len(clients.remotes) > 0, 'Must specify at least 1 client'
    (cli_remote, _) = clients.remotes.popitem()
    log.debug(cli_remote)

    # clients = dict(teuthology.get_clients(ctx=ctx, roles=config.keys()))
    # client = clients.popitem()
    # log.info(client)
    osds = ctx.cluster.only(teuthology.is_type('osd'))
    log.info("OSDS")
    log.info(osds)
    log.info(osds.remotes)

    first_mon = teuthology.get_first_mon(ctx, config)
    (mon,) = ctx.cluster.only(first_mon).remotes.iterkeys()
    manager = ceph_manager.CephManager(
        mon,
        ctx=ctx,
        config=config,
        logger=log.getChild('ceph_manager'),
        )
    ctx.manager = manager

    # ctx.manager.raw_cluster_cmd('osd', 'pool', 'create', REP_POOL, '12', '12', 'replicated')
    create_replicated_pool(cli_remote, REP_POOL, 12)
    REPID = get_pool_id(ctx, REP_POOL)

    log.debug("repid={num}".format(num=REPID))

    while len(manager.get_osd_status()['up']) != len(manager.get_osd_status()['raw']):
        time.sleep(10)
    while len(manager.get_osd_status()['in']) != len(manager.get_osd_status()['up']):
        time.sleep(10)
    manager.raw_cluster_cmd('osd', 'set', 'noout')
    manager.raw_cluster_cmd('osd', 'set', 'nodown')

    db = {}

    LOCALDIR = tempfile.mkdtemp("cod")

    cod_setup_local_data(log, ctx, NUM_OBJECTS, LOCALDIR, REP_NAME, DATALINECOUNT)
    allremote = []
    allremote.append(cli_remote)
    allremote += osds.remotes.keys()
    allremote = list(set(allremote))
    for remote in allremote:
        cod_setup_remote_data(log, ctx, remote, NUM_OBJECTS, DATADIR, REP_NAME, DATALINECOUNT)

    ERRORS += cod_setup(log, ctx, cli_remote, NUM_OBJECTS, DATADIR, REP_NAME, DATALINECOUNT, REP_POOL, db)

    pgs = {}
    jsontext = manager.raw_cluster_cmd('pg', 'dump_json')
    pgdump = json.loads(jsontext)
    PGS = [str(p["pgid"]) for p in pgdump["pg_stats"] if p["pgid"].find(str(REPID) + ".") == 0]
    for stats in pgdump["pg_stats"]:
        if stats["pgid"] in PGS:
            for osd in stats["acting"]:
                if not pgs.has_key(osd):
                    pgs[osd] = []
                pgs[osd].append(stats["pgid"])


    log.info(pgs)
    log.info(db)

    for osd in manager.get_osd_status()['up']:
        manager.kill_osd(osd)
    time.sleep(5)

    pgswithobjects = set()
    objsinpg = {}

    # Test --op list and generate json for all objects
    log.info("Test --op list by generating json for all objects")
    prefix = "sudo ceph_objectstore_tool --data-path {fpath} --journal-path {jpath} ".format(fpath=FSPATH, jpath=JPATH)
    for remote in osds.remotes.iterkeys():
        log.debug(remote)
        log.debug(osds.remotes[remote])
        for role in osds.remotes[remote]:
            if string.find(role, "osd.") != 0:
                continue
            osdid = int(role.split('.')[1])
            log.info("process osd.{id} on {remote}".format(id=osdid, remote=remote))
            for pg in pgs[osdid]:
                cmd = (prefix + "--op list --pgid {pg}").format(id=osdid, pg=pg)
                proc = remote.run(args=cmd.split(), check_status=False, stdout=StringIO())
                # proc.wait()
                if proc.exitstatus != 0:
                    log.error("Bad exit status {ret} from --op list request".format(ret=proc.exitstatus))
                    ERRORS += 1
                else:
                    data = proc.stdout.getvalue()
                    if len(data):
                        # This pg has some objects in it
                        pgswithobjects.add(pg)
                        pglines = data.split('\n')
                        # All copies of a pg are the same so we can overwrite
                        objsinpg[pg] = []
                        while(len(pglines)):
                            # Drop any blank lines
                            if (len(pglines[-1]) == 0):
                                pglines.pop()
                                continue
                            objjson = pglines.pop()
                            name = json.loads(objjson)['oid']
                            objsinpg[pg].append(name)
                            db[name]["pgid"] = pg
                            db[name]["json"] = objjson

    log.info(db)
    log.info(pgswithobjects)
    log.info(objsinpg)

    # Test get-bytes
    log.info("Test get-bytes and set-bytes")
    for basename in db.keys():
        file = os.path.join(DATADIR, basename)
        JSON = db[basename]["json"]
        GETNAME = os.path.join(DATADIR, "get")
        SETNAME = os.path.join(DATADIR, "set")

        for remote in osds.remotes.iterkeys():
            for role in osds.remotes[remote]:
                if string.find(role, "osd.") != 0:
                    continue
                osdid = int(role.split('.')[1])

                pg = db[basename]['pgid']
                if pg in pgs[osdid]:
                    cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                    cmd.append(run.Raw("'{json}'".format(json=JSON)))
                    cmd += "get-bytes {fname}".format(fname=GETNAME).split()
                    proc = remote.run(args=cmd, check_status=False)
                    if proc.exitstatus != 0:
                        remote.run(args="rm -f {getfile}".format(getfile=GETNAME).split())
                        log.error("Bad exit status {ret}".format(ret=proc.exitstatus))
                        ERRORS += 1
                        continue
                    cmd = "diff -q {file} {getfile}".format(file=file, getfile=GETNAME)
                    proc = remote.run(args=cmd.split())
                    if proc.exitstatus != 0:
                        log.error("Data from get-bytes differ")
                        # log.debug("Got:")
                        # cat_file(logging.DEBUG, GETNAME)
                        # log.debug("Expected:")
                        # cat_file(logging.DEBUG, file)
                        ERRORS += 1
                    remote.run(args="rm -f {getfile}".format(getfile=GETNAME).split())

                    data = "put-bytes going into {file}\n".format(file=file)
                    teuthology.write_file(remote, SETNAME, data)
                    cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                    cmd.append(run.Raw("'{json}'".format(json=JSON)))
                    cmd += "set-bytes {fname}".format(fname=SETNAME).split()
                    proc = remote.run(args=cmd, check_status=False)
                    proc.wait()
                    if proc.exitstatus != 0:
                        log.info("set-bytes failed for object {obj} in pg {pg} osd.{id} ret={ret}".format(obj=basename, pg=pg, id=osdid, ret=proc.exitstatus))
                        ERRORS += 1

                    cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                    cmd.append(run.Raw("'{json}'".format(json=JSON)))
                    cmd += "get-bytes -".split()
                    proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                    proc.wait()
                    if proc.exitstatus != 0:
                        log.error("get-bytes after set-bytes ret={ret}".format(ret=proc.exitstatus))
                        ERRORS += 1
                    else:
                        if data != proc.stdout.getvalue():
                            log.error("Data inconsistent after set-bytes, got:")
                            log.error(proc.stdout.getvalue())
                            ERRORS += 1

                    cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                    cmd.append(run.Raw("'{json}'".format(json=JSON)))
                    cmd += "set-bytes {fname}".format(fname=file).split()
                    proc = remote.run(args=cmd, check_status=False)
                    proc.wait()
                    if proc.exitstatus != 0:
                        log.info("set-bytes failed for object {obj} in pg {pg} osd.{id} ret={ret}".format(obj=basename, pg=pg, id=osdid, ret=proc.exitstatus))
                        ERRORS += 1

    log.info("Test list-attrs get-attr")
    for basename in db.keys():
        file = os.path.join(DATADIR, basename)
        JSON = db[basename]["json"]
        GETNAME = os.path.join(DATADIR, "get")
        SETNAME = os.path.join(DATADIR, "set")

        for remote in osds.remotes.iterkeys():
            for role in osds.remotes[remote]:
                if string.find(role, "osd.") != 0:
                    continue
                osdid = int(role.split('.')[1])

                pg = db[basename]['pgid']
                if pg in pgs[osdid]:
                    cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                    cmd.append(run.Raw("'{json}'".format(json=JSON)))
                    cmd += ["list-attrs"]
                    proc = remote.run(args=cmd, check_status=False, stdout=StringIO(), stderr=StringIO())
                    proc.wait()
                    if proc.exitstatus != 0:
                        log.error("Bad exit status {ret}".format(ret=proc.exitstatus))
                        ERRORS += 1
                        continue
                    keys = proc.stdout.getvalue().split()
                    values = dict(db[basename]["xattr"])

                    for key in keys:
                        if key == "_" or key == "snapset":
                            continue
                        key = key.strip("_")
                        if key not in values:
                            log.error("The key {key} should be present".format(key=key))
                            ERRORS += 1
                            continue
                        exp = values.pop(key)
                        cmd = (prefix + "--pgid {pg}").format(id=osdid, pg=pg).split()
                        cmd.append(run.Raw("'{json}'".format(json=JSON)))
                        cmd += "get-attr {key}".format(key="_" + key).split()
                        proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                        proc.wait()
                        if proc.exitstatus != 0:
                            log.error("get-attr failed with {ret}".format(ret=proc.exitstatus))
                            ERRORS += 1
                            continue
                        val = proc.stdout.getvalue()
                        if exp != val:
                            log.error("For key {key} got value {got} instead of {expected}".format(key=key, got=val, expected=exp))
                            ERRORS += 1
                    if len(values) != 0:
                        log.error("Not all keys found, remaining keys:")
                        log.error(values)

    log.info("Test pg info")
    for remote in osds.remotes.iterkeys():
        for role in osds.remotes[remote]:
            if string.find(role, "osd.") != 0:
                continue
            osdid = int(role.split('.')[1])

            for pg in pgs[osdid]:
                cmd = (prefix + "--op info --pgid {pg}").format(id=osdid, pg=pg).split()
                proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                proc.wait()
                if proc.exitstatus != 0:
                    log.error("Failure of --op info command with {ret}".format(proc.exitstatus))
                    ERRORS += 1
                    continue
                info = proc.stdout.getvalue()
                if not str(pg) in info:
                    log.error("Bad data from info: {info}".format(info=info))
                    ERRORS += 1

    log.info("Test pg logging")
    for remote in osds.remotes.iterkeys():
        for role in osds.remotes[remote]:
            if string.find(role, "osd.") != 0:
                continue
            osdid = int(role.split('.')[1])

            for pg in pgs[osdid]:
                cmd = (prefix + "--op log --pgid {pg}").format(id=osdid, pg=pg).split()
                proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                proc.wait()
                if proc.exitstatus != 0:
                    log.error("Getting log failed for pg {pg} from osd.{id} with {ret}".format(pg=pg, id=osdid, ret=proc.exitstatus))
                    ERRORS += 1
                    continue
                HASOBJ = pg in pgswithobjects
                MODOBJ = "modify" in proc.stdout.getvalue()
                if HASOBJ != MODOBJ:
                    log.error("Bad log for pg {pg} from osd.{id}".format(pg=pg, id=osdid))
                    MSG = (HASOBJ and [""] or ["NOT "])[0]
                    log.error("Log should {msg}have a modify entry".format(msg=MSG))
                    ERRORS += 1

    log.info("Test pg export")
    EXP_ERRORS = 0
    for remote in osds.remotes.iterkeys():
        for role in osds.remotes[remote]:
            if string.find(role, "osd.") != 0:
                continue
            osdid = int(role.split('.')[1])

            for pg in pgs[osdid]:
                fpath = os.path.join(DATADIR, "osd{id}.{pg}".format(id=osdid, pg=pg))

                cmd = (prefix + "--op export --pgid {pg} --file {file}").format(id=osdid, pg=pg, file=fpath)
                proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                proc.wait()
                if proc.exitstatus != 0:
                    log.error("Exporting failed for pg {pg} on osd.{id} with {ret}".format(pg=pg, id=osdid, ret=proc.exitstatus))
                    EXP_ERRORS += 1

    ERRORS += EXP_ERRORS

    log.info("Test pg removal")
    RM_ERRORS = 0
    for remote in osds.remotes.iterkeys():
        for role in osds.remotes[remote]:
            if string.find(role, "osd.") != 0:
                continue
            osdid = int(role.split('.')[1])

            for pg in pgs[osdid]:
                cmd = (prefix + "--op remove --pgid {pg}").format(pg=pg, id=osdid)
                proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                proc.wait()
                if proc.exitstatus != 0:
                    log.error("Removing failed for pg {pg} on osd.{id} with {ret}".format(pg=pg, id=osdid, ret=proc.exitstatus))
                    RM_ERRORS += 1

    ERRORS += RM_ERRORS

    IMP_ERRORS = 0
    if EXP_ERRORS == 0 and RM_ERRORS == 0:
        log.info("Test pg import")

        for remote in osds.remotes.iterkeys():
            for role in osds.remotes[remote]:
                if string.find(role, "osd.") != 0:
                    continue
                osdid = int(role.split('.')[1])

                for pg in pgs[osdid]:
                    fpath = os.path.join(DATADIR, "osd{id}.{pg}".format(id=osdid, pg=pg))

                    cmd = (prefix + "--op import --file {file}").format(id=osdid, file=fpath)
                    proc = remote.run(args=cmd, check_status=False, stdout=StringIO())
                    proc.wait()
                    if proc.exitstatus != 0:
                        log.error("Import failed from {file} with {ret}".format(file=fpath, ret=proc.exitstatus))
                        IMP_ERRORS += 1
    else:
        log.warning("SKIPPING IMPORT TESTS DUE TO PREVIOUS FAILURES")

    ERRORS += IMP_ERRORS

    if EXP_ERRORS == 0 and RM_ERRORS == 0 and IMP_ERRORS == 0:
        log.info("Restarting OSDs....")
        # They are still look to be up because of setting nodown
        for osd in manager.get_osd_status()['up']:
            manager.revive_osd(osd)
        # Wait for health?
        time.sleep(5)
        # Let scrub after test runs verify consistency of all copies
        log.info("Verify replicated import data")
        objects = range(1, NUM_OBJECTS + 1)
        for i in objects:
            NAME = REP_NAME + "{num}".format(num=i)
            TESTNAME = os.path.join(DATADIR, "gettest")
            REFNAME = os.path.join(DATADIR, NAME)

            proc = rados(ctx, cli_remote, ['-p', REP_POOL, 'get', NAME, TESTNAME], wait=False)

            ret = proc.wait()
            if ret != 0:
               log.errors("After import, rados get failed with {ret}".format(ret=r[0].exitstatus))
               ERRORS += 1
               continue

            cmd = "diff -q {gettest} {ref}".format(gettest=TESTNAME, ref=REFNAME)
            proc = cli_remote.run(args=cmd, check_status=False)
            proc.wait()
            if proc.exitstatus != 0:
                log.error("Data comparison failed for {obj}".format(obj=NAME))
                ERRORS += 1

    if ERRORS == 0:
        log.info("TEST PASSED")
    else:
        log.error("TEST FAILED WITH {errcount} ERRORS".format(errcount=ERRORS))

    try:
        yield
    finally:
        log.info('Ending ceph_objectstore_tool')
