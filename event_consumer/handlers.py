"""
Apparatus for consuming 'vanilla' AMQP messages (i.e. not Celery tasks) making use
of the battle-tested `bin/celery worker` utility rather than writing our own.

NOTE:
We don't access any Celery config in this file. That is because the config is
loaded by the bin/celery worker itself, according to usual mechanism.
"""
import logging
import traceback

import six
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Union  # noqa

import amqp  # noqa
import celery.bootsteps as bootsteps
import kombu
import kombu.message
import kombu.common as common

from event_consumer.conf import settings
from event_consumer.errors import InvalidQueueRegistration, NoExchange, PermanentFailure
from event_consumer.types import QueueRegistration

if settings.USE_DJANGO:
    from django.core.signals import request_finished


_logger = logging.getLogger(__name__)


# Maps routing-keys to handlers
REGISTRY = {}  # type: Dict[QueueRegistration, Callable]

# Map of exchange name to registered queues
QUEUE_NAMES = {}  # type: Dict[str, Set[str]]

DEFAULT_EXCHANGE = 'default'  # key in settings.EXCHANGES


def _validate_registration(register_key):  # type: (QueueRegistration) -> None
    """
    Raises:
        InvalidQueueRegistration
    """
    global REGISTRY
    existing = {(r.queue, r.exchange) for r in REGISTRY.keys()}
    if (register_key.queue, register_key.exchange) in existing:
        raise InvalidQueueRegistration(
            'Attempted duplicate registrations for messages with the queue name '
            '"{0}" and exchange "{1}"'.format(
                register_key.queue,
                register_key.exchange,
            )
        )


def message_handler(routing_keys,  # type: Union[str, Iterable]
                    queue=None,  # type: Optional[str]
                    exchange=DEFAULT_EXCHANGE  # type: str
                    ):
    # type: (...) ->  Callable[[Callable], Any]
    """
    Register a function as a handler for messages on a rabbitmq exchange with
    the given routing-key. Default behaviour is to use `routing_key` as the
    queue name and attach it to the 'default' exchange. If this key is not
    present in `settings.EXCHANGES` with your own config then you will get the
    underlying AMQP default exchange - this has some restrictions (you cannot
    bind custom queue names, only auto-bound same-as-routing-key queues are
    possible).Kwargs

    Otherwise Queues and Exchanges are automatically created on the broker
    by Kombu and you don't have to worry about it.

    Kwargs:
        routing_keys: The routing key/s of messages to be handled by the
            decorated task.
        queue: The name of the main queue from which messages
            will be consumed. Defaults to '{QUEUE_NAME_PREFIX}{routing_key}`
            if not supplied. Special case is '' this will give you Kombu
            default queue name without prepending `QUEUE_NAME_PREFIX`.
        exchange: The AMQP exchange config to use. This is a *key name*
            in the `settings.EXCHANGES` dict.

    Returns:
        Callable: function decorator

    Raises:
        InvalidQueueRegistration

    Usage:
        @message_handler('my.routing.key', 'my.queue', 'my.exchange')
        def process_message(body):
            print(body)  # Whatever

    Note that this is an import side-effect (as is Celery's @task decorator).
    In order for the event handler to be registered, its containing module must
    be imported before starting the AMQPRetryConsumerStep.
    """
    if (queue or (queue is None and settings.QUEUE_NAME_PREFIX)) \
            and exchange not in settings.EXCHANGES:
        raise InvalidQueueRegistration(
            "You must use a named exchange from settings.EXCHANGES "
            "if you want to bind a custom queue name."
        )

    if isinstance(routing_keys, six.string_types):
        routing_keys = [routing_keys]
    else:
        if queue is not None:
            raise InvalidQueueRegistration(
                "We need a queue-per-routing-key so you can't specify a "
                "custom queue name when attaching mutiple routes. Use "
                "separate handlers for each routing key in this case."
            )

    def decorator(f):  # type: (Callable) -> Callable
        global REGISTRY, QUEUE_NAMES

        for routing_key in routing_keys:
            queue_name = (settings.QUEUE_NAME_PREFIX + routing_key) if queue is None else queue

            # kombu.Consumer has no concept of routing-key (only queue name) so
            # so handler registrations must be unique on queue+exchange (otherwise
            # messages from the queue would be randomly sent to the duplicate handlers)
            register_key = QueueRegistration(routing_key, queue_name, exchange)
            _validate_registration(register_key)

            REGISTRY[register_key] = f

            names_ = QUEUE_NAMES.setdefault(exchange, set())
            for name_ in AMQPRetryHandler.queue_names_for(queue_name):
                if name_ in names_:
                    raise InvalidQueueRegistration(
                        "Queue name '{}' is already registered to exchange '{}'".format(
                            name_,
                            exchange
                        )
                    )
                names_.add(name_)

        return f

    return decorator


class AMQPRetryConsumerStep(bootsteps.StartStopStep):
    """
    An integration hook with Celery which is adapted from the built in class
    `bootsteps.ConsumerStep`. Instead of registering a `kombu.Consumer` on
    startup, we create instances of `AMQPRetryHandler` passing in a channel
    which is used to create all the queues/exchanges/etc. needed to
    implement our try-retry-archive scheme.

    See http://docs.celeryproject.org/en/latest/userguide/extending.html
    """

    requires = ('celery.worker.consumer:Connection', )

    def __init__(self, *args, **kwargs):
        self.handlers = []  # type: List[AMQPRetryHandler]
        self._tasks = kwargs.pop('tasks', REGISTRY)  # type: Dict[QueueRegistration, Callable]
        super(AMQPRetryConsumerStep, self).__init__(*args, **kwargs)

    def start(self, c):
        channel = c.connection.channel()
        self.handlers = self.get_handlers(channel)

        for handler in self.handlers:
            handler.declare_queues()
            handler.consumer.consume()

    def stop(self, c):
        self._close(c, True)

    def shutdown(self, c):
        self._close(c, False)

    def _close(self, c, cancel_consumers=True):
        channels = set()
        for handler in self.handlers:
            if cancel_consumers:
                common.ignore_errors(c.connection, handler.consumer.cancel)
            if handler.consumer.channel:
                channels.add(handler.consumer.channel)
        for channel in channels:
            common.ignore_errors(c.connection, channel.close)

    # custom methods:
    def get_handlers(self, channel):
        # type (channel: kombu.transport.base.StdChannel) -> List[AMQPRetryHandler]
        return [
            AMQPRetryHandler(
                channel,
                queue_registration.routing_key,
                queue_registration.queue,
                queue_registration.exchange,
                func,
                backoff_func=settings.BACKOFF_FUNC,
            )
            for queue_registration, func in self._tasks.items()
        ]


class AMQPRetryHandler(object):
    """
    Implements Depop's try-retry-archive message queue pattern.

    Briefly - messages are processed and may be retried by placing them on a separate retry
    queue on a dead-letter-exchange. Messages on the DLX are automatically re-queued by Rabbit
    once they expire. The expiry is set on a message-by-message basis to allow exponential
    backoff on retries.
    """

    WORKER = 'worker'
    RETRY = 'retry'
    ARCHIVE = 'archive'

    QUEUE_NAME_FORMATS = {
        WORKER: '{}',
        RETRY: '{}.retry',
        ARCHIVE: '{}.archived',
    }

    exchanges = None  # type: Dict[str, kombu.Exchange]
    queues = None  # type: Dict[str, kombu.Queue]

    def __init__(self,
                 channel,  # type: amqp.channel.Channel
                 routing_key,  # type: str
                 queue,  # type: str
                 exchange,  # type: str
                 func,  # type: Callable[[Any], Any]
                 backoff_func=None  # type: Optional[Callable[[int], float]]
                 ):
        # type: (...) -> None
        self.channel = channel
        self.routing_key = routing_key
        self.queue = queue  # queue name
        self.exchange = exchange  # `settings.EXCHANGES` config key
        self.func = func
        self.backoff_func = backoff_func or self.backoff

        self.exchanges = {}
        if exchange != DEFAULT_EXCHANGE:
            try:
                self.exchanges[exchange] = kombu.Exchange(
                    channel=self.channel,
                    **settings.EXCHANGES[exchange]
                )
            except KeyError:
                raise NoExchange(
                    "The exchange '{0}'' was not found in settings.EXCHANGES. \n"
                    "settings.EXCHANGES = {1}".format(
                        exchange,
                        settings.EXCHANGES
                    )
                )

        if DEFAULT_EXCHANGE in settings.EXCHANGES:
            self.exchanges[DEFAULT_EXCHANGE] = kombu.Exchange(
                channel=self.channel,
                **settings.EXCHANGES[DEFAULT_EXCHANGE]
            )
        else:
            self.exchanges[DEFAULT_EXCHANGE] = kombu.Exchange(channel=self.channel)

        self.queues = {}

        self.queues[self.WORKER] = kombu.Queue(
            name=self.QUEUE_NAME_FORMATS[self.WORKER].format(self.queue),
            exchange=self.exchanges[exchange],
            routing_key=self.routing_key,
            channel=self.channel,
        )

        self.queues[self.RETRY] = kombu.Queue(
            name=self.QUEUE_NAME_FORMATS[self.RETRY].format(self.queue),
            exchange=self.exchanges[DEFAULT_EXCHANGE],
            routing_key='{0}.retry'.format(queue),
            # N.B. default exchange automatically routes messages to a queue
            # with the same name as the routing key provided.
            queue_arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.queue,
            },
            channel=self.channel,
        )

        self.queues[self.ARCHIVE] = kombu.Queue(
            name=self.QUEUE_NAME_FORMATS[self.ARCHIVE].format(self.queue),
            exchange=self.exchanges[DEFAULT_EXCHANGE],
            routing_key='{0}.archived'.format(queue),
            queue_arguments={
                "x-expires": settings.ARCHIVE_EXPIRY,  # Messages dropped after this
                "x-max-length": 1000000,  # Maximum size of the queue
                "x-queue-mode": "lazy",  # Keep messages on disk (reqs. rabbitmq 3.6.0+)
            },
            channel=self.channel,
        )

        self.retry_producer = kombu.Producer(
            channel,
            exchange=self.queues[self.RETRY].exchange,
            routing_key=self.queues[self.RETRY].routing_key,
            serializer=settings.SERIALIZER,
        )

        self.archive_producer = kombu.Producer(
            channel,
            exchange=self.queues[self.ARCHIVE].exchange,
            routing_key=self.queues[self.ARCHIVE].routing_key,
            serializer=settings.SERIALIZER,
        )

        self.consumer = kombu.Consumer(
            channel,
            queues=[self.queues[self.WORKER]],
            callbacks=[self],
            accept=settings.ACCEPT,
        )

    @classmethod
    def queue_names_for(cls, queue):
        # type: (str) -> List[str]
        return [
            template.format(queue)
            for template in cls.QUEUE_NAME_FORMATS.values()
        ]

    def __call__(self, body, message):
        """
        Handle a vanilla AMQP message, called by the Celery framework.

        Raising an exception in this method will crash the Celery worker. Ensure
        that all Exceptions are caught and messages acknowledged or rejected
        as they are processed.

        Args:
            body (Any): the message content, which has been deserialized by Kombu
            message (kombu.message.Message)

        Returns:
            None
        """
        retry_count = self.retry_count(message)

        try:
            _logger.debug('Received: (key={}, retry_count={})'.format(
                self.routing_key,
                retry_count,
            ))
            self.func(body)

        except Exception as e:
            if isinstance(e, PermanentFailure):
                self.archive(
                    body,
                    message,
                    "Task '{}' raised '{}, {}'\n{}".format(
                        self.routing_key,
                        e.__class__.__name__,
                        e,
                        traceback.format_exc(),
                    )
                )
            elif retry_count >= settings.MAX_RETRIES:
                self.archive(
                    body,
                    message,
                    "Task '{}' ran out of retries on exception '{}, {}'\n{}".format(
                        self.routing_key,
                        e.__class__.__name__,
                        e,
                        traceback.format_exc(),
                    )
                )
            else:
                self.retry(
                    body,
                    message,
                    "Task '{}' raised the exception '{}, {}', but there are retries left\n{}".format(
                        self.routing_key,
                        e.__class__.__name__,
                        e,
                        traceback.format_exc(),
                    )
                )
        else:
            message.ack()
            _logger.debug("Task '{}' processed and ack() sent".format(self.routing_key))

        finally:
            if settings.USE_DJANGO:
                # avoid various problems with db connections, due to long-lived
                # worker not automatically participating in Django request lifecycle
                request_finished.send(sender="AMQPRetryHandler")

            if not message.acknowledged:
                message.requeue()
                _logger.critical(
                    "Messages for task '{}' are not sending an ack() or a reject(). "
                    "This needs attention. Assuming some kind of error and requeueing the "
                    "message.".format(self.routing_key)
                )

    def retry(self, body, message, reason=''):
        """
        Put the message onto the retry queue
        """
        _logger.warning(reason)
        try:
            retry_count = self.retry_count(message)
            headers = message.headers.copy()
            headers.update({
                settings.RETRY_HEADER: retry_count + 1
            })
            self.retry_producer.publish(
                body,
                headers=headers,
                retry=True,
                declares=[self.queues[self.RETRY]],
                expiration=self.backoff_func(retry_count)
            )
        except Exception as e:
            message.requeue()
            _logger.error(
                "Retry failure: retry-reason='{}' exception='{}, {}'\n{}".format(
                    reason,
                    e.__class__.__name__,
                    e,
                    traceback.format_exc(),
                )
            )

        else:
            message.ack()
            _logger.debug("Retry: {}".format(reason))

    def archive(self, body, message, reason=''):
        """
        Put the message onto the archive queue
        """
        try:
            self.archive_producer.publish(
                body,
                headers=message.headers,
                retry=True,
                declares=[self.queues[self.ARCHIVE]],
            )

        except Exception as e:
            message.requeue()
            _logger.error(
                "Archive failure: retry-reason='{}' exception='{}, {}'\n{}".format(
                    reason,
                    e.__class__.__name__,
                    e,
                    traceback.format_exc(),
                )
            )
        else:
            message.ack()
            _logger.debug("Archive: {}".format(reason))

    def declare_queues(self):
        for queue in self.queues.values():
            queue.declare()

    @classmethod
    def retry_count(cls, message):
        return message.headers.get(settings.RETRY_HEADER, 0)

    @staticmethod
    def backoff(retry_count):
        # type: (int) -> float
        """
        Given the number of attempted retries at delivering a message, return
        an increasing TTL for the message for the next retry (in seconds).
        """
        # First retry after 200 ms, then 1s, then 1m, then every 30m
        retry_delay = [0.2, 1, 60, 1800]
        try:
            return retry_delay[retry_count]
        except IndexError:
            return retry_delay[-1]
