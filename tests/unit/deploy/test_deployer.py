import os

import socket
import botocore.session

import pytest
import mock
from botocore.stub import Stubber
from botocore.vendored.requests import ConnectionError as \
    RequestsConnectionError
from pytest import fixture

from chalice.app import Chalice
from chalice.awsclient import LambdaClientError, AWSClientError
from chalice.awsclient import DeploymentPackageTooLargeError
from chalice.awsclient import LambdaErrorContext
from chalice.config import Config
from chalice.policy import AppPolicyGenerator
from chalice.deploy.deployer import ChaliceDeploymentError
from chalice.utils import UI
import unittest

from attr import attrs, attrib

from chalice.awsclient import TypedAWSClient
from chalice.utils import OSUtils, serialize_to_json
from chalice.deploy import models
from chalice.deploy import packager
from chalice.deploy.deployer import create_default_deployer, \
    create_deletion_deployer, Deployer, \
    ApplicationGraphBuilder, DependencyBuilder, BaseDeployStep, \
    InjectDefaults, DeploymentPackager, SwaggerBuilder, \
    PolicyGenerator, BuildStage, ResultsRecorder, DeploymentReporter
from chalice.deploy.executor import Executor
from chalice.deploy.swagger import SwaggerGenerator, TemplatedSwaggerGenerator
from chalice.deploy.planner import PlanStage
from chalice.deploy.planner import StringFormat
from chalice.deploy.sweeper import ResourceSweeper
from chalice.deploy.models import APICall
from chalice.constants import LAMBDA_TRUST_POLICY, VPC_ATTACH_POLICY
from chalice.constants import SQS_EVENT_SOURCE_POLICY
from chalice.constants import POST_TO_WEBSOCKET_CONNECTION_POLICY
from chalice.deploy.deployer import ChaliceBuildError
from chalice.deploy.deployer import LambdaEventSourcePolicyInjector
from chalice.deploy.deployer import WebsocketPolicyInjector


_SESSION = None


class InMemoryOSUtils(object):
    def __init__(self, filemap=None):
        if filemap is None:
            filemap = {}
        self.filemap = filemap

    def file_exists(self, filename):
        return filename in self.filemap

    def get_file_contents(self, filename, binary=True):
        return self.filemap[filename]

    def set_file_contents(self, filename, contents, binary=True):
        self.filemap[filename] = contents


@fixture
def in_memory_osutils():
    return InMemoryOSUtils()


def stubbed_client(service_name):
    global _SESSION
    if _SESSION is None:
        _SESSION = botocore.session.get_session()
    client = _SESSION.create_client(service_name,
                                    region_name='us-west-2')
    stubber = Stubber(client)
    return client, stubber


@fixture
def config_obj(sample_app):
    config = Config.create(
        chalice_app=sample_app,
        stage='dev',
        api_gateway_stage='api',
    )
    return config


@fixture
def ui():
    return mock.Mock(spec=UI)


class TestChaliceDeploymentError(object):
    def test_general_exception(self):
        general_exception = Exception('My Exception')
        deploy_error = ChaliceDeploymentError(general_exception)
        deploy_error_msg = str(deploy_error)
        assert (
            'ERROR - While deploying your chalice application'
            in deploy_error_msg
        )
        assert 'My Exception' in deploy_error_msg

    def test_lambda_client_error(self):
        lambda_error = LambdaClientError(
            Exception('My Exception'),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=1024 ** 2
            )
        )
        deploy_error = ChaliceDeploymentError(lambda_error)
        deploy_error_msg = str(deploy_error)
        assert (
            'ERROR - While sending your chalice handler code to '
            'Lambda to create function \n"foo"' in deploy_error_msg
        )
        assert 'My Exception' in deploy_error_msg

    def test_lambda_client_error_wording_for_update(self):
        lambda_error = LambdaClientError(
            Exception('My Exception'),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='update_function_code',
                deployment_size=1024 ** 2
            )
        )
        deploy_error = ChaliceDeploymentError(lambda_error)
        deploy_error_msg = str(deploy_error)
        assert (
            'sending your chalice handler code to '
            'Lambda to update function' in deploy_error_msg
        )

    def test_gives_where_and_suggestion_for_too_large_deployment_error(self):
        too_large_error = DeploymentPackageTooLargeError(
            Exception('Too large of deployment pacakge'),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=1024 ** 2,
            )
        )
        deploy_error = ChaliceDeploymentError(too_large_error)
        deploy_error_msg = str(deploy_error)
        assert (
            'ERROR - While sending your chalice handler code to '
            'Lambda to create function \n"foo"' in deploy_error_msg
        )
        assert 'Too large of deployment pacakge' in deploy_error_msg
        assert (
            'To avoid this error, decrease the size of your chalice '
            'application ' in deploy_error_msg
        )

    def test_include_size_context_for_too_large_deployment_error(self):
        too_large_error = DeploymentPackageTooLargeError(
            Exception('Too large of deployment pacakge'),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=58 * (1024 ** 2),
            )
        )
        deploy_error = ChaliceDeploymentError(
            too_large_error)
        deploy_error_msg = str(deploy_error)
        print(repr(deploy_error_msg))
        assert 'deployment package is 58.0 MB' in deploy_error_msg
        assert '50.0 MB or less' in deploy_error_msg
        assert 'To avoid this error' in deploy_error_msg

    def test_error_msg_for_general_connection(self):
        lambda_error = DeploymentPackageTooLargeError(
            RequestsConnectionError(
                Exception(
                    'Connection aborted.',
                    socket.error('Some vague reason')
                )
            ),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=1024 ** 2
            )
        )
        deploy_error = ChaliceDeploymentError(lambda_error)
        deploy_error_msg = str(deploy_error)
        assert 'Connection aborted.' in deploy_error_msg
        assert 'Some vague reason' not in deploy_error_msg

    def test_simplifies_error_msg_for_broken_pipe(self):
        lambda_error = DeploymentPackageTooLargeError(
            RequestsConnectionError(
                Exception(
                    'Connection aborted.',
                    socket.error(32, 'Broken pipe')
                )
            ),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=1024 ** 2
            )
        )
        deploy_error = ChaliceDeploymentError(lambda_error)
        deploy_error_msg = str(deploy_error)
        assert (
            'Connection aborted. Lambda closed the connection' in
            deploy_error_msg
        )

    def test_simplifies_error_msg_for_timeout(self):
        lambda_error = DeploymentPackageTooLargeError(
            RequestsConnectionError(
                Exception(
                    'Connection aborted.',
                    socket.timeout('The write operation timed out')
                )
            ),
            context=LambdaErrorContext(
                function_name='foo',
                client_method_name='create_function',
                deployment_size=1024 ** 2
            )
        )
        deploy_error = ChaliceDeploymentError(lambda_error)
        deploy_error_msg = str(deploy_error)
        assert (
            'Connection aborted. Timed out sending your app to Lambda.' in
            deploy_error_msg
        )


@attrs
class FooResource(models.Model):
    name = attrib()
    leaf = attrib()

    def dependencies(self):
        if not isinstance(self.leaf, list):
            return [self.leaf]
        return self.leaf


@attrs
class LeafResource(models.Model):
    name = attrib()


@fixture
def lambda_app():
    app = Chalice('lambda-only')

    @app.lambda_function()
    def foo(event, context):
        return {}

    return app


@fixture
def scheduled_event_app():
    app = Chalice('scheduled-event')

    @app.schedule('rate(5 minutes)')
    def foo(event):
        return {}

    return app


@fixture
def cloudwatch_event_app():
    app = Chalice('cloudwatch-event')

    @app.on_cw_event({'source': {'source': ['aws.ec2']}})
    def foo(event):
        return event

    return app


@fixture
def rest_api_app():
    app = Chalice('rest-api')

    @app.route('/')
    def index():
        return {}

    return app


@fixture
def s3_event_app():
    app = Chalice('s3-event')

    @app.on_s3_event(bucket='mybucket')
    def handler(event):
        pass

    return app


@fixture
def sns_event_app():
    app = Chalice('sns-event')

    @app.on_sns_message(topic='mytopic')
    def handler(event):
        pass

    return app


@fixture
def sqs_event_app():
    app = Chalice('sqs-event')

    @app.on_sqs_message(queue='myqueue')
    def handler(event):
        pass

    return app


@fixture
def websocket_app():
    app = Chalice('websocket-event')

    @app.on_ws_connect()
    def connect(event):
        pass

    @app.on_ws_message()
    def message(event):
        pass

    @app.on_ws_disconnect()
    def disconnect(event):
        pass

    return app


@fixture
def websocket_app_without_connect():
    app = Chalice('websocket-event-no-connect')

    @app.on_ws_message()
    def message(event):
        pass

    @app.on_ws_disconnect()
    def disconnect(event):
        pass

    return app


@fixture
def websocket_app_without_message():
    app = Chalice('websocket-event-no-message')

    @app.on_ws_connect()
    def connect(event):
        pass

    @app.on_ws_disconnect()
    def disconnect(event):
        pass

    return app


@fixture
def websocket_app_without_disconnect():
    app = Chalice('websocket-event-no-disconnect')

    @app.on_ws_connect()
    def connect(event):
        pass

    @app.on_ws_message()
    def message(event):
        pass

    return app


@fixture
def mock_client():
    return mock.Mock(spec=TypedAWSClient)


@fixture
def mock_osutils():
    return mock.Mock(spec=OSUtils)


def create_function_resource(name):
    return models.LambdaFunction(
        resource_name=name,
        function_name='appname-dev-%s' % name,
        environment_variables={},
        runtime='python2.7',
        handler='app.app',
        tags={},
        timeout=60,
        memory_size=128,
        deployment_package=models.DeploymentPackage(filename='foo'),
        role=models.PreCreatedIAMRole(role_arn='role:arn'),
        security_group_ids=[],
        subnet_ids=[],
        layers=[]
    )


class TestDependencyBuilder(object):
    def test_can_build_resource_with_single_dep(self):
        role = models.PreCreatedIAMRole(role_arn='foo')
        app = models.Application(stage='dev', resources=[role])

        dep_builder = DependencyBuilder()
        deps = dep_builder.build_dependencies(app)
        assert deps == [role]

    def test_can_build_resource_with_dag_deps(self):
        shared_leaf = LeafResource(name='leaf-resource')
        first_parent = FooResource(name='first', leaf=shared_leaf)
        second_parent = FooResource(name='second', leaf=shared_leaf)
        app = models.Application(
            stage='dev', resources=[first_parent, second_parent])

        dep_builder = DependencyBuilder()
        deps = dep_builder.build_dependencies(app)
        assert deps == [shared_leaf, first_parent, second_parent]

    def test_is_first_element_in_list(self):
        shared_leaf = LeafResource(name='leaf-resource')
        first_parent = FooResource(name='first', leaf=shared_leaf)
        app = models.Application(
            stage='dev', resources=[first_parent, shared_leaf],
        )
        dep_builder = DependencyBuilder()
        deps = dep_builder.build_dependencies(app)
        assert deps == [shared_leaf, first_parent]

    def test_can_compares_with_identity_not_equality(self):
        first_leaf = LeafResource(name='same-name')
        second_leaf = LeafResource(name='same-name')
        first_parent = FooResource(name='first', leaf=first_leaf)
        second_parent = FooResource(name='second', leaf=second_leaf)
        app = models.Application(
            stage='dev', resources=[first_parent, second_parent])

        dep_builder = DependencyBuilder()
        deps = dep_builder.build_dependencies(app)
        assert deps == [first_leaf, first_parent, second_leaf, second_parent]

    def test_no_duplicate_depedencies(self):
        leaf = LeafResource(name='leaf')
        second_parent = FooResource(name='second', leaf=leaf)
        first_parent = FooResource(name='first', leaf=[leaf, second_parent])
        app = models.Application(
            stage='dev', resources=[first_parent])

        dep_builder = DependencyBuilder()
        deps = dep_builder.build_dependencies(app)
        assert deps == [leaf, second_parent, first_parent]


class TestApplicationGraphBuilder(object):

    def create_config(self, app, app_name='lambda-only',
                      iam_role_arn=None, policy_file=None,
                      api_gateway_stage='api',
                      autogen_policy=False, security_group_ids=None,
                      subnet_ids=None, reserved_concurrency=None, layers=None,
                      api_gateway_endpoint_type=None,
                      api_gateway_endpoint_vpce=None,
                      api_gateway_policy_file=None,
                      project_dir='.'):
        kwargs = {
            'chalice_app': app,
            'app_name': app_name,
            'project_dir': project_dir,
            'api_gateway_stage': api_gateway_stage,
            'api_gateway_policy_file': api_gateway_policy_file,
            'api_gateway_endpoint_type': api_gateway_endpoint_type,
            'api_gateway_endpoint_vpce': api_gateway_endpoint_vpce
        }
        if iam_role_arn is not None:
            # We want to use an existing role.
            # This will skip all the autogen-policy
            # and role creation.
            kwargs['manage_iam_role'] = False
            kwargs['iam_role_arn'] = 'role:arn'
        elif policy_file is not None:
            # Otherwise this setting is when a user wants us to
            # manage the role, but they've written a policy file
            # they'd like us to use.
            kwargs['autogen_policy'] = False
            kwargs['iam_policy_file'] = policy_file
        elif autogen_policy:
            kwargs['autogen_policy'] = True
        if security_group_ids is not None and subnet_ids is not None:
            kwargs['security_group_ids'] = security_group_ids
            kwargs['subnet_ids'] = subnet_ids
        if reserved_concurrency is not None:
            kwargs['reserved_concurrency'] = reserved_concurrency
        kwargs['layers'] = layers
        config = Config.create(**kwargs)
        return config

    def test_can_build_single_lambda_function_app(self, lambda_app):
        # This is the simplest configuration we can get.
        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app, iam_role_arn='role:arn')
        application = builder.build(config, stage_name='dev')
        # The top level resource is always an Application.
        assert isinstance(application, models.Application)
        assert len(application.resources) == 1
        assert application.resources[0] == models.LambdaFunction(
            resource_name='foo',
            function_name='lambda-only-dev-foo',
            environment_variables={},
            runtime=config.lambda_python_version,
            handler='app.foo',
            tags=config.tags,
            timeout=None,
            memory_size=None,
            deployment_package=models.DeploymentPackage(
                models.Placeholder.BUILD_STAGE),
            role=models.PreCreatedIAMRole('role:arn'),
            security_group_ids=[],
            subnet_ids=[],
            layers=[],
            reserved_concurrency=None,
        )

    def test_can_build_lambda_function_with_layers(self, lambda_app):
        # This is the simplest configuration we can get.
        builder = ApplicationGraphBuilder()
        layers = ['arn:aws:lambda:us-east-1:111:layer:test_layer:1']
        config = self.create_config(lambda_app,
                                    iam_role_arn='role:arn',
                                    layers=layers)
        application = builder.build(config, stage_name='dev')
        # The top level resource is always an Application.
        assert isinstance(application, models.Application)
        assert len(application.resources) == 1
        assert application.resources[0] == models.LambdaFunction(
            resource_name='foo',
            function_name='lambda-only-dev-foo',
            environment_variables={},
            runtime=config.lambda_python_version,
            handler='app.foo',
            tags=config.tags,
            timeout=None,
            memory_size=None,
            deployment_package=models.DeploymentPackage(
                models.Placeholder.BUILD_STAGE),
            role=models.PreCreatedIAMRole('role:arn'),
            security_group_ids=[],
            subnet_ids=[],
            layers=layers,
            reserved_concurrency=None,
        )

    def test_can_build_lambda_function_app_with_vpc_config(self, lambda_app):
        @lambda_app.lambda_function()
        def foo(event, context):
            pass

        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app,
                                    iam_role_arn='role:arn',
                                    security_group_ids=['sg1', 'sg2'],
                                    subnet_ids=['sn1', 'sn2'])
        application = builder.build(config, stage_name='dev')

        assert application.resources[0] == models.LambdaFunction(
            resource_name='foo',
            function_name='lambda-only-dev-foo',
            environment_variables={},
            runtime=config.lambda_python_version,
            handler='app.foo',
            tags=config.tags,
            timeout=None,
            memory_size=None,
            deployment_package=models.DeploymentPackage(
                models.Placeholder.BUILD_STAGE),
            role=models.PreCreatedIAMRole('role:arn'),
            security_group_ids=['sg1', 'sg2'],
            subnet_ids=['sn1', 'sn2'],
            layers=[],
            reserved_concurrency=None,
        )

    def test_vpc_trait_added_when_vpc_configured(self, lambda_app):
        @lambda_app.lambda_function()
        def foo(event, context):
            pass

        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app,
                                    autogen_policy=True,
                                    security_group_ids=['sg1', 'sg2'],
                                    subnet_ids=['sn1', 'sn2'])
        application = builder.build(config, stage_name='dev')

        policy = application.resources[0].role.policy
        assert policy == models.AutoGenIAMPolicy(
            document=models.Placeholder.BUILD_STAGE,
            traits=set([models.RoleTraits.VPC_NEEDED]),
        )

    def test_exception_raised_when_missing_vpc_params(self, lambda_app):
        @lambda_app.lambda_function()
        def foo(event, context):
            pass

        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app,
                                    iam_role_arn='role:arn',
                                    security_group_ids=['sg1', 'sg2'],
                                    subnet_ids=[])
        with pytest.raises(ChaliceBuildError):
            builder.build(config, stage_name='dev')

    def test_can_build_lambda_function_app_with_reserved_concurrency(
            self,
            lambda_app):
        # This is the simplest configuration we can get.
        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app,
                                    iam_role_arn='role:arn',
                                    reserved_concurrency=5)
        application = builder.build(config, stage_name='dev')
        # The top level resource is always an Application.
        assert isinstance(application, models.Application)
        assert len(application.resources) == 1
        assert application.resources[0] == models.LambdaFunction(
            resource_name='foo',
            function_name='lambda-only-dev-foo',
            environment_variables={},
            runtime=config.lambda_python_version,
            handler='app.foo',
            tags=config.tags,
            timeout=None,
            memory_size=None,
            deployment_package=models.DeploymentPackage(
                models.Placeholder.BUILD_STAGE),
            role=models.PreCreatedIAMRole('role:arn'),
            security_group_ids=[],
            subnet_ids=[],
            layers=[],
            reserved_concurrency=5,
        )

    def test_multiple_lambda_functions_share_role_and_package(self,
                                                              lambda_app):
        # We're going to add another lambda_function to our app.
        @lambda_app.lambda_function()
        def bar(event, context):
            return {}

        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app, iam_role_arn='role:arn')
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 2
        # The lambda functions by default share the same role
        assert application.resources[0].role == application.resources[1].role
        # Not just in equality but the exact same role objects.
        assert application.resources[0].role is application.resources[1].role
        # And all lambda functions share the same deployment package.
        assert (application.resources[0].deployment_package ==
                application.resources[1].deployment_package)

    def test_autogen_policy_for_function(self, lambda_app):
        # This test is just a sanity test that verifies all the params
        # for an ManagedIAMRole.  The various combinations for role
        # configuration is all tested via RoleTestCase.
        config = self.create_config(lambda_app, autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        function = application.resources[0]
        role = function.role
        # We should have linked a ManagedIAMRole
        assert isinstance(role, models.ManagedIAMRole)
        assert role == models.ManagedIAMRole(
            resource_name='default-role',
            role_name='lambda-only-dev',
            trust_policy=LAMBDA_TRUST_POLICY,
            policy=models.AutoGenIAMPolicy(models.Placeholder.BUILD_STAGE),
        )

    def test_cloudwatch_event_models(self, cloudwatch_event_app):
        config = self.create_config(cloudwatch_event_app,
                                    app_name='cloudwatch-event',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        event = application.resources[0]
        assert isinstance(event, models.CloudWatchEvent)
        assert event.resource_name == 'foo-event'
        assert event.rule_name == 'cloudwatch-event-dev-foo-event'
        assert isinstance(event.lambda_function, models.LambdaFunction)
        assert event.lambda_function.resource_name == 'foo'

    def test_scheduled_event_models(self, scheduled_event_app):
        config = self.create_config(scheduled_event_app,
                                    app_name='scheduled-event',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        event = application.resources[0]
        assert isinstance(event, models.ScheduledEvent)
        assert event.resource_name == 'foo-event'
        assert event.rule_name == 'scheduled-event-dev-foo-event'
        assert isinstance(event.lambda_function, models.LambdaFunction)
        assert event.lambda_function.resource_name == 'foo'

    def test_can_build_private_rest_api(self, rest_api_app):
        config = self.create_config(rest_api_app,
                                    app_name='rest-api-app',
                                    api_gateway_endpoint_type='PRIVATE',
                                    api_gateway_endpoint_vpce='vpce-abc123')
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        rest_api = application.resources[0]
        assert isinstance(rest_api, models.RestAPI)
        assert rest_api.policy.document == {
            'Version': '2012-10-17',
            'Statement': [
                {'Action': 'execute-api:Invoke',
                 'Effect': 'Allow',
                 'Principal': '*',
                 'Resource': 'arn:aws:execute-api:*:*:*',
                 'Condition': {
                     'StringEquals': {
                         'aws:SourceVpce': 'vpce-abc123'}}},
            ]
        }

    def test_can_build_private_rest_api_custom_policy(
            self, tmpdir, rest_api_app):
        config = self.create_config(rest_api_app,
                                    app_name='rest-api-app',
                                    api_gateway_policy_file='foo.json',
                                    api_gateway_endpoint_type='PRIVATE',
                                    project_dir=str(tmpdir))
        tmpdir.mkdir('.chalice').join('foo.json').write(
            serialize_to_json({'Version': '2012-10-17', 'Statement': []}))

        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        rest_api = application.resources[0]
        rest_api.policy.document == {
                'Version': '2012-10-17', 'Statement': []
            }

    def test_can_build_rest_api(self, rest_api_app):
        config = self.create_config(rest_api_app,
                                    app_name='rest-api-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        rest_api = application.resources[0]
        assert isinstance(rest_api, models.RestAPI)
        assert rest_api.resource_name == 'rest_api'
        assert rest_api.api_gateway_stage == 'api'
        assert rest_api.lambda_function.resource_name == 'api_handler'
        assert rest_api.lambda_function.function_name == 'rest-api-app-dev'
        # The swagger document is validated elsewhere so we just
        # make sure it looks right.
        assert rest_api.swagger_doc == models.Placeholder.BUILD_STAGE

    def test_can_build_rest_api_with_authorizer(self, rest_api_app):
        @rest_api_app.authorizer()
        def my_auth(auth_request):
            pass

        @rest_api_app.route('/auth', authorizer=my_auth)
        def needs_auth():
            return {'foo': 'bar'}

        config = self.create_config(rest_api_app,
                                    app_name='rest-api-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        rest_api = application.resources[0]
        assert len(rest_api.authorizers) == 1
        assert isinstance(rest_api.authorizers[0], models.LambdaFunction)

    def test_can_create_s3_event_handler(self, s3_event_app):
        # TODO: don't require app name, get it from app obj.
        config = self.create_config(s3_event_app,
                                    app_name='s3-event-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        s3_event = application.resources[0]
        assert isinstance(s3_event, models.S3BucketNotification)
        assert s3_event.resource_name == 'handler-s3event'
        assert s3_event.bucket == 'mybucket'
        assert s3_event.events == ['s3:ObjectCreated:*']
        lambda_function = s3_event.lambda_function
        assert lambda_function.resource_name == 'handler'
        assert lambda_function.handler == 'app.handler'

    def test_can_create_sns_event_handler(self, sns_event_app):
        config = self.create_config(sns_event_app,
                                    app_name='s3-event-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        sns_event = application.resources[0]
        assert isinstance(sns_event, models.SNSLambdaSubscription)
        assert sns_event.resource_name == 'handler-sns-subscription'
        assert sns_event.topic == 'mytopic'
        lambda_function = sns_event.lambda_function
        assert lambda_function.resource_name == 'handler'
        assert lambda_function.handler == 'app.handler'

    def test_can_create_sqs_event_handler(self, sqs_event_app):
        config = self.create_config(sqs_event_app,
                                    app_name='sqs-event-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        sqs_event = application.resources[0]
        assert isinstance(sqs_event, models.SQSEventSource)
        assert sqs_event.resource_name == 'handler-sqs-event-source'
        assert sqs_event.queue == 'myqueue'
        lambda_function = sqs_event.lambda_function
        assert lambda_function.resource_name == 'handler'
        assert lambda_function.handler == 'app.handler'

    def test_can_create_websocket_event_handler(self, websocket_app):
        config = self.create_config(websocket_app,
                                    app_name='websocket-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        websocket_api = application.resources[0]
        assert isinstance(websocket_api, models.WebsocketAPI)
        assert websocket_api.resource_name == 'websocket_api'
        assert sorted(websocket_api.routes) == sorted(
            ['$connect', '$default', '$disconnect'])
        assert websocket_api.api_gateway_stage == 'api'

        connect_function = websocket_api.connect_function
        assert connect_function.resource_name == 'websocket_connect'
        assert connect_function.handler == 'app.connect'

        message_function = websocket_api.message_function
        assert message_function.resource_name == 'websocket_message'
        assert message_function.handler == 'app.message'

        disconnect_function = websocket_api.disconnect_function
        assert disconnect_function.resource_name == 'websocket_disconnect'
        assert disconnect_function.handler == 'app.disconnect'

    def test_can_create_websocket_app_missing_connect(
            self, websocket_app_without_connect):
        config = self.create_config(websocket_app_without_connect,
                                    app_name='websocket-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        websocket_api = application.resources[0]
        assert isinstance(websocket_api, models.WebsocketAPI)
        assert websocket_api.resource_name == 'websocket_api'
        assert sorted(websocket_api.routes) == sorted(
            ['$default', '$disconnect'])
        assert websocket_api.api_gateway_stage == 'api'

        connect_function = websocket_api.connect_function
        assert connect_function is None

        message_function = websocket_api.message_function
        assert message_function.resource_name == 'websocket_message'
        assert message_function.handler == 'app.message'

        disconnect_function = websocket_api.disconnect_function
        assert disconnect_function.resource_name == 'websocket_disconnect'
        assert disconnect_function.handler == 'app.disconnect'

    def test_can_create_websocket_app_missing_message(
            self, websocket_app_without_message):
        config = self.create_config(websocket_app_without_message,
                                    app_name='websocket-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        websocket_api = application.resources[0]
        assert isinstance(websocket_api, models.WebsocketAPI)
        assert websocket_api.resource_name == 'websocket_api'
        assert sorted(websocket_api.routes) == sorted(
            ['$connect', '$disconnect'])
        assert websocket_api.api_gateway_stage == 'api'

        connect_function = websocket_api.connect_function
        assert connect_function.resource_name == 'websocket_connect'
        assert connect_function.handler == 'app.connect'

        disconnect_function = websocket_api.disconnect_function
        assert disconnect_function.resource_name == 'websocket_disconnect'
        assert disconnect_function.handler == 'app.disconnect'

    def test_can_create_websocket_app_missing_disconnect(
            self, websocket_app_without_disconnect):
        config = self.create_config(websocket_app_without_disconnect,
                                    app_name='websocket-app',
                                    autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 1
        websocket_api = application.resources[0]
        assert isinstance(websocket_api, models.WebsocketAPI)
        assert websocket_api.resource_name == 'websocket_api'
        assert sorted(websocket_api.routes) == sorted(
            ['$connect', '$default'])
        assert websocket_api.api_gateway_stage == 'api'

        connect_function = websocket_api.connect_function
        assert connect_function.resource_name == 'websocket_connect'
        assert connect_function.handler == 'app.connect'

        message_function = websocket_api.message_function
        assert message_function.resource_name == 'websocket_message'
        assert message_function.handler == 'app.message'


class RoleTestCase(object):
    def __init__(self, given, roles, app_name='appname'):
        self.given = given
        self.roles = roles
        self.app_name = app_name

    def build(self):
        app = Chalice(self.app_name)

        for name in self.given:
            def foo(event, context):
                return {}
            foo.__name__ = name
            app.lambda_function(name)(foo)

        user_provided_params = {
            'chalice_app': app,
            'app_name': self.app_name,
            'project_dir': '.',
        }
        lambda_functions = {}
        for key, value in self.given.items():
            lambda_functions[key] = value
        config_from_disk = {
            'stages': {
                'dev': {
                    'lambda_functions': lambda_functions,
                }
            }
        }
        config = Config(chalice_stage='dev',
                        user_provided_params=user_provided_params,
                        config_from_disk=config_from_disk)
        return app, config

    def assert_required_roles_created(self, application):
        resources = application.resources
        assert len(resources) == len(self.given)
        functions_by_name = {f.function_name: f for f in resources}
        # Roles that have the same name/arn should be the same
        # object.  If we encounter a role that's already in
        # roles_by_identifier, we'll verify that it's the exact same object.
        roles_by_identifier = {}
        for function_name, expected in self.roles.items():
            full_name = 'appname-dev-%s' % function_name
            assert full_name in functions_by_name
            actual_role = functions_by_name[full_name].role
            expectations = self.roles[function_name]
            if not expectations.get('managed_role', True):
                actual_role_arn = actual_role.role_arn
                assert isinstance(actual_role, models.PreCreatedIAMRole)
                assert expectations['iam_role_arn'] == actual_role_arn
                if actual_role_arn in roles_by_identifier:
                    assert roles_by_identifier[actual_role_arn] is actual_role
                roles_by_identifier[actual_role_arn] = actual_role
                continue
            actual_name = actual_role.role_name
            assert expectations['name'] == actual_name
            if actual_name in roles_by_identifier:
                assert roles_by_identifier[actual_name] is actual_role
            roles_by_identifier[actual_name] = actual_role
            is_autogenerated = expectations.get('autogenerated', False)
            policy_file = expectations.get('policy_file')
            if is_autogenerated:
                assert isinstance(actual_role, models.ManagedIAMRole)
                assert isinstance(actual_role.policy, models.AutoGenIAMPolicy)
            if policy_file is not None and not is_autogenerated:
                assert isinstance(actual_role, models.ManagedIAMRole)
                assert isinstance(actual_role.policy,
                                  models.FileBasedIAMPolicy)
                assert actual_role.policy.filename == os.path.join(
                    '.', '.chalice', expectations['policy_file'])


# How to read these tests:
# 'given' is a mapping of lambda function name to config values.
# 'roles' is a mapping of lambda function to expected attributes
# of the role associated with the given function.
# The first test case is explained in more detail as an example.
ROLE_TEST_CASES = [
    # Default case, we use the shared 'appname-dev' role.
    RoleTestCase(
        # Given we have a lambda function in our app.py named 'a',
        # and we have our config file state that the 'a' function
        # should have an autogen'd policy,
        given={'a': {'autogen_policy': True}},
        # then we expect the IAM role associated with the lambda
        # function 'a' should be named 'appname-dev', and it should
        # be an autogenerated role/policy.
        roles={'a': {'name': 'appname-dev', 'autogenerated': True}}),
    # If you specify an explicit policy, we generate a function
    # specific role.
    RoleTestCase(
        given={'a': {'autogen_policy': False,
                     'iam_policy_file': 'mypolicy.json'}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'mypolicy.json'}}),
    # Multiple lambda functions that use autogen policies share
    # the same 'appname-dev' role.
    RoleTestCase(
        given={'a': {'autogen_policy': True},
               'b': {'autogen_policy': True}},
        roles={'a': {'name': 'appname-dev'},
               'b': {'name': 'appname-dev'}}),
    # Multiple lambda functions with separate policies result
    # in separate roles.
    RoleTestCase(
        given={'a': {'autogen_policy': False,
                     'iam_policy_file': 'a.json'},
               'b': {'autogen_policy': False,
                     'iam_policy_file': 'b.json'}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'a.json'},
               'b': {'name': 'appname-dev-b',
                     'autogenerated': False,
                     'policy_file': 'b.json'}}),
    # You can mix autogen and explicit policy files.  Autogen will
    # always use the '{app}-{stage}' role.
    RoleTestCase(
        given={'a': {'autogen_policy': True},
               'b': {'autogen_policy': False,
                     'iam_policy_file': 'b.json'}},
        roles={'a': {'name': 'appname-dev',
                     'autogenerated': True},
               'b': {'name': 'appname-dev-b',
                     'autogenerated': False,
                     'policy_file': 'b.json'}}),
    # Default location if no policy file is given is
    # policy-dev.json
    RoleTestCase(
        given={'a': {'autogen_policy': False}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'policy-dev.json'}}),
    # As soon as autogen_policy is false, we will *always*
    # create a function specific role.
    RoleTestCase(
        given={'a': {'autogen_policy': False},
               'b': {'autogen_policy': True}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'policy-dev.json'},
               'b': {'name': 'appname-dev'}}),
    RoleTestCase(
        given={'a': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'}},
        # 'managed_role' will verify the associated role is a
        # models.PreCreatedIAMRoleType with the provided iam_role_arn.
        roles={'a': {'managed_role': False, 'iam_role_arn': 'role:arn'}}),
    # Verify that we can use the same non-managed role for multiple
    # lambda functions.
    RoleTestCase(
        given={'a': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'}},
        roles={'a': {'managed_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'managed_role': False, 'iam_role_arn': 'role:arn'}}),
    RoleTestCase(
        given={'a': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'autogen_policy': True}},
        roles={'a': {'managed_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'name': 'appname-dev', 'autogenerated': True}}),

    # Functions that mix all four options:
    RoleTestCase(
        # 2 functions with autogen'd policies.
        given={
            'a': {'autogen_policy': True},
            'b': {'autogen_policy': True},
            # 2 functions with various iam role arns.
            'c': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'},
            'd': {'manage_iam_role': False, 'iam_role_arn': 'role:arn2'},
            # A function with a default filename for a policy.
            'e': {'autogen_policy': False},
            # Even though this uses the same policy as 'e', we will
            # still create a new role.  This could be optimized in the
            # future.
            'f': {'autogen_policy': False},
            # And finally 2 functions that have their own policy files.
            'g': {'autogen_policy': False, 'iam_policy_file': 'g.json'},
            'h': {'autogen_policy': False, 'iam_policy_file': 'h.json'}
        },
        roles={
            'a': {'name': 'appname-dev', 'autogenerated': True},
            'b': {'name': 'appname-dev', 'autogenerated': True},
            'c': {'managed_role': False, 'iam_role_arn': 'role:arn'},
            'd': {'managed_role': False, 'iam_role_arn': 'role:arn2'},
            'e': {'name': 'appname-dev-e',
                  'autogenerated': False,
                  'policy_file': 'policy-dev.json'},
            'f': {'name': 'appname-dev-f',
                  'autogenerated': False,
                  'policy_file': 'policy-dev.json'},
            'g': {'name': 'appname-dev-g',
                  'autogenerated': False,
                  'policy_file': 'g.json'},
            'h': {'name': 'appname-dev-h',
                  'autogenerated': False,
                  'policy_file': 'h.json'},
        }),
]


@pytest.mark.parametrize('case', ROLE_TEST_CASES)
def test_role_creation(case):
    _, config = case.build()
    builder = ApplicationGraphBuilder()
    application = builder.build(config, stage_name='dev')
    case.assert_required_roles_created(application)


class TestDefaultsInjector(object):
    def test_inject_when_values_are_none(self):
        injector = InjectDefaults(
            lambda_timeout=100,
            lambda_memory_size=512,
        )
        function = models.LambdaFunction(
            # The timeout/memory_size are set to
            # None, so the injector should fill them
            # in the with the default values above.
            timeout=None,
            memory_size=None,
            resource_name='foo',
            function_name='app-dev-foo',
            environment_variables={},
            runtime='python2.7',
            handler='app.app',
            tags={},
            deployment_package=None,
            role=None,
            security_group_ids=[],
            subnet_ids=[],
            layers=[],
            reserved_concurrency=None,
        )
        config = Config.create()
        injector.handle(config, function)
        assert function.timeout == 100
        assert function.memory_size == 512

    def test_no_injection_when_values_are_set(self):
        injector = InjectDefaults(
            lambda_timeout=100,
            lambda_memory_size=512,
        )
        function = models.LambdaFunction(
            # The timeout/memory_size are set to
            # None, so the injector should fill them
            # in the with the default values above.
            timeout=1,
            memory_size=1,
            resource_name='foo',
            function_name='app-stage-foo',
            environment_variables={},
            runtime='python2.7',
            handler='app.app',
            tags={},
            deployment_package=None,
            role=None,
            security_group_ids=[],
            subnet_ids=[],
            layers=[],
            reserved_concurrency=None,
        )
        config = Config.create()
        injector.handle(config, function)
        assert function.timeout == 1
        assert function.memory_size == 1


class TestPolicyGeneratorStage(object):
    def setup_method(self):
        self.osutils = mock.Mock(spec=OSUtils)

    def create_policy_generator(self, generator=None):
        if generator is None:
            generator = mock.Mock(spec=AppPolicyGenerator)
        p = PolicyGenerator(generator, self.osutils)
        return p

    def test_invokes_policy_generator(self):
        generator = mock.Mock(spec=AppPolicyGenerator)
        generator.generate_policy.return_value = {'policy': 'doc'}
        policy = models.AutoGenIAMPolicy(models.Placeholder.BUILD_STAGE)
        config = Config.create()

        p = self.create_policy_generator(generator)
        p.handle(config, policy)

        assert policy.document == {'policy': 'doc'}

    def test_no_policy_generated_if_exists(self):
        generator = mock.Mock(spec=AppPolicyGenerator)
        generator.generate_policy.return_value = {'policy': 'new'}
        policy = models.AutoGenIAMPolicy(document={'policy': 'original'})
        config = Config.create()

        p = self.create_policy_generator(generator)
        p.handle(config, policy)

        assert policy.document == {'policy': 'original'}
        assert not generator.generate_policy.called

    def test_policy_loaded_from_file_if_needed(self):
        p = self.create_policy_generator()
        policy = models.FileBasedIAMPolicy(
            filename='foo.json', document=models.Placeholder.BUILD_STAGE)
        self.osutils.get_file_contents.return_value = '{"iam": "policy"}'

        p.handle(Config.create(), policy)

        assert policy.document == {'iam': 'policy'}
        self.osutils.get_file_contents.assert_called_with('foo.json')

    def test_error_raised_if_file_policy_not_exists(self):
        p = self.create_policy_generator()
        policy = models.FileBasedIAMPolicy(
            filename='foo.json', document=models.Placeholder.BUILD_STAGE)
        self.osutils.get_file_contents.side_effect = IOError()

        with pytest.raises(RuntimeError):
            p.handle(Config.create(), policy)

    def test_vpc_policy_inject_if_needed(self):
        generator = mock.Mock(spec=AppPolicyGenerator)
        generator.generate_policy.return_value = {'Statement': []}
        policy = models.AutoGenIAMPolicy(
            document=models.Placeholder.BUILD_STAGE,
            traits=set([models.RoleTraits.VPC_NEEDED]),
        )
        config = Config.create()

        p = self.create_policy_generator(generator)
        p.handle(config, policy)

        assert policy.document['Statement'][0] == VPC_ATTACH_POLICY


class TestSwaggerBuilder(object):
    def test_can_generate_swagger_builder(self):
        generator = mock.Mock(spec=SwaggerGenerator)
        generator.generate_swagger.return_value = {'swagger': '2.0'}

        rest_api = models.RestAPI(
            resource_name='foo',
            swagger_doc=models.Placeholder.BUILD_STAGE,
            minimum_compression='',
            endpoint_type='EDGE',
            api_gateway_stage='api',
            lambda_function=None,
        )
        app = Chalice(app_name='foo')
        config = Config.create(chalice_app=app)
        p = SwaggerBuilder(generator)
        p.handle(config, rest_api)
        assert rest_api.swagger_doc == {'swagger': '2.0'}
        generator.generate_swagger.assert_called_with(app, rest_api)


class TestDeploymentPackager(object):
    def test_can_generate_package(self):
        generator = mock.Mock(spec=packager.LambdaDeploymentPackager)
        generator.create_deployment_package.return_value = 'package.zip'

        package = models.DeploymentPackage(models.Placeholder.BUILD_STAGE)
        config = Config.create()

        p = DeploymentPackager(generator)
        p.handle(config, package)

        assert package.filename == 'package.zip'

    def test_package_not_generated_if_filename_populated(self):
        generator = mock.Mock(spec=packager.LambdaDeploymentPackager)
        generator.create_deployment_package.return_value = 'NEWPACKAGE.zip'

        package = models.DeploymentPackage(filename='original-name.zip')
        config = Config.create()

        p = DeploymentPackager(generator)
        p.handle(config, package)

        assert package.filename == 'original-name.zip'
        assert not generator.create_deployment_package.called


def test_build_stage():
    first = mock.Mock(spec=BaseDeployStep)
    second = mock.Mock(spec=BaseDeployStep)
    build = BuildStage([first, second])

    foo_resource = mock.sentinel.foo_resource
    bar_resource = mock.sentinel.bar_resource
    config = Config.create()
    build.execute(config, [foo_resource, bar_resource])

    assert first.handle.call_args_list == [
        mock.call(config, foo_resource),
        mock.call(config, bar_resource),
    ]
    assert second.handle.call_args_list == [
        mock.call(config, foo_resource),
        mock.call(config, bar_resource),
    ]


class TestDeployer(unittest.TestCase):
    def setUp(self):
        self.resource_builder = mock.Mock(spec=ApplicationGraphBuilder)
        self.deps_builder = mock.Mock(spec=DependencyBuilder)
        self.build_stage = mock.Mock(spec=BuildStage)
        self.plan_stage = mock.Mock(spec=PlanStage)
        self.sweeper = mock.Mock(spec=ResourceSweeper)
        self.executor = mock.Mock(spec=Executor)
        self.recorder = mock.Mock(spec=ResultsRecorder)
        self.chalice_app = Chalice(app_name='foo')

    def create_deployer(self):
        return Deployer(
            self.resource_builder,
            self.deps_builder,
            self.build_stage,
            self.plan_stage,
            self.sweeper,
            self.executor,
            self.recorder,
        )

    def test_deploy_delegates_properly(self):
        app = mock.Mock(spec=models.Application)
        resources = [mock.Mock(spec=models.Model)]
        api_calls = [mock.Mock(spec=APICall)]

        self.resource_builder.build.return_value = app
        self.deps_builder.build_dependencies.return_value = resources
        self.plan_stage.execute.return_value = api_calls
        self.executor.resource_values = {'foo': {'name': 'bar'}}

        deployer = self.create_deployer()
        config = Config.create(project_dir='.', chalice_app=self.chalice_app)
        result = deployer.deploy(config, 'dev')

        self.resource_builder.build.assert_called_with(config, 'dev')
        self.deps_builder.build_dependencies.assert_called_with(app)
        self.build_stage.execute.assert_called_with(config, resources)
        self.plan_stage.execute.assert_called_with(resources)
        self.sweeper.execute.assert_called_with(api_calls, config)
        self.executor.execute.assert_called_with(api_calls)

        expected_result = {
            'resources': {'foo': {'name': 'bar'}},
            'schema_version': '2.0',
            'backend': 'api',
        }

        self.recorder.record_results.assert_called_with(
            expected_result, 'dev', '.')
        assert result == expected_result

    def test_deploy_errors_raises_chalice_error(self):
        self.resource_builder.build.side_effect = AWSClientError()

        deployer = self.create_deployer()
        config = Config.create(project_dir='.', chalice_app=self.chalice_app)
        with pytest.raises(ChaliceDeploymentError):
            deployer.deploy(config, 'dev')

    def test_validation_errors_raise_failure(self):

        @self.chalice_app.route('')
        def bad_route_empty_string():
            return {}

        deployer = self.create_deployer()
        config = Config.create(project_dir='.', chalice_app=self.chalice_app)
        with pytest.raises(ChaliceDeploymentError):
            deployer.deploy(config, 'dev')


def test_can_create_default_deployer():
    session = botocore.session.get_session()
    deployer = create_default_deployer(session, Config.create(
        project_dir='.',
        chalice_stage='dev',
    ), UI())
    assert isinstance(deployer, Deployer)


def test_can_create_deletion_deployer():
    session = botocore.session.get_session()
    deployer = create_deletion_deployer(TypedAWSClient(session), UI())
    assert isinstance(deployer, Deployer)


def test_templated_swagger_generator(rest_api_app):
    doc = TemplatedSwaggerGenerator().generate_swagger(rest_api_app)
    uri = doc['paths']['/']['get']['x-amazon-apigateway-integration']['uri']
    assert isinstance(uri, StringFormat)
    assert uri.template == (
        'arn:aws:apigateway:{region_name}:lambda:path'
        '/2015-03-31/functions/{api_handler_lambda_arn}/invocations'
    )
    assert uri.variables == ['region_name', 'api_handler_lambda_arn']


def test_templated_swagger_with_auth_uri(rest_api_app):
    @rest_api_app.authorizer()
    def myauth(auth_request):
        pass

    @rest_api_app.route('/auth', authorizer=myauth)
    def needsauth():
        return {}

    doc = TemplatedSwaggerGenerator().generate_swagger(rest_api_app)
    uri = doc['securityDefinitions']['myauth'][
        'x-amazon-apigateway-authorizer']['authorizerUri']
    assert isinstance(uri, StringFormat)
    assert uri.template == (
        'arn:aws:apigateway:{region_name}:lambda:path'
        '/2015-03-31/functions/{myauth_lambda_arn}/invocations'
    )
    assert uri.variables == ['region_name', 'myauth_lambda_arn']


class TestRecordResults(object):
    def setup_method(self):
        self.osutils = mock.Mock(spec=OSUtils)
        self.recorder = ResultsRecorder(self.osutils)
        self.deployed_values = {
            'stages': {
                'dev': {'resources': []},
            },
            'schema_version': '2.0',
        }
        self.osutils.joinpath = os.path.join
        self.deployed_dir = os.path.join('.', '.chalice', 'deployed')

    def test_can_record_results_initial_deploy(self):
        expected_filename = os.path.join(self.deployed_dir, 'dev.json')
        self.osutils.file_exists.return_value = False
        self.osutils.directory_exists.return_value = False
        self.recorder.record_results(
            self.deployed_values, 'dev', '.',
        )
        expected_contents = serialize_to_json(self.deployed_values)
        # Verify we created the deployed dir on an initial deploy.
        self.osutils.makedirs.assert_called_with(self.deployed_dir)
        self.osutils.set_file_contents.assert_called_with(
            filename=expected_filename,
            contents=expected_contents,
            binary=False
        )


class TestDeploymentReporter(object):
    def setup_method(self):
        self.ui = mock.Mock(spec=UI)
        self.reporter = DeploymentReporter(ui=self.ui)

    def test_can_generate_report(self):
        deployed_values = {
            "resources": [
                {"role_name": "james2-dev",
                 "role_arn": "my-role-arn",
                 "name": "default-role",
                 "resource_type": "iam_role"},
                {"lambda_arn": "lambda-arn-foo",
                 "name": "foo",
                 "resource_type": "lambda_function"},
                {"lambda_arn": "lambda-arn-dev",
                 "name": "api_handler",
                 "resource_type": "lambda_function"},
                {"name": "rest_api",
                 "rest_api_id": "rest_api_id",
                 "rest_api_url": "https://host/api",
                 "resource_type": "rest_api"},
                {"name": "websocket_api",
                 "websocket_api_id": "websocket_api_id",
                 "websocket_api_url": "wss://host/api",
                 "resource_type": "websocket_api"},
            ],
        }
        report = self.reporter.generate_report(deployed_values)
        assert report == (
            "Resources deployed:\n"
            "  - Lambda ARN: lambda-arn-foo\n"
            "  - Lambda ARN: lambda-arn-dev\n"
            "  - Rest API URL: https://host/api\n"
            "  - Websocket API URL: wss://host/api\n"
        )

    def test_can_display_report(self):
        deployed_values = {
            'resources': []
        }
        self.reporter.display_report(deployed_values)
        self.ui.write.assert_called_with('Resources deployed:\n')


class TestLambdaEventSourcePolicyInjector(object):
    def create_model_from_app(self, app, config):
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        return application.resources[0]

    def test_can_inject_policy(self, sqs_event_app):
        config = Config.create(chalice_app=sqs_event_app,
                               autogen_policy=True,
                               project_dir='.')
        event_source = self.create_model_from_app(sqs_event_app, config)
        role = event_source.lambda_function.role
        role.policy.document = {'Statement': []}
        injector = LambdaEventSourcePolicyInjector()
        injector.handle(config, event_source)
        assert role.policy.document == {
            'Statement': [SQS_EVENT_SOURCE_POLICY.copy()],
        }

    def test_no_inject_if_not_autogen_policy(self, sqs_event_app):
        config = Config.create(chalice_app=sqs_event_app,
                               autogen_policy=False,
                               project_dir='.')
        event_source = self.create_model_from_app(sqs_event_app, config)
        role = event_source.lambda_function.role
        role.policy.document = {'Statement': []}
        injector = LambdaEventSourcePolicyInjector()
        injector.handle(config, event_source)
        assert role.policy.document == {'Statement': []}

    def test_no_inject_is_already_injected(self, sqs_event_app):
        @sqs_event_app.on_sqs_message(queue='second-queue')
        def second_handler(event):
            pass

        config = Config.create(chalice_app=sqs_event_app,
                               autogen_policy=True,
                               project_dir='.')
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        event_sources = application.resources
        role = event_sources[0].lambda_function.role
        role.policy.document = {'Statement': []}
        injector = LambdaEventSourcePolicyInjector()
        injector.handle(config, event_sources[0])
        injector.handle(config, event_sources[1])
        # Even though we have two queue handlers, we only need to
        # inject the policy once.
        assert role.policy.document == {
            'Statement': [SQS_EVENT_SOURCE_POLICY.copy()],
        }


class TestWebsocketPolicyInjector(object):
    def create_model_from_app(self, app, config):
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        return application.resources[0]

    def test_can_inject_policy(self, websocket_app):
        config = Config.create(chalice_app=websocket_app,
                               autogen_policy=True,
                               project_dir='.')
        event_source = self.create_model_from_app(websocket_app, config)
        role = event_source.connect_function.role
        role.policy.document = {'Statement': []}
        injector = WebsocketPolicyInjector()
        injector.handle(config, event_source)
        assert role.policy.document == {
            'Statement': [POST_TO_WEBSOCKET_CONNECTION_POLICY.copy()],
        }

    def test_no_inject_if_not_autogen_policy(self, websocket_app):
        config = Config.create(chalice_app=websocket_app,
                               autogen_policy=False,
                               project_dir='.')
        event_source = self.create_model_from_app(websocket_app, config)
        role = event_source.connect_function.role
        role.policy.document = {'Statement': []}
        injector = LambdaEventSourcePolicyInjector()
        injector.handle(config, event_source)
        assert role.policy.document == {'Statement': []}
