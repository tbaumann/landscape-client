from twisted.internet.defer import Deferred
from twisted.internet.error import ConnectError
from twisted.internet.task import Clock

from landscape.tests.helpers import LandscapeTest
from landscape.reactor import FakeReactor
from landscape.deployment import Configuration
from landscape.amp import ComponentPublisher, ComponentConnector


class TestComponent(object):

    name = "test"


class TestComponentConnector(ComponentConnector):

    component = TestComponent


class FakeAMP(object):

    def __init__(self, locator):
        self._locator = locator


class ComponentPublisherTest(LandscapeTest):

    def setUp(self):
        super(ComponentPublisherTest, self).setUp()
        reactor = FakeReactor()
        config = Configuration()
        config.data_path = self.makeDir()
        self.makeDir(path=config.sockets_path)
        self.component = TestComponent()
        self.publisher = ComponentPublisher(self.component, reactor, config)
        self.publisher.start()

        self.connector = TestComponentConnector(reactor, config)
        connected = self.connector.connect()
        connected.addCallback(lambda remote: setattr(self, "remote", remote))
        return connected

    def tearDown(self):
        self.connector.disconnect()
        self.publisher.stop()
        super(ComponentPublisherTest, self).tearDown()

    def test_ping(self):
        """
        The L{ComponentProtocol} exposes the C{ping} method of a
        remote Landscape component.
        """
        self.component.ping = self.mocker.mock()
        self.expect(self.component.ping()).result(True)
        self.mocker.replay()
        result = self.remote.ping()
        return self.assertSuccess(result, True)

    def test_exit(self):
        """
        The L{ComponentProtocol} exposes the C{exit} method of a
        remote Landscape component.
        """
        self.component.exit = self.mocker.mock()
        self.component.exit()
        self.mocker.replay()
        result = self.remote.exit()
        return self.assertSuccess(result)


class ComponentConnectorTest(LandscapeTest):

    def setUp(self):
        super(ComponentConnectorTest, self).setUp()
        self.reactor = FakeReactor()
        # XXX this should be dropped once the FakeReactor doesn't use the
        # real reactor anymore under the hood.
        self.reactor._reactor = Clock()
        self.config = Configuration()
        self.config.data_path = self.makeDir()
        self.makeDir(path=self.config.sockets_path)
        self.connector = TestComponentConnector(self.reactor, self.config)

    def test_connect_with_max_retries(self):
        """
        If C{max_retries} is passed to L{RemoteObjectConnector.connect},
        then it will give up trying to connect after that amount of times.
        """
        self.log_helper.ignore_errors("Error while connecting to test")
        deferred = self.connector.connect(max_retries=2)
        self.assertNoResult(deferred)
        return
        self.failureResultOf(deferred).trap(ConnectError)

    def test_connect_logs_errors(self):
        """
        Connection errors are logged.
        """
        self.log_helper.ignore_errors("Error while connecting to test")

        def assert_log(ignored):
            self.assertIn("Error while connecting to test",
                          self.logfile.getvalue())

        result = self.connector.connect(max_retries=0)
        self.assertFailure(result, ConnectError)
        return result.addCallback(assert_log)

    def test_connect_with_quiet(self):
        """
        If the C{quiet} option is passed, no errors will be logged.
        """
        result = self.connector.connect(max_retries=0, quiet=True)
        return self.assertFailure(result, ConnectError)

    def test_reconnect_fires_event(self):
        """
        An event is fired whenever the connection is established again after
        it has been lost.
        """
        reconnects = []
        self.reactor.call_on("test-reconnect", lambda: reconnects.append(True))

        component = TestComponent()
        publisher = ComponentPublisher(component, self.reactor, self.config)
        publisher.start()
        deferred = self.connector.connect()
        self.successResultOf(deferred)
        self.connector._connector.disconnect()  # Simulate a disconnection
        self.assertEqual([], reconnects)
        self.reactor._reactor.advance(10)
        self.assertEqual([True], reconnects)

    def test_connect_with_factor(self):
        """
        If C{factor} is passed to the L{ComponentConnector.connect} method,
        then the associated protocol factory will be set to that value.
        """
        component = TestComponent()
        publisher = ComponentPublisher(component, self.reactor, self.config)
        publisher.start()
        deferred = self.connector.connect(factor=1.0)
        remote = self.successResultOf(deferred)
        self.assertEqual(1.0, remote._factory.factor)

    def test_disconnect(self):
        """
        It is possible to call L{ComponentConnector.disconnect} multiple times,
        even if the connection has been already closed.
        """
        component = TestComponent()
        publisher = ComponentPublisher(component, self.reactor, self.config)
        publisher.start()
        self.connector.connect()
        self.connector.disconnect()
        self.connector.disconnect()

    def test_disconnect_without_connect(self):
        """
        It is possible to call L{ComponentConnector.disconnect} even if the
        connection was never established. In that case the method is
        effectively a no-op.
        """
        self.connector.disconnect()
