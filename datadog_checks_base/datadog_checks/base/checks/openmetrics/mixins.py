# (C) Datadog, Inc. 2018
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

from fnmatch import fnmatchcase
from math import isinf, isnan
from os.path import isfile

import requests
from prometheus_client.parser import text_fd_to_metric_families
from six import PY3, iteritems, itervalues, string_types
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

from ...config import is_affirmative
from ...errors import CheckException
from ...utils.common import to_string
from .. import AgentCheck

if PY3:
    long = int


class OpenMetricsScraperMixin(object):
    # pylint: disable=E1101
    # This class is not supposed to be used by itself, it provides scraping behavior but
    # need to be within a check in the end

    REQUESTS_CHUNK_SIZE = 1024 * 10  # use 10kb as chunk size when using the Stream feature in requests.get
    # indexes in the sample tuple of core.Metric
    SAMPLE_NAME = 0
    SAMPLE_LABELS = 1
    SAMPLE_VALUE = 2

    MINUS_INF = float("-inf")

    TELEMETRY_GAUGE_MESSAGE_SIZE = "payload.size"
    TELEMETRY_COUNTER_METRICS_BLACKLIST_COUNT = "metrics.blacklist.count"
    TELEMETRY_COUNTER_METRICS_INPUT_COUNT = "metrics.input.count"
    TELEMETRY_COUNTER_METRICS_IGNORE_COUNT = "metrics.ignored.count"
    TELEMETRY_COUNTER_METRICS_PROCESS_COUNT = "metrics.processed.count"

    METRIC_TYPES = ['counter', 'gauge', 'summary', 'histogram']

    KUBERNETES_TOKEN_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token'

    def __init__(self, *args, **kwargs):
        # Initialize AgentCheck's base class
        super(OpenMetricsScraperMixin, self).__init__(*args, **kwargs)

    def create_scraper_configuration(self, instance=None):

        # We can choose to create a default mixin configuration for an empty instance
        if instance is None:
            instance = {}

        # Create an empty configuration
        config = {}

        # Set the endpoint
        endpoint = instance.get('prometheus_url')
        if instance and endpoint is None:
            raise CheckException("You have to define a prometheus_url for each prometheus instance")

        config['prometheus_url'] = endpoint

        # `NAMESPACE` is the prefix metrics will have. Need to be hardcoded in the
        # child check class.
        namespace = instance.get('namespace')
        # Check if we have a namespace
        if instance and namespace is None:
            if self.default_namespace is None:
                raise CheckException("You have to define a namespace for each prometheus check")
            namespace = self.default_namespace

        config['namespace'] = namespace

        # Retrieve potential default instance settings for the namespace
        default_instance = self.default_instances.get(namespace, {})

        # `metrics_mapper` is a dictionary where the keys are the metrics to capture
        # and the values are the corresponding metrics names to have in datadog.
        # Note: it is empty in the parent class but will need to be
        # overloaded/hardcoded in the final check not to be counted as custom metric.

        # Metrics are preprocessed if no mapping
        metrics_mapper = {}
        # We merge list and dictionaries from optional defaults & instance settings
        metrics = default_instance.get('metrics', []) + instance.get('metrics', [])
        for metric in metrics:
            if isinstance(metric, string_types):
                metrics_mapper[metric] = metric
            else:
                metrics_mapper.update(metric)

        config['metrics_mapper'] = metrics_mapper

        # `_metrics_wildcards` holds the potential wildcards to match for metrics
        config['_metrics_wildcards'] = None

        # `prometheus_metrics_prefix` allows to specify a prefix that all
        # prometheus metrics should have. This can be used when the prometheus
        # endpoint we are scrapping allows to add a custom prefix to it's
        # metrics.
        config['prometheus_metrics_prefix'] = instance.get(
            'prometheus_metrics_prefix', default_instance.get('prometheus_metrics_prefix', '')
        )

        # `label_joins` holds the configuration for extracting 1:1 labels from
        # a target metric to all metric matching the label, example:
        # self.label_joins = {
        #     'kube_pod_info': {
        #         'label_to_match': 'pod',
        #         'labels_to_get': ['node', 'host_ip']
        #     }
        # }
        config['label_joins'] = default_instance.get('label_joins', {})
        config['label_joins'].update(instance.get('label_joins', {}))

        # `_label_mapping` holds the additionals label info to add for a specific
        # label value, example:
        # self._label_mapping = {
        #     'pod': {
        #         'dd-agent-9s1l1': [("node","yolo"),("host_ip","yey")]
        #     }
        # }
        config['_label_mapping'] = {}

        # `_active_label_mapping` holds a dictionary of label values found during the run
        # to cleanup the label_mapping of unused values, example:
        # self._active_label_mapping = {
        #     'pod': {
        #         'dd-agent-9s1l1': True
        #     }
        # }
        config['_active_label_mapping'] = {}

        # `_watched_labels` holds the list of label to watch for enrichment
        config['_watched_labels'] = set()

        config['_dry_run'] = True

        # Some metrics are ignored because they are duplicates or introduce a
        # very high cardinality. Metrics included in this list will be silently
        # skipped without a 'Unable to handle metric' debug line in the logs
        config['ignore_metrics'] = instance.get('ignore_metrics', default_instance.get('ignore_metrics', []))

        # If you want to send the buckets as tagged values when dealing with histograms,
        # set send_histograms_buckets to True, set to False otherwise.
        config['send_histograms_buckets'] = is_affirmative(
            instance.get('send_histograms_buckets', default_instance.get('send_histograms_buckets', True))
        )

        # If you want the bucket to be non cumulative and to come with upper/lower bound tags
        # set non_cumulative_buckets to True, enabled when distribution metrics are enabled.
        config['non_cumulative_buckets'] = is_affirmative(
            instance.get('non_cumulative_buckets', default_instance.get('non_cumulative_buckets', False))
        )

        # Send histograms as datadog distribution metrics
        config['send_distribution_buckets'] = is_affirmative(
            instance.get('send_distribution_buckets', default_instance.get('send_distribution_buckets', False))
        )

        # Non cumulative buckets are mandatory for distribution metrics
        if config['send_distribution_buckets'] is True:
            config['non_cumulative_buckets'] = True

        # If you want to send `counter` metrics as monotonic counts, set this value to True.
        # Set to False if you want to instead send those metrics as `gauge`.
        config['send_monotonic_counter'] = is_affirmative(
            instance.get('send_monotonic_counter', default_instance.get('send_monotonic_counter', True))
        )

        config['send_distribution_counts_as_monotonic'] = is_affirmative(
            instance.get(
                'send_distribution_counts_as_monotonic',
                default_instance.get('send_distribution_counts_as_monotonic', False),
            )
        )

        # If the `labels_mapper` dictionary is provided, the metrics labels names
        # in the `labels_mapper` will use the corresponding value as tag name
        # when sending the gauges.
        config['labels_mapper'] = default_instance.get('labels_mapper', {})
        config['labels_mapper'].update(instance.get('labels_mapper', {}))
        # Rename bucket "le" label to "upper_bound"
        config['labels_mapper']['le'] = 'upper_bound'

        # `exclude_labels` is an array of labels names to exclude. Those labels
        # will just not be added as tags when submitting the metric.
        config['exclude_labels'] = default_instance.get('exclude_labels', []) + instance.get('exclude_labels', [])

        # `type_overrides` is a dictionary where the keys are prometheus metric names
        # and the values are a metric type (name as string) to use instead of the one
        # listed in the payload. It can be used to force a type on untyped metrics.
        # Note: it is empty in the parent class but will need to be
        # overloaded/hardcoded in the final check not to be counted as custom metric.
        config['type_overrides'] = default_instance.get('type_overrides', {})
        config['type_overrides'].update(instance.get('type_overrides', {}))

        # Some metrics are retrieved from differents hosts and often
        # a label can hold this information, this transfers it to the hostname
        config['label_to_hostname'] = instance.get('label_to_hostname', default_instance.get('label_to_hostname', None))

        # In combination to label_as_hostname, allows to add a common suffix to the hostnames
        # submitted. This can be used for instance to discriminate hosts between clusters.
        config['label_to_hostname_suffix'] = instance.get(
            'label_to_hostname_suffix', default_instance.get('label_to_hostname_suffix', None)
        )

        # Add a 'health' service check for the prometheus endpoint
        config['health_service_check'] = is_affirmative(
            instance.get('health_service_check', default_instance.get('health_service_check', True))
        )

        # Can either be only the path to the certificate and thus you should specify the private key
        # or it can be the path to a file containing both the certificate & the private key
        config['ssl_cert'] = instance.get('ssl_cert', default_instance.get('ssl_cert', None))

        # Needed if the certificate does not include the private key
        #
        # /!\ The private key to your local certificate must be unencrypted.
        # Currently, Requests does not support using encrypted keys.
        config['ssl_private_key'] = instance.get('ssl_private_key', default_instance.get('ssl_private_key', None))

        # The path to the trusted CA used for generating custom certificates
        config['ssl_ca_cert'] = instance.get('ssl_ca_cert', default_instance.get('ssl_ca_cert', None))

        # Whether or not to validate SSL certificates
        config['ssl_verify'] = is_affirmative(instance.get('ssl_verify', default_instance.get('ssl_verify', True)))

        # Extra http headers to be sent when polling endpoint
        config['extra_headers'] = default_instance.get('extra_headers', {})
        config['extra_headers'].update(instance.get('extra_headers', {}))

        # Timeout used during the network request
        config['prometheus_timeout'] = instance.get(
            'prometheus_timeout', default_instance.get('prometheus_timeout', 10)
        )

        # Authentication used when polling endpoint
        config['username'] = instance.get('username', default_instance.get('username', None))
        config['password'] = instance.get('password', default_instance.get('password', None))

        # Custom tags that will be sent with each metric
        config['custom_tags'] = instance.get('tags', [])

        # Additional tags to be sent with each metric
        config['_metric_tags'] = []

        # List of strings to filter the input text payload on. If any line contains
        # one of these strings, it will be filtered out before being parsed.
        # INTERNAL FEATURE, might be removed in future versions
        config['_text_filter_blacklist'] = []

        # Whether or not to use the service account bearer token for authentication
        # if 'bearer_token_path' is not set, we use /var/run/secrets/kubernetes.io/serviceaccount/token
        # as a default path to get the token.
        config['bearer_token_auth'] = is_affirmative(
            instance.get('bearer_token_auth', default_instance.get('bearer_token_auth', False))
        )

        # Can be used to get a service account bearer token from files
        # other than /var/run/secrets/kubernetes.io/serviceaccount/token
        # 'bearer_token_auth' should be enabled.
        config['bearer_token_path'] = instance.get('bearer_token_path', default_instance.get('bearer_token_path', None))

        # The service account bearer token to be used for authentication
        config['_bearer_token'] = self._get_bearer_token(config['bearer_token_auth'], config['bearer_token_path'])

        config['telemetry'] = is_affirmative(instance.get('telemetry', default_instance.get('telemetry', False)))

        # The metric name services use to indicate build information
        config['metadata_metric_name'] = instance.get(
            'metadata_metric_name', default_instance.get('metadata_metric_name')
        )

        # Map of metadata key names to label names
        config['metadata_label_map'] = instance.get(
            'metadata_label_map', default_instance.get('metadata_label_map', {})
        )

        config['_default_metric_transformers'] = {}
        if config['metadata_metric_name'] and config['metadata_label_map']:
            config['_default_metric_transformers'][config['metadata_metric_name']] = self.transform_metadata

        return config

    def parse_metric_family(self, response, scraper_config):
        """
        Parse the MetricFamily from a valid requests.Response object to provide a MetricFamily object (see [0])
        The text format uses iter_lines() generator.
        :param response: requests.Response
        :return: core.Metric
        """
        input_gen = response.iter_lines(chunk_size=self.REQUESTS_CHUNK_SIZE, decode_unicode=True)
        if scraper_config['_text_filter_blacklist']:
            input_gen = self._text_filter_input(input_gen, scraper_config)

        for metric in text_fd_to_metric_families(input_gen):
            self._send_telemetry_counter(
                self.TELEMETRY_COUNTER_METRICS_INPUT_COUNT, len(metric.samples), scraper_config
            )
            metric.type = scraper_config['type_overrides'].get(metric.name, metric.type)
            if metric.type not in self.METRIC_TYPES:
                continue
            metric.name = self._remove_metric_prefix(metric.name, scraper_config)
            yield metric

    def _text_filter_input(self, input_gen, scraper_config):
        """
        Filters out the text input line by line to avoid parsing and processing
        metrics we know we don't want to process. This only works on `text/plain`
        payloads, and is an INTERNAL FEATURE implemented for the kubelet check
        :param input_get: line generator
        :output: generator of filtered lines
        """
        for line in input_gen:
            for item in scraper_config['_text_filter_blacklist']:
                if item in line:
                    self._send_telemetry_counter(self.TELEMETRY_COUNTER_METRICS_BLACKLIST_COUNT, 1, scraper_config)
                    break
            else:
                # No blacklist matches, passing the line through
                yield line

    def _remove_metric_prefix(self, metric, scraper_config):
        prometheus_metrics_prefix = scraper_config['prometheus_metrics_prefix']
        return metric[len(prometheus_metrics_prefix) :] if metric.startswith(prometheus_metrics_prefix) else metric

    def scrape_metrics(self, scraper_config):
        """
        Poll the data from prometheus and return the metrics as a generator.
        """
        response = self.poll(scraper_config)
        if scraper_config['telemetry']:
            if 'content-length' in response.headers:
                content_len = int(response.headers['content-length'])
            else:
                content_len = len(response.content)
            self._send_telemetry_gauge(self.TELEMETRY_GAUGE_MESSAGE_SIZE, content_len, scraper_config)
        try:
            # no dry run if no label joins
            if not scraper_config['label_joins']:
                scraper_config['_dry_run'] = False
            elif not scraper_config['_watched_labels']:
                # build the _watched_labels set
                for val in itervalues(scraper_config['label_joins']):
                    scraper_config['_watched_labels'].add(val['label_to_match'])

            for metric in self.parse_metric_family(response, scraper_config):
                yield metric

            # Set dry run off
            scraper_config['_dry_run'] = False
            # Garbage collect unused mapping and reset active labels
            for metric, mapping in list(iteritems(scraper_config['_label_mapping'])):
                for key in list(mapping):
                    if key not in scraper_config['_active_label_mapping'][metric]:
                        del scraper_config['_label_mapping'][metric][key]
            scraper_config['_active_label_mapping'] = {}
        finally:
            response.close()

    def process(self, scraper_config, metric_transformers=None):
        """
        Polls the data from prometheus and pushes them as gauges
        `endpoint` is the metrics endpoint to use to poll metrics from Prometheus

        Note that if the instance has a 'tags' attribute, it will be pushed
        automatically as additional custom tags and added to the metrics
        """
        transformers = scraper_config['_default_metric_transformers'].copy()
        if metric_transformers:
            transformers.update(metric_transformers)

        for metric in self.scrape_metrics(scraper_config):
            self.process_metric(metric, scraper_config, metric_transformers=transformers)

    def transform_metadata(self, metric, scraper_config):
        labels = metric.samples[0][self.SAMPLE_LABELS]
        for metadata_name, label_name in iteritems(scraper_config['metadata_label_map']):
            if label_name in labels:
                self.set_metadata(metadata_name, labels[label_name])

    def _telemetry_metric_name_with_namespace(self, metric_name, scraper_config):
        return '{}.{}.{}'.format(scraper_config['namespace'], 'telemetry', metric_name)

    def _send_telemetry_gauge(self, metric_name, val, scraper_config):
        if scraper_config['telemetry']:
            metric_name_with_namespace = self._telemetry_metric_name_with_namespace(metric_name, scraper_config)
            # Determine the tags to send
            custom_tags = scraper_config['custom_tags']
            tags = list(custom_tags)
            tags.extend(scraper_config['_metric_tags'])
            self.gauge(metric_name_with_namespace, val, tags=tags)

    def _send_telemetry_counter(self, metric_name, val, scraper_config, extra_tags=None):
        if scraper_config['telemetry']:
            metric_name_with_namespace = self._telemetry_metric_name_with_namespace(metric_name, scraper_config)
            # Determine the tags to send
            custom_tags = scraper_config['custom_tags']
            tags = list(custom_tags)
            tags.extend(scraper_config['_metric_tags'])
            if extra_tags:
                tags.extend(extra_tags)
            self.count(metric_name_with_namespace, val, tags=tags)

    def _store_labels(self, metric, scraper_config):
        # If targeted metric, store labels
        if metric.name in scraper_config['label_joins']:
            matching_label = scraper_config['label_joins'][metric.name]['label_to_match']
            for sample in metric.samples:
                # metadata-only metrics that are used for label joins are always equal to 1
                # this is required for metrics where all combinations of a state are sent
                # but only the active one is set to 1 (others are set to 0)
                # example: kube_pod_status_phase in kube-state-metrics
                if sample[self.SAMPLE_VALUE] != 1:
                    continue
                label_dict = dict()
                matching_value = None
                for label_name, label_value in iteritems(sample[self.SAMPLE_LABELS]):
                    if label_name == matching_label:
                        matching_value = label_value
                    elif label_name in scraper_config['label_joins'][metric.name]['labels_to_get']:
                        label_dict[label_name] = label_value
                try:
                    if scraper_config['_label_mapping'][matching_label].get(matching_value):
                        scraper_config['_label_mapping'][matching_label][matching_value].update(label_dict)
                    else:
                        scraper_config['_label_mapping'][matching_label][matching_value] = label_dict
                except KeyError:
                    if matching_value is not None:
                        scraper_config['_label_mapping'][matching_label] = {matching_value: label_dict}

    def _join_labels(self, metric, scraper_config):
        # Filter metric to see if we can enrich with joined labels
        if scraper_config['label_joins']:
            for sample in metric.samples:
                watched_labels = scraper_config['_watched_labels'].intersection(set(sample[self.SAMPLE_LABELS].keys()))
                for label_name in watched_labels:
                    # Set this label value as active
                    if label_name not in scraper_config['_active_label_mapping']:
                        scraper_config['_active_label_mapping'][label_name] = {}
                    scraper_config['_active_label_mapping'][label_name][sample[self.SAMPLE_LABELS][label_name]] = True
                    # If mapping found add corresponding labels
                    try:
                        for name, val in iteritems(
                            scraper_config['_label_mapping'][label_name][sample[self.SAMPLE_LABELS][label_name]]
                        ):
                            sample[self.SAMPLE_LABELS][name] = val
                    except KeyError:
                        pass

    def process_metric(self, metric, scraper_config, metric_transformers=None):
        """
        Handle a prometheus metric according to the following flow:
            - search scraper_config['metrics_mapper'] for a prometheus.metric <--> datadog.metric mapping
            - call check method with the same name as the metric
            - log some info if none of the above worked

        `metric_transformers` is a dict of <metric name>:<function to run when the metric name is encountered>
        """
        # If targeted metric, store labels
        self._store_labels(metric, scraper_config)

        if metric.name in scraper_config['ignore_metrics']:
            self._send_telemetry_counter(
                self.TELEMETRY_COUNTER_METRICS_IGNORE_COUNT, len(metric.samples), scraper_config
            )
            return  # Ignore the metric

        self._send_telemetry_counter(self.TELEMETRY_COUNTER_METRICS_PROCESS_COUNT, len(metric.samples), scraper_config)

        if self._filter_metric(metric, scraper_config):
            return  # Ignore the metric

        # Filter metric to see if we can enrich with joined labels
        self._join_labels(metric, scraper_config)

        if scraper_config['_dry_run']:
            return

        try:
            self.submit_openmetric(scraper_config['metrics_mapper'][metric.name], metric, scraper_config)
        except KeyError:
            if metric_transformers is not None and metric.name in metric_transformers:
                try:
                    # Get the transformer function for this specific metric
                    transformer = metric_transformers[metric.name]
                    transformer(metric, scraper_config)
                except Exception as err:
                    self.log.warning('Error handling metric: %s - error: %s', metric.name, err)

                return

            # build the wildcard list if first pass
            if scraper_config['_metrics_wildcards'] is None:
                scraper_config['_metrics_wildcards'] = [x for x in scraper_config['metrics_mapper'] if '*' in x]

            # try matching wildcard
            for wildcard in scraper_config['_metrics_wildcards']:
                if fnmatchcase(metric.name, wildcard):
                    self.submit_openmetric(metric.name, metric, scraper_config)
                    return

            self.log.debug(
                'Skipping metric `%s` as it is not defined in the metrics mapper, '
                'has no transformer function, nor does it match any wildcards.',
                metric.name,
            )

    def poll(self, scraper_config, headers=None):
        """
        Custom headers can be added to the default headers.

        Returns a valid requests.Response, raise requests.HTTPError if the status code of the requests.Response
        isn't valid - see response.raise_for_status()

        The caller needs to close the requests.Response

        :param endpoint: string url endpoint
        :param headers: extra headers
        :return: requests.Response
        """
        endpoint = scraper_config.get('prometheus_url')

        # Should we send a service check for when we make a request
        health_service_check = scraper_config['health_service_check']
        service_check_name = '{}{}'.format(scraper_config['namespace'], '.prometheus.health')
        service_check_tags = ['endpoint:{}'.format(endpoint)]
        service_check_tags.extend(scraper_config['custom_tags'])

        try:
            response = self.send_request(endpoint, scraper_config, headers)
        except requests.exceptions.SSLError:
            self.log.error("Invalid SSL settings for requesting %s endpoint", endpoint)
            raise
        except IOError:
            if health_service_check:
                self.service_check(service_check_name, AgentCheck.CRITICAL, tags=service_check_tags)
            raise
        try:
            response.raise_for_status()
            if health_service_check:
                self.service_check(service_check_name, AgentCheck.OK, tags=service_check_tags)
            return response
        except requests.HTTPError:
            response.close()
            if health_service_check:
                self.service_check(service_check_name, AgentCheck.CRITICAL, tags=service_check_tags)
            raise

    def send_request(self, endpoint, scraper_config, headers=None):
        # Determine the headers
        if headers is None:
            headers = {}
        if 'accept-encoding' not in headers:
            headers['accept-encoding'] = 'gzip'
        headers.update(scraper_config['extra_headers'])

        # Add the bearer token to headers
        bearer_token = scraper_config['_bearer_token']
        if bearer_token is not None:
            auth_header = {'Authorization': 'Bearer {}'.format(bearer_token)}
            headers.update(auth_header)

        # Determine the SSL verification settings
        cert = None
        if isinstance(scraper_config['ssl_cert'], string_types):
            if isinstance(scraper_config['ssl_private_key'], string_types):
                cert = (scraper_config['ssl_cert'], scraper_config['ssl_private_key'])
            else:
                cert = scraper_config['ssl_cert']

        verify = scraper_config['ssl_verify']
        # TODO: deprecate use as `ssl_verify` boolean
        if scraper_config['ssl_ca_cert'] is False:
            verify = False

        if isinstance(scraper_config['ssl_ca_cert'], string_types):
            verify = scraper_config['ssl_ca_cert']
        elif verify is False:
            disable_warnings(InsecureRequestWarning)

        # Determine the authentication settings
        username = scraper_config['username']
        password = scraper_config['password']
        auth = (username, password) if username is not None and password is not None else None

        return requests.get(
            endpoint,
            headers=headers,
            stream=True,
            timeout=scraper_config['prometheus_timeout'],
            cert=cert,
            verify=verify,
            auth=auth,
        )

    def get_hostname_for_sample(self, sample, scraper_config):
        """
        Expose the label_to_hostname mapping logic to custom handler methods
        """
        return self._get_hostname(None, sample, scraper_config)

    def submit_openmetric(self, metric_name, metric, scraper_config, hostname=None):
        """
        For each sample in the metric, report it as a gauge with all labels as tags
        except if a labels dict is passed, in which case keys are label names we'll extract
        and corresponding values are tag names we'll use (eg: {'node': 'node'}).

        Histograms generate a set of values instead of a unique metric.
        send_histograms_buckets is used to specify if yes or no you want to
            send the buckets as tagged values when dealing with histograms.

        `custom_tags` is an array of 'tag:value' that will be added to the
        metric when sending the gauge to Datadog.
        """
        if metric.type in ["gauge", "counter", "rate"]:
            metric_name_with_namespace = '{}.{}'.format(scraper_config['namespace'], metric_name)
            for sample in metric.samples:
                val = sample[self.SAMPLE_VALUE]
                if not self._is_value_valid(val):
                    self.log.debug("Metric value is not supported for metric %s", sample[self.SAMPLE_NAME])
                    continue
                custom_hostname = self._get_hostname(hostname, sample, scraper_config)
                # Determine the tags to send
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname=custom_hostname)
                if metric.type == "counter" and scraper_config['send_monotonic_counter']:
                    self.monotonic_count(metric_name_with_namespace, val, tags=tags, hostname=custom_hostname)
                elif metric.type == "rate":
                    self.rate(metric_name_with_namespace, val, tags=tags, hostname=custom_hostname)
                else:
                    self.gauge(metric_name_with_namespace, val, tags=tags, hostname=custom_hostname)
        elif metric.type == "histogram":
            self._submit_gauges_from_histogram(metric_name, metric, scraper_config)
        elif metric.type == "summary":
            self._submit_gauges_from_summary(metric_name, metric, scraper_config)
        else:
            self.log.error("Metric type %s unsupported for metric %s.", metric.type, metric_name)

    def _get_hostname(self, hostname, sample, scraper_config):
        """
        If hostname is None, look at label_to_hostname setting
        """
        if (
            hostname is None
            and scraper_config['label_to_hostname'] is not None
            and scraper_config['label_to_hostname'] in sample[self.SAMPLE_LABELS]
        ):
            hostname = sample[self.SAMPLE_LABELS][scraper_config['label_to_hostname']]
            suffix = scraper_config['label_to_hostname_suffix']
            if suffix is not None:
                hostname += suffix

        return hostname

    def _submit_gauges_from_summary(self, metric_name, metric, scraper_config, hostname=None):
        """
        Extracts metrics from a prometheus summary metric and sends them as gauges
        """
        for sample in metric.samples:
            val = sample[self.SAMPLE_VALUE]
            if not self._is_value_valid(val):
                self.log.debug("Metric value is not supported for metric %s", sample[self.SAMPLE_NAME])
                continue
            custom_hostname = self._get_hostname(hostname, sample, scraper_config)
            if sample[self.SAMPLE_NAME].endswith("_sum"):
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname=custom_hostname)
                self.gauge(
                    "{}.{}.sum".format(scraper_config['namespace'], metric_name),
                    val,
                    tags=tags,
                    hostname=custom_hostname,
                )
            elif sample[self.SAMPLE_NAME].endswith("_count"):
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname=custom_hostname)
                self._submit_distribution_count(
                    scraper_config['send_distribution_counts_as_monotonic'],
                    "{}.{}.count".format(scraper_config['namespace'], metric_name),
                    val,
                    tags=tags,
                    hostname=custom_hostname,
                )
            else:
                sample[self.SAMPLE_LABELS]["quantile"] = str(float(sample[self.SAMPLE_LABELS]["quantile"]))
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname=custom_hostname)
                self.gauge(
                    "{}.{}.quantile".format(scraper_config['namespace'], metric_name),
                    val,
                    tags=tags,
                    hostname=custom_hostname,
                )

    def _submit_gauges_from_histogram(self, metric_name, metric, scraper_config, hostname=None):
        """
        Extracts metrics from a prometheus histogram and sends them as gauges
        """
        if scraper_config['non_cumulative_buckets']:
            self._decumulate_histogram_buckets(metric)
        for sample in metric.samples:
            val = sample[self.SAMPLE_VALUE]
            if not self._is_value_valid(val):
                self.log.debug("Metric value is not supported for metric %s", sample[self.SAMPLE_NAME])
                continue
            custom_hostname = self._get_hostname(hostname, sample, scraper_config)
            if sample[self.SAMPLE_NAME].endswith("_sum") and not scraper_config['send_distribution_buckets']:
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname)
                self.gauge(
                    "{}.{}.sum".format(scraper_config['namespace'], metric_name),
                    val,
                    tags=tags,
                    hostname=custom_hostname,
                )
            elif sample[self.SAMPLE_NAME].endswith("_count") and not scraper_config['send_distribution_buckets']:
                tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname)
                if scraper_config['send_histograms_buckets']:
                    tags.append("upper_bound:none")
                self._submit_distribution_count(
                    scraper_config['send_distribution_counts_as_monotonic'],
                    "{}.{}.count".format(scraper_config['namespace'], metric_name),
                    val,
                    tags=tags,
                    hostname=custom_hostname,
                )
            elif scraper_config['send_histograms_buckets'] and sample[self.SAMPLE_NAME].endswith("_bucket"):
                if scraper_config['send_distribution_buckets']:
                    self._submit_sample_histogram_buckets(metric_name, sample, scraper_config, hostname)
                elif "Inf" not in sample[self.SAMPLE_LABELS]["le"] or scraper_config['non_cumulative_buckets']:
                    sample[self.SAMPLE_LABELS]["le"] = str(float(sample[self.SAMPLE_LABELS]["le"]))
                    tags = self._metric_tags(metric_name, val, sample, scraper_config, hostname)
                    self._submit_distribution_count(
                        scraper_config['send_distribution_counts_as_monotonic'],
                        "{}.{}.count".format(scraper_config['namespace'], metric_name),
                        val,
                        tags=tags,
                        hostname=custom_hostname,
                    )

    def _compute_bucket_hash(self, tags):
        # we need the unique context for all the buckets
        # hence we remove the "le" tag
        return hash(frozenset(sorted((k, v) for k, v in iteritems(tags) if k != 'le')))

    def _decumulate_histogram_buckets(self, metric):
        """
        Decumulate buckets in a given histogram metric and adds the lower_bound label (le being upper_bound)
        """
        bucket_values_by_context_upper_bound = {}
        for sample in metric.samples:
            if sample[self.SAMPLE_NAME].endswith("_bucket"):
                context_key = self._compute_bucket_hash(sample[self.SAMPLE_LABELS])
                if context_key not in bucket_values_by_context_upper_bound:
                    bucket_values_by_context_upper_bound[context_key] = {}
                bucket_values_by_context_upper_bound[context_key][float(sample[self.SAMPLE_LABELS]["le"])] = sample[
                    self.SAMPLE_VALUE
                ]

        sorted_buckets_by_context = {}
        for context in bucket_values_by_context_upper_bound:
            sorted_buckets_by_context[context] = sorted(bucket_values_by_context_upper_bound[context])

        # Tuples (lower_bound, upper_bound, value)
        bucket_tuples_by_context_upper_bound = {}
        for context in sorted_buckets_by_context:
            for i, upper_b in enumerate(sorted_buckets_by_context[context]):
                if i == 0:
                    if context not in bucket_tuples_by_context_upper_bound:
                        bucket_tuples_by_context_upper_bound[context] = {}
                    if upper_b > 0:
                        # positive buckets start at zero
                        bucket_tuples_by_context_upper_bound[context][upper_b] = (
                            0,
                            upper_b,
                            bucket_values_by_context_upper_bound[context][upper_b],
                        )
                    else:
                        # negative buckets start at -inf
                        bucket_tuples_by_context_upper_bound[context][upper_b] = (
                            self.MINUS_INF,
                            upper_b,
                            bucket_values_by_context_upper_bound[context][upper_b],
                        )
                    continue
                tmp = (
                    bucket_values_by_context_upper_bound[context][upper_b]
                    - bucket_values_by_context_upper_bound[context][sorted_buckets_by_context[context][i - 1]]
                )
                bucket_tuples_by_context_upper_bound[context][upper_b] = (
                    sorted_buckets_by_context[context][i - 1],
                    upper_b,
                    tmp,
                )

        # modify original metric to inject lower_bound & modified value
        for i, sample in enumerate(metric.samples):
            if not sample[self.SAMPLE_NAME].endswith("_bucket"):
                continue

            context_key = self._compute_bucket_hash(sample[self.SAMPLE_LABELS])
            matching_bucket_tuple = bucket_tuples_by_context_upper_bound[context_key][
                float(sample[self.SAMPLE_LABELS]["le"])
            ]
            # Replacing the sample tuple
            sample[self.SAMPLE_LABELS]["lower_bound"] = str(matching_bucket_tuple[0])
            metric.samples[i] = (sample[self.SAMPLE_NAME], sample[self.SAMPLE_LABELS], matching_bucket_tuple[2])

    def _submit_sample_histogram_buckets(self, metric_name, sample, scraper_config, hostname=None):
        if "lower_bound" not in sample[self.SAMPLE_LABELS] or "le" not in sample[self.SAMPLE_LABELS]:
            self.log.warning(
                "Metric: %s was not containing required bucket boundaries labels: %s",
                metric_name,
                sample[self.SAMPLE_LABELS],
            )
            return
        sample[self.SAMPLE_LABELS]["le"] = str(float(sample[self.SAMPLE_LABELS]["le"]))
        sample[self.SAMPLE_LABELS]["lower_bound"] = str(float(sample[self.SAMPLE_LABELS]["lower_bound"]))
        if sample[self.SAMPLE_LABELS]["le"] == sample[self.SAMPLE_LABELS]["lower_bound"]:
            # this can happen for -inf/-inf bucket that we don't want to send (always 0)
            self.log.warning(
                "Metric: %s has bucket boundaries equal, skipping: %s", metric_name, sample[self.SAMPLE_LABELS]
            )
            return
        tags = self._metric_tags(metric_name, sample[self.SAMPLE_VALUE], sample, scraper_config, hostname)
        self.submit_histogram_bucket(
            "{}.{}".format(scraper_config['namespace'], metric_name),
            sample[self.SAMPLE_VALUE],
            float(sample[self.SAMPLE_LABELS]["lower_bound"]),
            float(sample[self.SAMPLE_LABELS]["le"]),
            True,
            hostname,
            tags,
        )

    def _submit_distribution_count(self, monotonic, metric_name, value, tags=None, hostname=None):
        if monotonic:
            self.monotonic_count(metric_name, value, tags=tags, hostname=hostname)
        else:
            self.gauge(metric_name, value, tags=tags, hostname=hostname)

    def _metric_tags(self, metric_name, val, sample, scraper_config, hostname=None):
        custom_tags = scraper_config['custom_tags']
        _tags = list(custom_tags)
        _tags.extend(scraper_config['_metric_tags'])
        for label_name, label_value in iteritems(sample[self.SAMPLE_LABELS]):
            if label_name not in scraper_config['exclude_labels']:
                tag_name = scraper_config['labels_mapper'].get(label_name, label_name)
                _tags.append('{}:{}'.format(to_string(tag_name), to_string(label_value)))
        return self._finalize_tags_to_submit(
            _tags, metric_name, val, sample, custom_tags=custom_tags, hostname=hostname
        )

    def _is_value_valid(self, val):
        return not (isnan(val) or isinf(val))

    def _get_bearer_token(self, bearer_token_auth, bearer_token_path):
        if bearer_token_auth is False:
            return None

        path = None
        if bearer_token_path is not None:
            if isfile(bearer_token_path):
                path = bearer_token_path
            else:
                self.log.error("File not found: %s", bearer_token_path)
        elif isfile(self.KUBERNETES_TOKEN_PATH):
            path = self.KUBERNETES_TOKEN_PATH

        if path is None:
            self.log.error("Cannot get bearer token from bearer_token_path or auto discovery")
            raise IOError("Cannot get bearer token from bearer_token_path or auto discovery")

        try:
            with open(path, 'r') as f:
                return f.read().rstrip()
        except Exception as err:
            self.log.error("Cannot get bearer token from path: %s - error: %s", path, err)
            raise
