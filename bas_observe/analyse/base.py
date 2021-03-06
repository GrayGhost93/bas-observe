"""
Abstract base implementation of an analyser class
"""
import logging
from datetime import datetime
from collections import OrderedDict
import json
import os.path

from sklearn.externals import joblib

from ..config import Config
from .. import misc, datamodel


class JsonSetEncoder(json.JSONEncoder):
    """Encodes python sets as JSON lists"""
    # from https://stackoverflow.com/a/8230505

    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        return json.JSONEncoder.default(self, obj)


class BaseAnalyser(object):
    """Abstract base implementation of an analyser class"""
    LOGGER_NAME = 'ANALYSER'

    def __init__(self, conf: Config, model: str):
        self.conf = conf
        self.model_path = model

        self.log = None
        self.channel = None
        self.influxdb = None
        self.model = None

        self._init_log()

    def _init_log(self):
        self.log = logging.getLogger(self.LOGGER_NAME)

    def get_channel(self):
        if not self.channel:
            self.channel = self.conf.get_amqp_channel()

        return self.channel

    def get_influxdb(self):
        if not self.influxdb:
            self.influxdb = self.conf.get_influxdb_connection()

        return self.influxdb

    def train(self, start: datetime, end: datetime):
        raise NotImplemented("train function is not implemented")

    def analyse(self):
        raise NotImplemented("analyse function is not implemented")

    def load_model(self):
        with open(self.model_path, mode='r') as fp:
            self.model = json.load(fp)

        return self.model

    def save_model(self):
        with open(self.model_path, mode='w') as fp:
            json.dump(self.model, fp, cls=JsonSetEncoder)

    def get_windows(self, start: datetime, end: datetime):
        windows = OrderedDict()  # {time: [window, window, ...], time: [...]}

        result = self.get_influxdb().query('SELECT * FROM "agent_status" WHERE "project" = \'{project}\' and time > \'{start}\' and time < \'{end}\' ORDER BY time DESC'.format(
            project=self.conf.project_name,
            start=misc.format_influx_datetime(start),
            end=misc.format_influx_datetime(end),
        ))

        for data in result.get_points('agent_status'):
            # construct window datamodel
            self.log.debug(data)
            window = datamodel.Window(
                misc.parse_influxdb_datetime(data['time']),
                data['agent'],
                misc.parse_influxdb_datetime(data['end'])
            )

            # fill it with the measurements
            window = self._query_measurements(window)

            key = misc.get_uncertain_date_key(windows, window.start)
            if not key:
                windows[window.start] = [window]
                self.log.debug(f"window key \"{window.start}\" does not exist yet. Gets created")
            else:
                # entry already exists, so add this row as well
                windows[key].append(window)

                # recalc key timestamp
                new_key = datetime.fromtimestamp(sum([e.start.timestamp() for e in windows[key]]) / len(windows[key]))
                self.log.info(f"File window into \"{key}\". Updated key is now \"{new_key}\"")

                windows[new_key] = windows.pop(key)

        return windows

    def _query_measurements(self, window: datamodel.Window):
        queries = []

        for measure in misc.MEASUREMENTS:
            queries.append(
                'SELECT * FROM "{measurement}" WHERE "project" = \'{project}\' and "agent" = \'{agent}\' and time = \'{time}\' LIMIT 1'.format(
                    project=self.conf.project_name,
                    agent=window.agent,
                    time=misc.format_influx_datetime(window.start),
                    measurement=measure,
                )
            )

        self.log.debug(f"Execute InfluxDB queries: \"{'; '.join(queries)}\"")
        result = self.get_influxdb().query('; '.join(queries))
        for resultset in result:
            if len(resultset.items()) <= 0:
                # no items in resultset
                self.log.warn(f"Got empty resultset for InfluxDB query\"{queries[result.index(resultset)]}\"")
                continue

            (measure, group), data = resultset.items()[0]
            data = next(data)
            # writes values to window
            setattr(window, measure, {k: v for k, v in data.items() if k not in ('time', 'project', 'agent')})

        return window


class BaseSkLearnAnalyser(BaseAnalyser):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # cache for de-pickled model files
        self._model_cache = {}

    def get_world_model(self):
        return self.get_model_for_agent('__world_model__')

    def get_model_for_agent(self, agent):
        # check if a model exists for this agent
        if agent not in self.model:
            # model is neither know nor loaded

            model = self.create_new_model()
            # store some refs
            self.model[agent] = f'{self.conf.project_name}-{self.__class__.__name__}-{agent}.joblib'
            self._model_cache[agent] = model

            return model
        elif agent in self._model_cache:
            # model is known and loaded
            return self._model_cache[agent]
        else:
            # model is known but not loaded
            try:
                model = joblib.load(os.path.join(os.path.dirname(self.model_path), self.model[agent]))
            except Exception as e:
                self.log.error(f"Error while loading sklearn model {self.model[agent]}. Generating new one.")
                model = self.create_new_model()

            self._model_cache[agent] = model
            return model

    def create_new_model(self):
        raise NotImplementedError("create_new_model is not implemented.")

    def save_model(self):
        # extend save_model to also save the LoF models
        for agent, filename in self.model.items():
            if agent in self._model_cache:
                joblib.dump(self._model_cache[agent], os.path.join(os.path.dirname(self.model_path), self.model[agent]))

        # save the json model
        super().save_model()
