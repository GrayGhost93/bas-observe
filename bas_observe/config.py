"""
BAS OBserve

config module.
"""
import sys
import logging
import urllib.parse

from attr import attrs, attrib
import pika
import influxdb

from .queue import declare_amqp_pipeline


log = logging.getLogger('CONFIG')


@attrs
class Config(object):
    """Data class holding the main config parameters for BAS Observe

    Attributes:
        project_name        Name of the observation project. Used to determine AMQP topics and InfluxDB database
        amqp_url            URL to the AMQP/RabbitMQ server
        influxdb_url        URL to the InfluxDB server

    """

    project_name = attrib()  # type: str
    amqp_url = attrib()  # type: str
    influxdb_url = attrib()  # type: str
    # timeout between the checks, if messages can be relayed to the analysers
    relay_timeout = attrib(default=1)  # type: int
    # maximum time to wait for all agent windows to appear
    window_wait_timeout = attrib(default=4)  # type: int
    # size of thread pool
    pool_size = attrib(default=4)  # type: int

    _amqp_connection = attrib(default=None)
    _influxdb_connection = attrib(default=None)

    def parse_influxdb_url(self):
        url = urllib.parse.urlparse(self.influxdb_url)
        if url.scheme not in ('http', 'https', 'udp'):
            raise ValueError(f"Only http, https, and udp are supported as protocoll for InfluxDB, not {self.influxdb_url}")

        result = {
            'scheme': url.scheme,
            'host': url.hostname,
            'user': url.username,
            'pass': url.password,
        }
        if url.port:
            result['port'] = int(url.port)
        elif url.scheme == 'https':
            result['port'] = 443
        elif url.scheme == 'http':
            result['port'] = 8086
        elif url.scheme == 'udp':
            result['port'] = 4444

        if url.path:
            db = url.path
            if db[0] == '/':
                db = db[1:]
            if db[-1] == '/':
                db = db[:-1]

            result['db'] = db
        else:
            result['db'] = f'bob_{self.project_name}'

        return result

    def get_amqp_connection(self) -> pika.connection.Connection:
        if not self._amqp_connection:
            log.debug(f"Attemp AMQP connection to {self.amqp_url}")
            self._amqp_connection = pika.BlockingConnection(pika.URLParameters(self.amqp_url))
            log.info(f"Connected to AMQP server {self.amqp_url}")

        return self._amqp_connection

    def get_amqp_channel(self) -> pika.channel.Channel:
        connection = self.get_amqp_connection()
        log.info("Get new AMQP channel")
        channel = connection.channel()
        # just in case declare the pipelines every time a new channel is opened
        declare_amqp_pipeline(self, channel)
        return channel

    def get_influxdb_connection(self) -> influxdb.InfluxDBClient:
        if not self._influxdb_connection:
            param = self.parse_influxdb_url()
            log.debug(f"Attemp connection to InfluxDB at {self.influxdb_url}")
            self._influxdb_connection = influxdb.InfluxDBClient(
                host=param['host'],
                port=param['port'],
                ssl=True if param['scheme'] == 'https' else False,
                username=param['user'],
                password=param['pass'],
                database=param['db'],
                use_udp=True if param['scheme'] == 'udp' else False,
                udp_port=param['port']
            )
            log.info(f"Connected to InfluxDB at {self.influxdb_url}")

        return self._influxdb_connection

    @property
    def name_exchange_agents(self) -> str:
        return f'bob-{self.project_name}-exchange-agents'

    @property
    def name_queue_agents(self) -> str:
        return f'bob-{self.project_name}-queue-agents'

    @property
    def name_exchange_analyser(self) -> str:
        return f'bob-{self.project_name}-exchange-analyser'

    def _name_queue_analyser(self, name) -> str:
        return f'bob-{self.project_name}-queue-analyzer-{name}'

    @property
    def name_queue_analyser_addr(self) -> str:
        return self._name_queue_analyser('addr')

    @property
    def name_queue_analyser_entropy(self) -> str:
        return self._name_queue_analyser('entropy')

    @property
    def name_queue_analyser_lof(self) -> str:
        return self._name_queue_analyser('lof')

    @property
    def name_queue_analyser_svm(self) -> str:
        return self._name_queue_analyser('svm')


def setup_logging(level=logging.WARN, logfile=None) -> None:
    log_root = logging.getLogger()
    log_root.setLevel(level)
    log_format = logging.Formatter('%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
    # log_format = logging.Formatter('{asctime} {name: <12} {levelname: <8} {message}', style='{')

    # setting up logging to file
    if logfile:
        log_file_handler = logging.FileHandler(logfile)
        log_file_handler.setFormatter(log_format)
        log_root.addHandler(log_file_handler)

    # setting up logging to stdout
    log_stream_handler = logging.StreamHandler(sys.stdout)
    log_stream_handler.setFormatter(log_format)
    log_root.addHandler(log_stream_handler)

    # get the logger for this application
    # log = logging.getLogger('')
    # log.setLevel(logging.INFO)
