import re
import time
from elasticsearch import Elasticsearch

# graphite-api and graphite-web have different logging systems
try:
    from graphite_api.intervals import Interval, IntervalSet
    from graphite_api.node import LeafNode, BranchNode
    from flask import g
    import structlog
    logger = structlog.get_logger()
except ImportError:
    from graphite.intervals import Interval, IntervalSet
    from graphite.node import LeafNode, BranchNode
    from graphite.logger import log
    import logging

    class StructlogCompat(object):
        def __init__(self):
            log.debugLogger = logging.getLogger("debug")

        @staticmethod
        def debug(*args, **kwargs):
            if not log.infoLogger.isEnabledFor(logging.DEBUG):
                return

            args_list = []
            for arg in args:
                args_list.append(arg)
            for key, value in kwargs.iteritems():
                args_list.append(key + '=' + str(value))
            return log.infoLogger.debug(' '.join(args_list))
    logger = StructlogCompat()
# uncomment following to enable debugging logger
#    log.infoLogger.setLevel(logging.DEBUG)

from influxdb import InfluxDBClient

# uncomment the following block if you suspect that logger output is often not visible for some reason, but printing works, when you ctrl-c gunicorn at least (this is still a mystery to me)

# def debug(*args, **kwargs):
#    import pprint
#    pprint.pprint((args, kwargs))
# logger.debug = debug


class NullStatsd():
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def timer(self, key, val=None):
        return self

    def timing(self, key, val):
        pass


# in case graphite-api doesn't have statsd configured,
# just use dummy one that doesn't do anything
# (or if you use graphite-web which just doesn't support statsd)
try:
    from graphite_api.app import app
    statsd = app.statsd
    assert statsd is not None
except:
    statsd = NullStatsd()

# if you want to manually set a statsd client, do this:
# from statsd import StatsClient
# statsd = StatsClient("host", 8125)


def print_time(t=None):
    """
    t unix timestamp or None
    """
    if t is None:
        t = time.time()
    return "%d (%s)" % (t, time.ctime(t))


def normalize_config(config=None):
    ret = {}
    if config is not None:
        cfg = config.get('influxdb', {})
        ret['host'] = cfg.get('host', 'localhost')
        ret['port'] = cfg.get('port', 8086)
        ret['user'] = cfg.get('user', 'graphite')
        ret['passw'] = cfg.get('pass', 'graphite')
        ret['db'] = cfg.get('db', 'graphite')
        ssl = cfg.get('ssl', False)
        ret['ssl'] = (ssl == 'true')
        ret['es'] = cfg.get('es', {})
        ret['public_org'] = cfg.get('public_org', 1)
    else:
        from django.conf import settings
        ret['host'] = getattr(settings, 'INFLUXDB_HOST', 'localhost')
        ret['port'] = getattr(settings, 'INFLUXDB_PORT', 8086)
        ret['user'] = getattr(settings, 'INFLUXDB_USER', 'graphite')
        ret['passw'] = getattr(settings, 'INFLUXDB_PASS', 'graphite')
        ret['db'] = getattr(settings, 'INFLUXDB_DB', 'graphite')
        ssl = getattr(settings, 'INFLUXDB_SSL', False)
        ret['ssl'] = (ssl == 'true')
        ret['schema'] = getattr(settings, 'INFLUXDB_SCHEMA', [])
    return ret


class InfluxdbReader(object):
    __slots__ = ('client', 'path', 'step', 'cache', 'public_org')

    def __init__(self, client, path, step, cache, public_org=None):
        self.client = client
        self.path = path
        self.step = step
        self.cache = cache
        self.public_org = public_org

    def fetch(self, start_time, end_time):
        # in graphite,
        # from is exclusive (from=foo returns data at ts=foo+1 and higher)
        # until is inclusive (until=bar returns data at ts=bar and lower)
        # influx doesn't support <= and >= yet, hence the add.
        logger.debug(caller="fetch()", start_time=start_time, end_time=end_time, step=self.step, debug_key=self.path)
        with statsd.timer('service=graphite-api.ext_service=influxdb.target_type=gauge.unit=ms.what=query_individual_duration'):
            prefix, step = InfluxdbFinder.get_prefix(start_time, end_time)
            org = g.org
            if self.path.startswith('public'):
                org = self.public_org
            series_name = "%s%s.%s" % (prefix, org, self.path)

            data = self.client.query('select time, value from "%s" where time > %ds '
                                     'and time < %ds order asc' % (
                                         series_name, start_time, end_time + 1))

        logger.debug(caller="fetch()", returned_data=data, debug_key=self.path)

        try:
            known_points = data[0]['points']
        except Exception:
            logger.debug(caller="fetch()", msg="COULDN'T READ POINTS. SETTING TO EMPTY LIST", debug_key=self.path)
            known_points = []
        logger.debug(caller="fetch()", msg="invoking fix_datapoints()", debug_key=self.path)
        datapoints = InfluxdbReader.fix_datapoints(known_points, start_time, end_time, self.step, self.path)

        time_info = start_time, end_time, self.step
        return time_info, datapoints

    @staticmethod
    def fix_datapoints_multi(data, start_time, end_time, step, series):
        out = {}
        """
        data looks like:
        [{u'columns': [u'time', u'sequence_number', u'value'],
          u'name': u'stats.timers.dfvimeoplayproxy3.varnish.miss.410.count_ps',
            u'points': [[1402928319, 1, 0.133333],
            ....
        """
        for seriesdata in data:
            if seriesdata['name'] in series:
                seriesdata['name'] =  series[seriesdata['name']]['path']
            logger.debug(caller="fix_datapoints_multi", msg="invoking fix_datapoints()", debug_key=seriesdata['name'])
            datapoints = InfluxdbReader.fix_datapoints(seriesdata['points'], start_time, end_time, step, seriesdata['name'])
            out[seriesdata['name']] = datapoints
        return out

    @staticmethod
    def fix_datapoints(known_points, start_time, end_time, step, debug_key):
        """
        points is a list of known points (potentially empty)
        """
        logger.debug(caller='fix_datapoints', len_known_points=len(known_points), debug_key=debug_key)
        if len(known_points) == 1:
            logger.debug(caller='fix_datapoints', only_known_point=known_points[0], debug_key=debug_key)
        elif len(known_points) > 1:
            logger.debug(caller='fix_datapoints', first_known_point=known_points[0], debug_key=debug_key)
            logger.debug(caller='fix_datapoints', last_known_point=known_points[-1], debug_key=debug_key)

        datapoints = []
        steps = int(round((end_time - start_time) * 1.0 / step))
        # if we have 3 datapoints: at 0, at 60 and 120, then step is 60, steps = 2 and should have 3 points
        # note that graphite assumes data at quantized intervals, whereas in influx they can be stored at like 07, 67, etc.
        ratio = len(known_points) * 1.0 / (steps + 1)
        statsd.timer('service=graphite-api.target_type=gauge.unit=none.what=known_points/needed_points', ratio)

        if len(known_points) == steps + 1:
            logger.debug(action="No steps missing", debug_key=debug_key)
            datapoints = [p[2] for p in known_points]
        else:
            amount = steps + 1 - len(known_points)
            logger.debug(action="Fill missing steps with None values", amount=amount, debug_key=debug_key)
            next_point = 0
            for s in range(0, steps + 1):
                # if we have no more known points, fill with None's
                # even ininitially when next_point = 0, len(known_points) might be == 0
                if next_point >= len(known_points):
                    datapoints.append(None)
                    continue

                # if points are not evenly spaced. i.e. they should be a minute apart but sometimes they are 55 or 65 seconds,
                # and if they are all about step/2 away from the target timestamps, then sometimes a target point has 2 candidates, and
                # sometimes 0. So a point might be more than step/2 older.  in that case, since points are sorted, we can just forward the pointer
                # influxdb's fill(null) will make this cleaner and stop us from having to worry about this.

                should_be_near = start_time + step * s
                diff = known_points[next_point][0] - should_be_near
                while next_point + 1 < len(known_points) and diff < (step / 2) * -1:
                    next_point += 1
                    diff = known_points[next_point][0] - should_be_near

                # use this point if it's within step/2 from our target
                if abs(diff) <= step / 2:
                    datapoints.append(known_points[next_point][2])
                    next_point += 1  # note: might go out of bounds, which we use as signal

                else:
                    datapoints.append(None)

        logger.debug(caller='fix_datapoints', len_known_points=len(known_points), len_datapoints=len(datapoints), debug_key=debug_key)
        logger.debug(caller='fix_datapoints', first_returned_point=datapoints[0], debug_key=debug_key)
        logger.debug(caller='fix_datapoints', last_returned_point=datapoints[-1], debug_key=debug_key)
        return datapoints

    def get_intervals(self):
            now = int(time.time())
            return IntervalSet([Interval(1, now)])


class InfluxLeafNode(LeafNode):
    __fetch_multi__ = 'influxdb'


class InfluxdbFinder(object):
    __fetch_multi__ = 'influxdb'
    __slots__ = ('client', 'schemas', 'cache', 'es', 'public_org')

    def __init__(self, config=None):
        try:
            from graphite_api.app import app
            self.cache = app.cache
        except:
            from django.core.cache import cache
            self.cache = cache

        config = normalize_config(config)
        self.client = InfluxDBClient(config['host'], config['port'], config['user'], config['passw'], config['db'], config['ssl'])
        self.es = Elasticsearch([config['es']['url']])
        self.public_org = config['public_org']

    def assure_series(self, query):
        regex = self.compile_regex(query, True)

        key_series = "%s.%s_series" % (g.org, query.pattern)
        with statsd.timer('service=graphite-api.action=cache_get_series.target_type=gauge.unit=ms'):
            series = self.cache.get(key_series)
        if series is not None:
            return series

        # if not in cache, generate from scratch
        # first we must load the list with all nodes
        with statsd.timer('service=graphite-api.ext_service=influxdb.target_type=gauge.unit=ms.action=get_series'):
            if query.pattern.startswith('public'):
                #get public only
                series = self.search_series(regex, public=True)
            elif query.pattern.startswith('*'):
                # get public and private
                series = self.search_series(regex, public=True)
                series.extend(self.search_series(regex, public=False))
            else:
                # get private only
                series = self.search_series(regex, public=False)
            
        # store in cache
        with statsd.timer('service=graphite-api.action=cache_set_series.target_type=gauge.unit=ms'):
            self.cache.add(key_series, series, timeout=300)
        return series

    def search_series(self, regex, public=False):
        org = g.org
        if public:
            org = self.public_org

        search_body = {
          "query": {
            "filtered": {
              "filter": {
                "term": {
                  "org_id": org
                  
                }
              },
              "query": {
                "regexp": {
                "name": regex.pattern
                }
              }
            }
          }
        }
        ret = self.es.search(index="definitions", doc_type="metric", body=search_body, fields=['name', 'interval'], size=1000 )
        series = []
        if len(ret["hits"]["hits"]) > 0:
            for hit in ret["hits"]["hits"] :
                series.append((hit["fields"]["name"][0], hit['fields']['interval'][0]))

        return series

    def compile_regex(self, query, series=False):
        # we turn graphite's custom glob-like thing into a regex, like so:
        # * becomes [^\.]*
        # . becomes \.

        if series:
            regex = '{0}.*'
        else:
            regex = '^{0}$'

        regex = regex.format(
            query.pattern.replace('.', '\.').replace('*', '[^\.]*').replace('{', '(').replace(',', '|').replace('}', ')')
        )
        logger.debug("searching for nodes", pattern=query.pattern, regex=regex)
        return re.compile(regex)

    def get_leaves(self, query):
        key_leaves = "%s.%s_leaves" % (g.org, query.pattern)
        with statsd.timer('service=graphite-api.action=cache_get_leaves.target_type=gauge.unit=ms'):
            data = self.cache.get(key_leaves)
        if data is not None:
            return data
        series = self.assure_series(query)
        regex = self.compile_regex(query)
        logger.debug(caller="get_leaves", key=key_leaves)
        leaves = []
        with statsd.timer('service=graphite-api.action=find_leaves.target_type=gauge.unit=ms'):
            for name, res in series:
                if regex.match(name) is not None:
                    logger.debug("found leaf", name=name)
                    leaves.append([name, res])
        with statsd.timer('service=graphite-api.action=cache_set_leaves.target_type=gauge.unit=ms'):
            self.cache.add(key_leaves, leaves, timeout=300)
        return leaves

    def get_branches(self, query):
        seen_branches = set()
        key_branches = "%s.%s_branches" % (g.org, query.pattern)
        with statsd.timer('service=graphite-api.action=cache_get_branches.target_type=gauge.unit=ms'):
            data = self.cache.get(key_branches)
        if data is not None:
            return data
        series = self.assure_series(query)
        regex = self.compile_regex(query)
        logger.debug(caller="get_branches", key=key_branches)
        branches = []
        with statsd.timer('service=graphite-api.action=find_branches.target_type=gauge.unit=ms'):
            for name, interval in series:
                while '.' in name:
                    name = name.rsplit('.', 1)[0]
                    if name not in seen_branches:
                        seen_branches.add(name)
                        if regex.match(name) is not None:
                            logger.debug("found branch", name=name)
                            branches.append(name)
        with statsd.timer('service=graphite-api.action=cache_set_branches.target_type=gauge.unit=ms'):
            self.cache.add(key_branches, branches, timeout=300)
        return branches

    def find_nodes(self, query):
        # TODO: once we can query influx better for retention periods, honor the start/end time in the FindQuery object
        with statsd.timer('service=graphite-api.action=yield_nodes.target_type=gauge.unit=ms.what=query_duration'):
            for (name, res) in self.get_leaves(query):
                yield InfluxLeafNode(name, InfluxdbReader(self.client, name, res, self.cache, self.public_org))
            for name in self.get_branches(query):
                yield BranchNode(name)

    def fetch_multi(self, nodes, start_time, end_time):
        prefix, step = InfluxdbFinder.get_prefix(start_time, end_time)
        series = {}
        for node in nodes:
            org = g.org
            if node.path.startswith('public'):
                org = self.public_org
            name = "%s%s.%s" % (prefix, org, node.path)
            series[name] = {
                "prefix": "%s%s." % (prefix, org),
                "path": node.path
            }

        series_list = ', '.join(['"%s"' % key for key in series.keys()])
        # use the step of the node that is the most coarse
        # not sure if there's a batter way? can we combine series with different steps (and use the optimal step for each?)
        # probably not
        if step is None:
            step = max([node.reader.step for node in nodes])
        query = 'select time, value from %s where time > %ds and time < %ds order asc' % (
                series_list, start_time, end_time + 1)
        logger.debug(caller='fetch_multi', query=query)
        logger.debug(caller='fetch_multi', start_time=print_time(start_time), end_time=print_time(end_time), step=step)
        with statsd.timer('service=graphite-api.ext_service=influxdb.target_type=gauge.unit=ms.action=select_datapoints'):
            data = self.client.query(query)
        logger.debug(caller='fetch_multi', returned_data=data)
        if not len(data):
            data = [{'name': name, 'points': []} for name in series.keys()]
            logger.debug(caller='fetch_multi', FIXING_DATA_TO=data)
        logger.debug(caller='fetch_multi', len_datapoints_before_fixing=len(data))

        with statsd.timer('service=graphite-api.action=fix_datapoints_multi.target_type=gauge.unit=ms'):
            logger.debug(caller='fetch_multi', action='invoking fix_datapoints_multi()')
            datapoints = InfluxdbReader.fix_datapoints_multi(data, start_time, end_time, step, series)

        time_info = start_time, end_time, step
        return time_info, datapoints
     
    @staticmethod
    def get_prefix(start_time, end_time):
        now = time.time()
        prefix = '';
        time_range = end_time - start_time
        res = None

        if (start_time < (now - 2678400)) or (time_range > 604800):
            # older then 31days or for a range of more then 7days then show 6hourly data.
            prefix = '6hour.avg.'
            res = 21600
        elif (start_time < now- 172800) or (time_range > 10800):
            #older then 7days or for a range of more then 3hours, then show 10minute data.
            prefix = '10m.avg.'
            res = 600

        #else show raw data.

        return prefix, res
