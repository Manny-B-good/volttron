import ConfigParser as configparser
import json
import os
import shutil
import logging
from gevent.fileobject import FileObject
import gevent.subprocess as subprocess
from gevent.subprocess import Popen
import sys
import time
import tempfile
import unittest

from os.path import dirname
from contextlib import closing
from StringIO import StringIO

import zmq
from zmq.utils import jsonapi

import gevent

from volttron.platform.agent.utils import strip_comments
from volttron.platform.messaging import topics
from volttron.platform.main import start_volttron_process
from volttron.platform.vip.agent import Agent
from volttron.platform.vip.socket import encode_key
from volttron.platform.aip import AIPplatform
#from volttron.platform.control import client, server
from volttron.platform import packaging

_log = logging.getLogger(__name__)

RESTRICTED_AVAILABLE = False

try:
    from volttron.restricted import (auth, certs)
    RESTRICTED_AVAILABLE = True

except ImportError:
    RESTRICTED_AVAILABLE = False
    auth = None
    certs = None

#Filenames for the config files which are created during setup and then
#passed on the command line
TMP_PLATFORM_CONFIG_FILENAME = "config"
TMP_SMAP_CONFIG_FILENAME = "test-smap.ini"

#Used to fill in TWISTED_CONFIG template
TEST_CONFIG_FILE = 'base-platform-test.json'

PLATFORM_CONFIG_UNRESTRICTED = """
no-resource-monitor
no-verify
no-mobility
"""


PLATFORM_CONFIG_RESTRICTED = """
mobility-address = {mobility-address}
control-socket = {tmpdir}/run/control
resource-monitor = {resource-monitor}
"""



TWISTED_CONFIG = """
[report 0]
ReportDeliveryLocation = {smap-uri}/add/{smap-key}

[/]
type = Collection
Metadata/SourceName = {smap-source}
uuid = {smap-uuid}

[/datalogger]
type = volttron.drivers.data_logger.DataLogger
interval = 1

"""

UNRESTRICTED = 0
VERIFY_ONLY = 1
RESOURCE_CHECK_ONLY = 2
RESTRICTED = 3

MODES = (UNRESTRICTED, VERIFY_ONLY, RESOURCE_CHECK_ONLY, RESTRICTED)

VOLTTRON_ROOT = dirname(dirname(dirname(os.path.realpath(__file__))))

if os.environ.get('CI', None) is None:
    VSTART = os.path.join(VOLTTRON_ROOT, "env/bin/volttron")
    VCTRL = os.path.join(VOLTTRON_ROOT, "env/bin/volttron-ctl")
    TWISTED_START = os.path.join(VOLTTRON_ROOT, "env/bin/twistd")
else:
    VSTART ="volttron"
    VCTRL = "volttron-ctl"
    TWISTED_START = "twistd"

SEND_AGENT = "send"

RUN_DIR = 'run'
PUBLISH_TO = RUN_DIR+'/publish'
SUBSCRIBE_TO = RUN_DIR+'/subscribe'

class PlatformWrapperError(Exception):
    pass

class PlatformWrapper:
    def __init__(self):
        '''Initializes a new volttron environment

        Creates a temporary VOLTTRON_HOME directory with a packaged directory for
        agents that are built.
        '''
        self.volttron_home = tempfile.mkdtemp()
        self.packaged_dir = os.path.join(self.volttron_home, "packaged")
        os.makedirs(self.packaged_dir)
        self.env = os.environ.copy()
        self.env['VOLTTRON_HOME'] = self.volttron_home

        self._p_process = None
        self._t_process = None
        self.publickey = self.generate_key()
        self._started_pids = []
        print('Creating Platform Wrapper at: {}'.format(self.volttron_home))

    def build_agent(self, address=None, should_spawn=True, identity=None,
                    publickey=None, secretkey=None, serverkey=None):
        _log.debug('BUILD GENERIC AGENT')
        if address is None:
            address = self.vip_address[0]

        agent = Agent(address=address, identity=identity, publickey=publickey,
                      secretkey=secretkey, serverkey=serverkey)
        print('platformwrapper.build_agent.address: {}'.format(address))

        # Automatically add agent's credentials to auth.json file
        if publickey:
            self._append_allow_curve_key(publickey)

        if should_spawn:
            print('platformwrapper.build_agent spawning')
            event = gevent.event.Event()
            gevent.spawn(agent.core.run, event)#.join(0)
            event.wait(timeout=2)

            hello = agent.vip.hello().get(timeout=.3)
            print('Got hello response {}'.format(hello))

        return agent

    def generate_key(self):
        key = ''.join(zmq.curve_keypair())
        with open(os.path.join(self.volttron_home, 'curve.key'), 'w') as fd:
            fd.write(key)
        return encode_key(key[:40]) # public key

    def _append_allow_curve_key(self, publickey):
        cred = 'CURVE:{}'.format(publickey)
        auth_path = os.path.join(self.volttron_home, 'auth.json')

        try:
            with open(auth_path, 'r') as fd:
                data = strip_comments(FileObject(fd, close=False).read())
                auth = jsonapi.loads(data)
        except IOError:
            auth = {}

        if not 'allow' in auth:
            auth['allow'] = []

        allow = auth['allow']
        if not any(record['credentials'] == cred for record in allow):
            allow.append({'credentials': cred})

        with open(auth_path, 'w+') as fd:
            json.dump(auth, fd)

    def set_auth_dict(self, auth_dict):
        if auth_dict:
            with open(os.path.join(self.volttron_home, 'auth.json'), 'w') as fd:
                fd.write(json.dumps(auth_dict))

    def startup_platform(self, vip_address, auth_dict=None, use_twistd=False,
        mode=UNRESTRICTED, encrypt=False):
        # if not isinstance(vip_address, list):
        #     self.vip_address = [vip_address]
        # else:
        #     self.vip_address = vip_address

        self.vip_address = [vip_address]
        self.mode = mode

        assert self.mode in MODES, 'Invalid platform mode set: '+str(mode)
        opts = None

        # see main.py for how we handle pub sub addresses.
        ipc = 'ipc://{}{}/run/'.format(
            '@' if sys.platform.startswith('linux') else '',
            self.volttron_home)

        if not encrypt:
            # Remove connection encryption
            with open(os.path.join(self.volttron_home, 'curve.key'), 'w'):
                pass

        self.set_auth_dict(auth_dict)

        self.opts = {'verify_agents': False,
                'volttron_home': self.volttron_home,
                'vip_address': vip_address,
                'vip_local_address': ipc + 'vip.socket',
                'publish_address': ipc + 'publish',
                'subscribe_address': ipc + 'subscribe',
                'developer_mode': not encrypt,
                'log': os.path.join(self.volttron_home,'volttron.log'),
                'log_config': None,
                'monitor': True,
                'autostart': True,
                'log_level': logging.DEBUG,
                'verboseness': logging.DEBUG}

        pconfig = os.path.join(self.volttron_home, 'config')
        config = {}

        parser =  configparser.ConfigParser()
        parser.add_section('volttron')
        parser.set('volttron', 'vip-address', vip_address)

        if self.mode == UNRESTRICTED:
            if RESTRICTED_AVAILABLE:
                config['mobility'] = False
                config['resource-monitor'] = False
                config['verify'] = False
            with closing(open(pconfig, 'wb')) as cfg:
                cfg.write(PLATFORM_CONFIG_UNRESTRICTED.format(**config))
                parser.write(cfg)


        elif self.mode == RESTRICTED:
            if not RESTRICTED_AVAILABLE:
                raise ValueError("restricted is not available.")

            certsdir = os.path.join(os.path.expanduser(self.env['VOLTTRON_HOME']),
                                     'certificates')

            print ("certsdir", certsdir)
            self.certsobj = certs.Certs(certsdir)


            with closing(open(pconfig, 'wb')) as cfg:
                cfg.write(PLATFORM_CONFIG_RESTRICTED.format(**config))
            opts = type('Options', (), {'resource-monitor':False,
                                        'verify_agents': True,
                                        'volttron_home': self.volttron_home})()
        else:
            raise PlatformWrapperError("Invalid platform mode specified: {}".format(mode))

        log = os.path.join(self.env['VOLTTRON_HOME'], 'volttron.log')

        cmd = ['volttron', '-vv', '-l{}'.format(log)]
        if self.opts['developer_mode']:
            cmd.append('--developer-mode')

        self._p_process = Popen(cmd, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        assert self._p_process is not None
        # A None value means that the process is still running.
        # A negative means that the process exited with an error.
        assert self._p_process.poll() is None

        #os.environ['VOLTTRON_HOME'] = self.opts['volttron_home']
        #self._p_process = Process(target=start_volttron_process, args=(self.opts,))
        #self._p_process.daemon = True
        #self._p_process.start()

        gevent.sleep()
        self.use_twistd = use_twistd

        #TODO: Revise this to start twistd with platform.
        if self.use_twistd:
            tconfig = os.path.join(self.volttron_home, TMP_SMAP_CONFIG_FILENAME)

            with closing(open(tconfig, 'w')) as cfg:
                cfg.write(TWISTED_CONFIG.format(**config))

            tparams = [TWISTED_START, "-n", "smap", tconfig]
            self._t_process = subprocess.Popen(tparams, env=self.env)
            time.sleep(5)
        #self._t_process = subprocess.Popen(["twistd", "-n", "smap", "test-smap.ini"])

    def is_running(self):
        return self._p_process is not None and self._p_process.poll() is None

    def twistd_is_running(self):
        return self._t_process is not None

    # def publish(self, topic, data):
    #     '''Publish data to a zmq context.
    #
    #     The publisher is goint to use the platform that is contained within
    #     this wrapper to write data to.
    #     '''
    #     if not self.zmq_context:
    #         self.zmq_context = zmq.Context()
    #     print("binding publisher to: ", self.env['AGENT_PUB_ADDR'])
    #     pub = zmq.Socket(self.zmq_context, zmq.PUB)
    #     pub.bind(self.env['AGENT_PUB_ADDR'])
    #     pub.send_multipart([topic, data])

    # def fillout_file(self, filename, template, config_file):
    #
    #     try:
    #         config = json.loads(open(config_file, 'r').read())
    #     except Exception as e:
    #         sys.stderr.write (str(e))
    #         raise PlatformWrapperError("Could not load configuration file for tests")
    #
    #     config['tmpdir'] = self.tmpdir
    #
    #     outfile = os.path.join(self.tmpdir, filename)
    #     with closing(open(outfile, 'w')) as cfg:
    #         cfg.write(template.format(**config))
    #
    #     return outfile

    def direct_sign_agentpackage_creator(self, package):
        assert (RESTRICTED), "Auth not available"
        print ("wrapper.certsobj", self.certsobj.cert_dir)
        assert(auth.sign_as_creator(package, 'creator', certsobj=self.certsobj)), "Signing as {} failed.".format('creator')


    def direct_sign_agentpackage_admin(self, package):
        assert (RESTRICTED), "Auth not available"
        assert(auth.sign_as_admin(package, 'admin', certsobj=self.certsobj)), "Signing as {} failed.".format('admin')


    def direct_sign_agentpackage_initiator(self, package, config_file, contract):
        assert (RESTRICTED), "Auth not available"
        files = {"config_file":config_file,"contract":contract}
        assert(auth.sign_as_initiator(package, 'initiator', files=files,
                                      certsobj=self.certsobj)), "Signing as {} failed.".format('initiator')

    def _aip(self):
        opts = type('Options', (), self.opts)
        aip = AIPplatform(opts)
        aip.setup()
        return aip

    def _install_agent(self, wheel_file, start):
        aip = self._aip()
        auuid = aip.install_agent(wheel_file)
        assert auuid is not None
        if start:
            print('STARTING', wheel_file)
            status = self.start_agent(auuid)
            #aip.start_agent(auuid)
            #status = aip.agent_status(auuid)
            print('STATUS NOW:', status)
            assert status > 0

        return auuid

    def install_agent(self, agent_wheel=None, agent_dir=None, config_file=None,
        start=True):
        '''Install and optionally start an agent on the platform.

            This function allows installation from an agent wheel or an
            agent directory (NOT BOTH).  If an agent_wheel is specified then
            it is assumed to be ready for installation (has a config file).
            If an agent_dir is specified then a config_file file must be
            specified or if it is not specified then it is assumed that the
            file agent_dir/config is to be used as the configuration file.  If
            none of these exist then an assertion error will be thrown.

            This function will return with a uuid of the installed agent.
        '''

        assert self.is_running()
        assert agent_wheel or agent_dir

        if agent_wheel:
            assert not agent_dir
            assert not config_file
            assert os.path.exists(agent_wheel)
            wheel_file = agent_wheel

        if agent_dir:
            assert not agent_wheel
            if not config_file:
                assert os.path.exists(os.path.join(agent_dir, "config"))
                config_file = os.path.join(agent_dir, "config")
            else:
                if isinstance(config_file, dict):
                    from os.path import join, basename
                    temp_config=join(self.volttron_home, basename(agent_dir) + "_config_file")
                    with open(temp_config,"w") as fp:
                        fp.write(json.dumps(config_file))
                    config_file = temp_config
            print('Building agent package')
            wheel_file = self.build_agentpackage(agent_dir, config_file)
            assert wheel_file

        agent_uuid = self._install_agent(wheel_file, start)

        assert agent_uuid is not None

        if start:
            assert self.is_agent_running(agent_uuid)

        return agent_uuid

    def start_agent(self, agent_uuid):
        print('Starting agent {}'.format(agent_uuid))
        cmd = ['volttron-ctl', 'start', agent_uuid]
        p = Popen(cmd, env=self.env)
        p.wait()

        # Confirm agent running
        cmd = ['volttron-ctl', 'status', agent_uuid]
        res = subprocess.check_output(cmd, env=self.env)
        assert 'running' in res
        pidpos = res.index('[') + 1
        pidend = res.index(']')
        pid = int(res[pidpos: pidend])

        self._started_pids.append(pid)
        return int(pid)


    def stop_agent(self, agent_uuid):
        # Confirm agent running
        cmd = ['volttron-ctl', 'stop', agent_uuid]
        res = subprocess.check_output(cmd, env=self.env)
        return self.agent_status(agent_uuid)

    def list_agents(self):
        aip = self._aip()
        return aip.list_agents()

    def remove_agent(self, agent_uuid):
        aip = self._aip()
        uuid = aip.remove_agent(agent_uuid)
        return aip.agent_status(uuid)

    def is_agent_running(self, agent_uuid):
        return self.agent_status(agent_uuid) is not None

    def agent_status(self, agent_uuid):
        # Confirm agent running
        cmd = ['volttron-ctl', 'status', agent_uuid]
        res = subprocess.check_output(cmd, env=self.env)

        try:
            pidpos = res.index('[') + 1
            pidend = res.index(']')
            pid = int(res[pidpos: pidend])
        except:
            pid = None
        return pid

    def build_agentpackage(self, agent_dir, config_file):
        assert os.path.exists(agent_dir)
        assert os.path.exists(config_file)
        wheel_path = packaging.create_package(agent_dir,
                                              self.packaged_dir)
        packaging.add_files_to_package(wheel_path, {
                'config_file':os.path.join('./', config_file)
            })

        return wheel_path

    # def direct_build_agentpackage(self, agent_dir):
    #     print("Building agent_directory ", agent_dir)
    #     wheel_path = packaging.create_package(os.path.join('./', agent_dir),
    #                                           self.packaged_dir)
    #
    #     return wheel_path
    #
    # def direct_send_agent(self, package, target):
    #     pparams = [VCTRL, SEND_AGENT, target, package]
    #     print (pparams, "CWD", os.getcwd())
    #     send_process = subprocess.call(pparams, env=self.env)
    #     print ("Done sending to", target)
    #
    # def direct_configure_agentpackage(self, agent_wheel, config_file):
    #     packaging.add_files_to_package(agent_wheel, {
    #                             'config_file':os.path.join('./', config_file)
    #                         })
    #
    #

#     def direct_build_install_agent(self, agent_dir, config_file):
#         agent_wheel = self.build_agentpackage(agent_dir=agent_dir,
#             config_file=config_file)
#         self.direct_configure_agentpackage(agent_wheel, config_file)
#         assert(agent_wheel is not None,"Agent wheel was not built")
#
#         uuid = self.test_aip.install_agent(agent_wheel)
#         #aip volttron_home, verify_agents
#         return uuid
# #         conn.call.start_agent()



    # def direct_build_install_run_agent(self, agent_dir, config_file):
    #     agent_uuid = self.direct_build_install_agent(agent_dir, config_file)
    #     self.direct_start_agent(agent_uuid)
    #     return agent_uuid
    #
    # def direct_build_send_agent(self, agent_dir, config_file, target):
    #     agent_uuid = self.direct_buid_install_agent(agent_dir, config_file)
    #     self.direct_start_agent(agent_uuid)
    #     return agent_uuid


    def confirm_agent_running(self, agent_name, max_retries=5, timeout_seconds=2):
        running = False
        retries = 0
        while (not running and retries < max_retries):
            status = self.test_aip.status_agents()
            print ("Status", status)
            if len(status) > 0:
                status_name = status[0][1]
                assert status_name == agent_name

                assert len(status[0][2]) == 2, 'Unexpected agent status message'
                status_agent_status = status[0][2][1]
                running = not isinstance(status_agent_status, int)
            retries += 1
            time.sleep(timeout_seconds)
        return running


    # def direct_stop_agent(self, agent_uuid):
    #     result = self.conn.call.stop_agent(agent_uuid)
    #     print result


    def shutdown_platform(self, cleanup=True):
        '''Stop platform here

           This function will shutdown the platform and attempt to kill any
           process that the platformwrapper has started.
        '''
        import signal
        print('shutting down platform: PIDS: {}'.format(self._started_pids))
        while self._started_pids:
            pid = self._started_pids.pop()
            print('ending pid: {}'.format(pid))
            try:
                os.kill(pid,signal.SIGTERM)
            except:
                print('could not kill: {} '.format(pid))
        if self._p_process != None:

            gevent.sleep(0.1)
            self._p_process.terminate()
            gevent.sleep(0.1)
        else:
            print "platform process was null"



        if self.use_twistd and self._t_process != None:
            self._t_process.kill()
            self._t_process.wait()
        elif self.use_twistd:
            print "twistd process was null"
        if cleanup:
            shutil.rmtree(self.volttron_home, ignore_errors=True)


def mergetree(src, dst, symlinks=False, ignore=None):
    if not os.path.exists(dst):
        os.makedirs(dst)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            mergetree(s, d, symlinks, ignore)
        else:
            if not os.path.exists(d) or os.stat(src).st_mtime - os.stat(dst).st_mtime > 1:
                shutil.copy2(s, d)
