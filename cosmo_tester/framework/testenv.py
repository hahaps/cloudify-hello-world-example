########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.


import unittest
import logging
import sys
import shutil
import tempfile
import time
import copy
import os
import importlib
import json
from StringIO import StringIO

import yaml
from fabric import api as fabric_api
from path import path

from cloudify_rest_client import CloudifyClient

from cosmo_tester.framework.cfy_helper import (CfyHelper,
                                               DEFAULT_EXECUTE_TIMEOUT)
from cosmo_tester.framework.util import (get_blueprint_path,
                                         get_actual_keypath,
                                         process_variables,
                                         YamlPatcher)

root = logging.getLogger()
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter(fmt='%(asctime)s [%(levelname)s] '
                                  '[%(name)s] %(message)s',
                              datefmt='%H:%M:%S')
ch.setFormatter(formatter)

# clear all other handlers
for logging_handler in root.handlers:
    root.removeHandler(logging_handler)

root.addHandler(ch)
logger = logging.getLogger('TESTENV')
logger.setLevel(logging.DEBUG)

HANDLER_CONFIGURATION = 'HANDLER_CONFIGURATION'
SUITES_YAML_PATH = 'SUITES_YAML_PATH'

test_environment = None


def initialize_without_bootstrap():
    global test_environment
    if not test_environment:
        test_environment = TestEnvironment()


def clear_environment():
    global test_environment
    test_environment = None


def bootstrap():
    global test_environment
    if not test_environment:
        test_environment = TestEnvironment()
        test_environment.bootstrap()


def teardown():
    global test_environment
    if test_environment:
        try:
            test_environment.teardown()
        finally:
            clear_environment()


# Singleton class
class TestEnvironment(object):
    # Singleton class
    def __init__(self):
        self._initial_cwd = os.getcwd()
        self._global_cleanup_context = None
        self._management_running = False
        self.rest_client = None
        self.management_ip = None
        self.handler = None
        self._manager_blueprint_path = None
        self._workdir = tempfile.mkdtemp(prefix='cloudify-testenv-')

        if HANDLER_CONFIGURATION not in os.environ:
            raise RuntimeError('handler configuration name must be configured '
                               'in "HANDLER_CONFIGURATION" env variable')
        handler_configuration_name = os.environ[HANDLER_CONFIGURATION]
        suites_yaml_path = os.environ.get(
            SUITES_YAML_PATH,
            path(__file__).dirname().dirname().dirname() / 'suites' / 'suites'
                                                         / 'suites.yaml')
        with open(suites_yaml_path) as f:
            self.suites_yaml = yaml.load(f.read())
        self.handler_configuration = self.suites_yaml[
            'handler_configurations'][handler_configuration_name]

        self.cloudify_config_path = path(os.path.expanduser(
            self.handler_configuration['inputs']))

        if not self.cloudify_config_path.isfile():
            raise RuntimeError('config file configured in handler '
                               'configuration does not seem to exist: {0}'
                               .format(self.cloudify_config_path))

        self.is_provider_bootstrap = self.handler_configuration.get(
            'bootstrap_using_providers', False)

        if not self.is_provider_bootstrap and not (
                'manager_blueprints_dir' in self.handler_configuration and
                'manager_blueprint' in self.handler_configuration):
            raise RuntimeError(
                'manager blueprint and manager blueprints dir must be '
                'configured in handler configuration env variable in '
                'order to run non-provider bootstraps')

        if not self.is_provider_bootstrap:
            manager_blueprints_base_dir = os.path.expanduser(
                self.handler_configuration['manager_blueprints_dir'])
            manager_blueprint = self.handler_configuration['manager_blueprint']
            self._manager_blueprint_path = \
                os.path.join(manager_blueprints_base_dir, manager_blueprint)

        # make a temp config files than can be modified freely
        self._generate_unique_configurations()

        if not self.is_provider_bootstrap:
            with YamlPatcher(self._manager_blueprint_path) as patch:
                manager_blueprint_override = process_variables(
                    self.suites_yaml,
                    self.handler_configuration.get(
                        'manager_blueprint_override', {}))
                for key, value in manager_blueprint_override.items():
                    patch.set_value(key, value)

        handler = self.handler_configuration['handler']
        if 'external' in self.handler_configuration:
            module_path = 'system_tests.{0}'.format(handler)
        else:
            module_path = 'suites.helpers.handlers.{0}.handler'.format(handler)
        handler_module = importlib.import_module(module_path)
        handler_class = getattr(handler_module, 'handler')
        self.handler = handler_class(self)

        if 'manager_ip' in self.handler_configuration:
            self._running_env_setup(self.handler_configuration['manager_ip'])

        self.cloudify_config = yaml.load(self.cloudify_config_path.text())
        self._config_reader = self.handler.CloudifyConfigReader(
            self.cloudify_config,
            manager_blueprint_path=self._manager_blueprint_path)
        with self.handler.update_cloudify_config() as patch:
            processed_inputs = process_variables(
                self.suites_yaml,
                self.handler_configuration.get('inputs_override', {}))
            for key, value in processed_inputs.items():
                patch.set_value(key, value)

        global test_environment
        test_environment = self

    def _generate_unique_configurations(self):
        inputs_path = os.path.join(self._workdir, 'inputs.yaml')
        shutil.copy(self.cloudify_config_path, inputs_path)
        self.cloudify_config_path = path(inputs_path)
        if not self.is_provider_bootstrap:
            manager_blueprint_base = os.path.basename(
                self._manager_blueprint_path)
            source_manager_blueprint_dir = os.path.dirname(
                self._manager_blueprint_path)
            target_manager_blueprint_dir = os.path.join(self._workdir,
                                                        'manager-blueprint')
            shutil.copytree(source_manager_blueprint_dir,
                            target_manager_blueprint_dir)
            self._manager_blueprint_path = path(
                os.path.join(target_manager_blueprint_dir,
                             manager_blueprint_base))

    def setup(self):
        os.chdir(self._initial_cwd)
        return self

    def bootstrap(self):
        if self._management_running:
            return

        self._global_cleanup_context = self.handler.CleanupContext(
            'testenv', self)

        cfy = CfyHelper(cfy_workdir=self._workdir)

        self.handler.before_bootstrap()
        if self.is_provider_bootstrap:
            cfy.bootstrap_with_providers(
                self.cloudify_config_path,
                self.handler.provider,
                keep_up_on_failure=False,
                verbose=True,
                dev_mode=False)
        else:

            install_plugins = self.handler_configuration.get(
                'install_manager_blueprint_dependencies', True)

            cfy.bootstrap(
                self._manager_blueprint_path,
                inputs_file=self.cloudify_config_path,
                install_plugins=install_plugins,
                keep_up_on_failure=False,
                verbose=True)
        self._running_env_setup(cfy.get_management_ip())
        self.handler.after_bootstrap(cfy.get_provider_context())

    def teardown(self):
        if self._global_cleanup_context is None:
            return
        self.setup()
        cfy = CfyHelper(cfy_workdir=self._workdir)
        try:
            cfy.use(self.management_ip, provider=self.is_provider_bootstrap)
            if self.is_provider_bootstrap:
                cfy.teardown_with_providers(
                    self.cloudify_config_path,
                    verbose=True)
            else:
                cfy.teardown(verbose=True)
        finally:
            self._global_cleanup_context.cleanup()
            self.handler.after_teardown()
            if os.path.exists(self._workdir):
                shutil.rmtree(self._workdir)

    def _running_env_setup(self, management_ip):
        self.management_ip = management_ip
        self.rest_client = CloudifyClient(self.management_ip)
        response = self.rest_client.manager.get_status()
        if not response['status'] == 'running':
            raise RuntimeError('Manager at {0} is not running.'
                               .format(self.management_ip))
        self._management_running = True

    # Will return provider specific handler/config properties if not found in
    # test env.
    def __getattr__(self, item):
        if hasattr(self.handler, item):
            return getattr(self.handler, item)
        elif hasattr(self._config_reader, item):
            return getattr(self._config_reader, item)
        else:
            raise AttributeError(
                'Property \'{0}\' was not found in env'.format(item))


class TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        global test_environment
        self.env = test_environment.setup()
        self.logger = logging.getLogger(self._testMethodName)
        self.logger.setLevel(logging.INFO)
        self.workdir = tempfile.mkdtemp(prefix='cosmo-test-')
        self.cfy = CfyHelper(cfy_workdir=self.workdir,
                             management_ip=self.env.management_ip)
        self.client = self.env.rest_client
        self.test_id = 'system-test-{0}'.format(time.strftime("%Y%m%d-%H%M"))
        self.blueprint_yaml = None
        self._test_cleanup_context = self.env.handler.CleanupContext(
            self._testMethodName, self.env)
        # register cleanup
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._test_cleanup_context.cleanup()
        shutil.rmtree(self.workdir)

    def tearDown(self):
        # note that the cleanup function is registered in setUp
        # because it is called regardless of whether setUp succeeded or failed
        # unlike tearDown which is not called when setUp fails (which might
        # happen when tests override setUp)
        if self.env.management_ip:
            try:
                self.logger.info('Running ps aux on Cloudify manager...')
                output = StringIO()
                with fabric_api.settings(
                        user=self.env.management_user_name,
                        host_string=self.env.management_ip,
                        key_filename=get_actual_keypath(
                            self.env,
                            self.env.management_key_path),
                        disable_known_hosts=True):
                    fabric_api.run('ps aux --sort -rss', stdout=output)
                    self.logger.info(
                        'Cloudify manager ps aux output:\n{0}'.format(
                            output.getvalue()))
            except Exception as e:
                self.logger.info(
                    'Error running ps aux on Cloudify manager: {0}'.format(
                        str(e)))

    def get_manager_state(self):
        self.logger.info('Fetching manager current state')
        blueprints = {}
        for blueprint in self.client.blueprints.list():
            blueprints[blueprint.id] = blueprint
        deployments = {}
        for deployment in self.client.deployments.list():
            deployments[deployment.id] = deployment
        nodes = {}
        for deployment_id in deployments.keys():
            for node in self.client.node_instances.list(deployment_id):
                nodes[node.id] = node
        deployment_nodes = {}
        node_state = {}
        for deployment_id in deployments.keys():
            deployment_nodes[deployment_id] = self.client.node_instances.list(
                deployment_id)
            node_state[deployment_id] = {}
            for node in deployment_nodes[deployment_id]:
                node_state[deployment_id][node.id] = node

        return {
            'blueprints': blueprints,
            'deployments': deployments,
            'nodes': nodes,
            'node_state': node_state,
            'deployment_nodes': deployment_nodes
        }

    def get_manager_state_delta(self, before, after):
        after = copy.deepcopy(after)
        for blueprint_id in before['blueprints'].keys():
            del after['blueprints'][blueprint_id]
        for deployment_id in before['deployments'].keys():
            del after['deployments'][deployment_id]
            del after['deployment_nodes'][deployment_id]
            del after['node_state'][deployment_id]
        for node_id in before['nodes'].keys():
            del after['nodes'][node_id]
        return after

    def execute_install(self,
                        deployment_id=None,
                        fetch_state=True):
        return self._make_operation_with_before_after_states(
            self.cfy.execute_install,
            fetch_state,
            deployment_id=deployment_id)

    def upload_deploy_and_execute_install(
            self,
            blueprint_id=None,
            deployment_id=None,
            fetch_state=True,
            execute_timeout=DEFAULT_EXECUTE_TIMEOUT,
            inputs=None):

        return self._make_operation_with_before_after_states(
            self.cfy.upload_deploy_and_execute_install,
            fetch_state,
            str(self.blueprint_yaml),
            blueprint_id=blueprint_id or self.test_id,
            deployment_id=deployment_id or self.test_id,
            execute_timeout=execute_timeout,
            inputs=inputs)

    def _make_operation_with_before_after_states(self, operation, fetch_state,
                                                 *args, **kwargs):
        before_state = None
        after_state = None
        if fetch_state:
            before_state = self.get_manager_state()
        operation(*args, **kwargs)
        if fetch_state:
            after_state = self.get_manager_state()
        return before_state, after_state

    def execute_uninstall(self, deployment_id=None):
        self.cfy.execute_uninstall(deployment_id=deployment_id or self.test_id)

    def copy_blueprint(self, blueprint_dir_name):
        blueprint_path = path(self.workdir) / blueprint_dir_name
        shutil.copytree(get_blueprint_path(blueprint_dir_name),
                        str(blueprint_path))
        return blueprint_path

    def wait_for_execution(self, execution, timeout):
        end = time.time() + timeout
        while time.time() < end:
            status = self.client.executions.get(execution.id).status
            if status == 'failed':
                raise AssertionError('Execution "{}" failed'.format(
                    execution.id))
            if status == 'terminated':
                return
            time.sleep(1)
        events, _ = self.client.events.get(execution.id,
                                           batch_size=1000,
                                           include_logs=True)
        self.logger.info('Deployment creation events & logs:')
        for event in events:
            self.logger.info(json.dumps(event))
        raise AssertionError('Execution "{}" timed out'.format(execution.id))

    def repetitive(self, func, timeout=10, exception_class=Exception,
                   args=None, kwargs=None):
        args = args or []
        kwargs = kwargs or {}
        deadline = time.time() + timeout
        while True:
            try:
                return func(*args, **kwargs)
            except exception_class:
                if time.time() > deadline:
                    raise
                time.sleep(1)
