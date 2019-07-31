import ast
import json
import arrow
import ciso8601
import pytz  # NOQA
import logging
import elasticsearch

from bson import ObjectId
from elasticsearch.helpers import bulk, reindex as reindex_new
from .helpers import reindex as reindex_old

from uuid import uuid4
from flask import request, abort
from eve.utils import config
from eve.io.base import DataLayer
from eve.io.mongo.parser import parse, ParseError


logging.basicConfig()
logger = logging.getLogger("elastic")


def parse_date(date_str):
    """Parse elastic datetime string."""
    if not date_str:
        return None

    try:
        date = ciso8601.parse_datetime(date_str)
        if not date:
            date = arrow.get(date_str).datetime
    except TypeError:
        date = arrow.get(date_str[0]).datetime
    return date


def get_dates(schema):
    """Return list of datetime fields for given schema."""
    dates = [config.LAST_UPDATED, config.DATE_CREATED]
    for field, field_schema in schema.items():
        if field_schema["type"] == "datetime":
            dates.append(field)
    return dates


def format_doc(hit, schema, dates):
    """Format given doc to match given schema."""
    doc = hit.get("_source", {})
    doc.setdefault(config.ID_FIELD, hit.get("_id"))
    doc.setdefault("_type", hit.get("_type"))
    if hit.get("highlight"):
        doc["es_highlight"] = hit.get("highlight")

    if hit.get("inner_hits"):
        doc["_inner_hits"] = {}
        for key, value in hit.get("inner_hits").items():
            doc["_inner_hits"][key] = []
            for item in value.get("hits", {}).get("hits", []):
                doc["_inner_hits"][key].append(item.get("_source", {}))

    for key in dates:
        if key in doc:
            doc[key] = parse_date(doc[key])

    return doc


def test_settings_contain(current_settings, new_settings):
    """Test if current settings contain everything from new settings."""
    try:
        for key, val in new_settings.items():
            if isinstance(val, dict):
                if not test_settings_contain(current_settings[key], val):
                    return False
            elif val != current_settings[key]:
                return False
        return True
    except KeyError:
        return False


def noop():
    pass


def is_elastic(datasource):
    """Detect if given resource uses elastic."""
    return (
        datasource.get("backend") == "elastic"
        or datasource.get("search_backend") == "elastic"
    )


def generate_index_name(alias):
    random = str(uuid4()).split("-")[0]
    return "{}_{}".format(alias, random)


def reindex(es, source, dest):
    version = es.info().get("version").get("number")
    if version.startswith("1."):
        return reindex_old(es, source, dest)
    else:
        return reindex_new(es, source, dest)


def fix_old_mapping(mapping):
    if mapping.get("type") == "string" and mapping.get("index") == "not_analyzed":
        mapping["type"] = "keyword"
        mapping.pop("index")
    elif mapping.get("type") == "string":
        mapping["type"] = "text"
    return mapping


class InvalidSearchString(Exception):
    """Exception thrown when search string has invalid value"""

    pass


class InvalidIndexSettings(Exception):
    """Exception is thrown when put_settings is called without ELASTIC_SETTINGS"""

    pass


class ElasticJSONSerializer(elasticsearch.JSONSerializer):
    """Customize the JSON serializer used in Elastic."""

    def default(self, value):
        """Convert mongo.ObjectId."""
        if isinstance(value, ObjectId):
            return str(value)
        return super(ElasticJSONSerializer, self).default(value)


class ElasticCursor(object):
    """Search results cursor."""

    no_hits = {"hits": {"total": 0, "hits": []}}

    def __init__(self, hits=None, docs=None):
        """Parse hits into docs."""
        self.hits = hits if hits else self.no_hits
        self.docs = docs if docs else []

    def __getitem__(self, key):
        return self.docs[key]

    def first(self):
        """Get first doc."""
        return self.docs[0] if self.docs else None

    def count(self, **kwargs):
        """Get hits count."""
        hits = self.hits.get("hits")
        if hits:
            total = hits.get("total")
            if total and total.get("value"):
                return int(total["value"])
        return 0

    def extra(self, response):
        """Add extra info to response."""
        if "facets" in self.hits:
            response["_facets"] = self.hits["facets"]
        if "aggregations" in self.hits:
            response["_aggregations"] = self.hits["aggregations"]


def set_filters(query, filters):
    """Put together all filters we have and set them as 'and' filter
    within filtered query.

    :param query: elastic query being constructed
    :param base_filters: all filters set outside of query (eg. resource config, sub_resource_lookup)
    """
    query["query"].setdefault("bool", {})
    if filters:
        for f in filters:
            if f is not None:
                query["query"]["bool"].setdefault("must", []).append(f)


def set_sort(query, sort):
    query["sort"] = []
    for (key, sortdir) in sort:
        sort_dict = dict([(key, "asc" if sortdir > 0 else "desc")])
        query["sort"].append(sort_dict)


def get_es(url, **kwargs):
    """Create elasticsearch client instance.

    :param url: elasticsearch url
    """
    urls = [url] if isinstance(url, str) else url
    kwargs.setdefault("serializer", ElasticJSONSerializer())
    es = elasticsearch.Elasticsearch(urls, **kwargs)
    return es


def get_indices(es):
    return es.indices


class Elastic(DataLayer):
    """ElasticSearch data layer."""

    serializers = {"integer": int, "datetime": parse_date, "objectid": ObjectId}

    def __init__(self, app=None, **kwargs):
        """Let user specify extra arguments for Elasticsearch"""
        self.es = None
        self.app = app
        self.index = None
        self.kwargs = kwargs
        self.elastics = {}
        super(Elastic, self).__init__(app)

    def init_app(self, app):
        app.config.setdefault("ELASTICSEARCH_URL", "http://localhost:9200/")
        app.config.setdefault("ELASTICSEARCH_INDEX", "eve")
        app.config.setdefault("ELASTICSEARCH_INDEXES", {})
        app.config.setdefault("ELASTICSEARCH_FORCE_REFRESH", True)
        app.config.setdefault("ELASTICSEARCH_AUTO_AGGREGATIONS", True)

        self.app = app
        self.index = app.config["ELASTICSEARCH_INDEX"]
        self.es = get_es(app.config["ELASTICSEARCH_URL"], **self.kwargs)

    def init_index(self):
        """Create indexes and put mapping."""
        for resource in self._get_elastic_resources():
            es = self.elastic(resource)
            index = self._resource_index(resource)
            settings = self._resource_config(resource, "SETTINGS")
            mappings = self._resource_mapping(resource)
            self._init_index(es, index, settings, mappings)

    def _init_index(self, es, index, settings=None, mappings=None):
        if not es.indices.exists(index):
            self._create_index(es, index, settings, mappings)
        else:
            if settings:
                self._put_settings(es, index, settings)
            if mappings:
                self._put_mappings(es, index, mappings)

    def get_datasource(self, resource):
        return getattr(self, "_datasource", self.datasource)(resource)

    def _get_mapping(self, schema):
        """Get mapping for given resource or item schema.

        :param schema: resource or dict/list type item schema
        """
        properties = {}
        for field, field_schema in schema.items():
            field_mapping = self._get_field_mapping(field_schema)
            if field_mapping:
                properties[field] = field_mapping
        return {"properties": properties}

    def _get_field_mapping(self, schema):
        """Get mapping for single field schema.

        :param schema: field schema
        """
        if "mapping" in schema:
            return fix_old_mapping(schema["mapping"])
        elif schema["type"] == "dict" and "schema" in schema:
            return self._get_mapping(schema["schema"])
        elif schema["type"] == "list" and "schema" in schema.get("schema", {}):
            return self._get_mapping(schema["schema"]["schema"])
        elif schema["type"] == "datetime":
            return {"type": "date"}
        elif schema["type"] == "string" and schema.get("unique"):
            return {"type": "keyword"}
        elif schema["type"] == "string":
            return {"type": "text"}

    def _create_index(self, es, index, settings=None, mappings=None):
        """Create new index and ignore if it exists already."""
        try:
            alias = index
            index = generate_index_name(alias)

            args = {"index": index, "body": {}}

            if settings:
                args["body"].update(settings)
            if mappings:
                args["body"].update({"mappings": mappings})

            es.indices.create(**args)
            es.indices.put_alias(index, alias)
            logger.info("created index alias=%s index=%s" % (alias, index))
        except elasticsearch.TransportError:  # index exists
            pass

    def _get_elastic_resources(self):
        elastic_resources = {}
        for resource, resource_config in self.app.config["DOMAIN"].items():
            datasource = resource_config.get("datasource", {})

            if not is_elastic(datasource):
                continue

            if (
                datasource.get("source", resource) != resource
            ):  # only put mapping for core types
                continue

            elastic_resources[resource] = resource_config
        return elastic_resources

    def _resource_mapping(self, resource):
        resource_config = self.app.config["DOMAIN"][resource]
        properties = self._get_mapping_properties(
            resource_config, parent=self._get_parent_type(resource)
        )
        return properties

    def _put_resource_mapping(
        self, resource, es, force_index=None, properties=None, **kwargs
    ):
        if not properties:
            resource_config = self.app.config["DOMAIN"][resource]
            properties = self._get_mapping_properties(
                resource_config, parent=self._get_parent_type(resource)
            )

        if not kwargs:
            kwargs = self._es_args(resource)

        kwargs["body"] = {"properties": properties}

        if force_index:
            kwargs["index"] = force_index

        if not es:
            es = self.elastic(resource)

        try:
            es.indices.put_mapping(**kwargs)
        except elasticsearch.exceptions.RequestError:
            logger.exception("mapping error, updating settings resource=%s" % resource)

    def _get_mapping_properties(self, resource_config, parent=None):
        properties = self._get_mapping(resource_config["schema"])
        properties["properties"].update(
            {
                config.DATE_CREATED: self._get_field_mapping({"type": "datetime"}),
                config.LAST_UPDATED: self._get_field_mapping({"type": "datetime"}),
            }
        )

        if parent:
            properties.update({"_parent": {"type": parent.get("type")}})

        properties["properties"].pop("_id", None)
        return properties

    def put_mapping(self, app, index=None):
        """Put mapping for elasticsearch for current schema.

        It's not called automatically now, but rather left for user to call it whenever it makes sense.
        """
        for resource, resource_config in self._get_elastic_resources().items():
            datasource = resource_config.get("datasource", {})

            if not is_elastic(datasource):
                continue

            if (
                datasource.get("source", resource) != resource
            ):  # only put mapping for core types
                continue

            properties = self._get_mapping_properties(resource_config)

            kwargs = {"index": self._resource_index(resource), "body": properties}

            try:
                self.elastic(resource).indices.put_mapping(**kwargs)
            except elasticsearch.exceptions.RequestError:
                logger.exception(
                    "mapping error, updating settings resource=%s" % resource
                )

    def _put_mappings(self, es, index, mappings):
        es.indices.put_mapping(index=index, body=mappings)

    def get_mapping(self, resource):
        """Get mapping for resource.

        :param resource: resource name
        """
        index = self._resource_index(resource)
        mapping = self.elastic(resource).indices.get_mapping(index=index)
        return next(iter(mapping.values()))

    def get_settings(self, resource):
        """Get settings for resource.

        :param resource: resource name
        """
        index = self._resource_index(resource)
        settings = self.elastic(resource).indices.get_settings(index=index)
        return next(iter(settings.values()))

    def get_index_by_alias(self, alias):
        """Get index name for given alias.

        If there is no alias assume it's an index.

        :param alias: alias name
        """
        try:
            info = self.es.indices.get_alias(name=alias)
            return next(iter(info.keys()))
        except elasticsearch.exceptions.NotFoundError:
            return alias

    def find(self, resource, req, sub_resource_lookup, **kwargs):
        """Find documents for resource."""
        args = getattr(req, "args", request.args if request else {}) or {}
        source_config = config.SOURCES[resource]

        if args.get("source"):
            query = json.loads(args.get("source"))
            query.setdefault("query", {})
            must = []
            for key, val in query["query"].items():
                if key != "bool":
                    must.append({key: val})
            if must:
                query["query"] = {"bool": {"must": must}}
        else:
            query = {"query": {"bool": {}}}

        if args.get("q", None):
            query["query"]["bool"].setdefault("must", []).append(
                _build_query_string(
                    args.get("q"),
                    default_field=args.get("df"),
                    default_operator=args.get("default_operator", "OR"),
                )
            )

        if "sort" not in query:
            if req.sort:
                sort = ast.literal_eval(req.sort)
                set_sort(query, sort)
            elif self._default_sort(resource) and "sort" not in query["query"]:
                set_sort(query, self._default_sort(resource))

        if req.max_results:
            query.setdefault("size", req.max_results)

        if req.page > 1:
            query.setdefault("from", (req.page - 1) * req.max_results)

        filters = []
        filters.append(source_config.get("elastic_filter"))
        filters.append(source_config.get("elastic_filter_callback", noop)())
        filters.append(
            {"bool": {"must": _build_lookup_filter(sub_resource_lookup)}}
            if sub_resource_lookup
            else None
        )
        filters.append(json.loads(args.get("filter")) if "filter" in args else None)
        filters.extend(args.get("filters") if "filters" in args else [])

        if req.where:
            try:
                filters.append({"term": json.loads(req.where)})
            except ValueError:
                try:
                    filters.append({"term": parse(req.where)})
                except ParseError:
                    abort(400)

        set_filters(query, filters)

        if "facets" in source_config:
            query["facets"] = source_config["facets"]

        if "aggregations" in source_config and self.should_aggregate(req):
            query["aggs"] = source_config["aggregations"]

        if "es_highlight" in source_config and self.should_highlight(req):
            for q in query["query"].get("bool", {}).get("must", []):
                if q.get("query_string"):
                    highlights = source_config.get("es_highlight", noop)(
                        q["query_string"]
                    )

            if highlights:
                query["highlight"] = highlights
                query["highlight"].setdefault("require_field_match", False)

        source_projections = None
        if self.should_project(req):
            source_projections = self.get_projected_fields(req)

        args = self._es_args(resource, source_projections=source_projections)
        try:
            hits = self.elastic(resource).search(body=query, **args)
        except elasticsearch.exceptions.RequestError as e:
            if e.status_code == 400 and "No mapping found for" in e.error:
                hits = {}
            elif e.status_code == 400 and "SearchParseException" in e.error:
                raise InvalidSearchString
            else:
                raise

        return self._parse_hits(hits, resource)

    def should_aggregate(self, req):
        """Check the environment variable and the given argument parameter to decide if aggregations needed.

        argument value is expected to be '0' or '1'
        """
        try:
            return self.app.config.get("ELASTICSEARCH_AUTO_AGGREGATIONS") or bool(
                req.args and int(req.args.get("aggregations"))
            )
        except (AttributeError, TypeError):
            return False

    def should_highlight(self, req):
        """
        Check the given argument parameter to decide if highlights needed.

        argument value is expected to be '0' or '1'
        """
        try:
            return bool(req.args and int(req.args.get("es_highlight", 0)))
        except (AttributeError, TypeError):
            return False

    def should_project(self, req):
        """
        Check the given argument parameter to decide if projections needed.

        argument value is expected to be a list of strings
        """
        try:
            return req.args and json.loads(req.args.get("projections", []))
        except (AttributeError, TypeError):
            return False

    def get_projected_fields(self, req):
        """
        Returns the projected fields from request.

        """
        try:
            args = getattr(req, "args", {})
            return ",".join(json.loads(args.get("projections")))
        except (AttributeError, TypeError):
            return None

    def find_one(self, resource, req, **lookup):
        """Find single document, if there is _id in lookup use that, otherwise filter."""
        if config.ID_FIELD in lookup:
            return self._find_by_id(
                resource=resource,
                _id=lookup[config.ID_FIELD],
                parent=lookup.get("parent"),
            )
        else:
            args = self._es_args(resource)
            filters = [{"term": {key: val}} for key, val in lookup.items()]
            query = {"query": {"bool": {"must": [filters]}}}

            try:
                args["size"] = 1
                hits = self.elastic(resource).search(body=query, **args)
                docs = self._parse_hits(hits, resource)
                return docs.first()
            except elasticsearch.NotFoundError:
                return

    def _find_by_id(self, resource, _id, parent=None):
        """Find the document by Id. If parent is not provided then on
        routing exception try to find using search.
        """

        def is_found(hit):
            if "exists" in hit:
                hit["found"] = hit["exists"]
            return hit.get("found", False)

        args = self._es_args(resource)
        try:
            # set the parent if available
            if parent:
                args["parent"] = parent

            hit = self.elastic(resource).get(id=_id, **args)

            if not is_found(hit):
                return

            docs = self._parse_hits({"hits": {"hits": [hit]}}, resource)
            return docs.first()

        except elasticsearch.NotFoundError:
            return
        except elasticsearch.TransportError as tex:
            if (
                tex.error == "routing_missing_exception"
                or "RoutingMissingException" in tex.error
            ):
                # search for the item
                args = self._es_args(resource)
                query = {"query": {"bool": {"must": [{"term": {"_id": _id}}]}}}
                try:
                    args["size"] = 1
                    hits = self.elastic(resource).search(body=query, **args)
                    docs = self._parse_hits(hits, resource)
                    return docs.first()
                except elasticsearch.NotFoundError:
                    return

    def find_one_raw(self, resource, _id):
        """Find document by id."""
        return self._find_by_id(resource=resource, _id=_id)

    def find_list_of_ids(self, resource, ids, client_projection=None):
        """Find documents by ids."""
        args = self._es_args(resource)
        return self._parse_hits(
            self.elastic(resource).mget(body={"ids": ids}, **args), resource
        )

    def insert(self, resource, doc_or_docs, **kwargs):
        """Insert document, it must be new if there is ``_id`` in it."""
        ids = []
        kwargs.update(self._es_args(resource))
        for doc in doc_or_docs:
            self._update_parent_args(resource, kwargs, doc)
            _id = doc.pop("_id", None)
            res = self.elastic(resource).index(body=doc, id=_id, **kwargs)
            doc.setdefault("_id", res.get("_id", _id))
            ids.append(doc.get("_id"))
        self._refresh_resource_index(resource)
        return ids

    def bulk_insert(self, resource, docs, **kwargs):
        """Bulk insert documents."""
        kwargs.update(self._es_args(resource))
        parent_type = self._get_parent_type(resource)
        if parent_type:
            for doc in docs:
                if doc.get(parent_type.get("field")):
                    doc["_parent"] = doc.get(parent_type.get("field"))

        res = bulk(self.elastic(resource), docs, stats_only=False, **kwargs)
        self._refresh_resource_index(resource)
        return res

    def update(self, resource, id_, updates):
        """Update document in index."""
        args = self._es_args(resource, refresh=True)
        if self._get_retry_on_conflict():
            args["retry_on_conflict"] = self._get_retry_on_conflict()

        updates.pop("_id", None)
        updates.pop("_type", None)
        self._update_parent_args(resource, args, updates)
        return self.elastic(resource).update(id=id_, body={"doc": updates}, **args)

    def replace(self, resource, id_, document):
        """Replace document in index."""
        args = self._es_args(resource, refresh=True)
        document.pop("_id", None)
        document.pop("_type", None)
        self._update_parent_args(resource, args, document)
        return self.elastic(resource).index(body=document, id=id_, **args)

    def remove(self, resource, lookup=None, parent=None, **kwargs):
        """Remove docs for resource.

        :param resource: resource name
        :param lookup: filter
        :param parent: parent id
        """
        kwargs.update(self._es_args(resource))
        if parent:
            kwargs["parent"] = parent

        if lookup:
            if lookup.get("_id"):
                try:
                    return self.elastic(resource).delete(
                        id=lookup.get("_id"), refresh=True, **kwargs
                    )
                except elasticsearch.NotFoundError:
                    return
        return ValueError("there must be `lookup._id` specified")

    def is_empty(self, resource):
        """Test if there is no document for resource.

        :param resource: resource name
        """
        args = self._es_args(resource)
        res = self.elastic(resource).count(body={"query": {"match_all": {}}}, **args)
        return res.get("count", 0) == 0

    def put_settings(self, resource, settings=None):
        """Modify index settings.

        Index must exist already.
        """
        if not settings:
            return

        try:
            old_settings = self.get_settings(resource)
            if test_settings_contain(
                old_settings["settings"]["index"], settings["settings"]
            ):
                return
        except KeyError:
            pass

        es = self.elastic(resource)
        index = self._resource_index(resource)
        self._put_settings(es, index, settings)

    def _put_settings(self, es, index, settings):
        es.indices.close(index=index)
        es.indices.put_settings(index=index, body=settings)
        es.indices.open(index=index)

    def _parse_hits(self, hits, resource):
        """Parse hits response into documents."""
        datasource = self.get_datasource(resource)
        schema = {}
        schema.update(config.DOMAIN[datasource[0]].get("schema", {}))
        schema.update(config.DOMAIN[resource].get("schema", {}))
        dates = get_dates(schema)
        docs = []
        for hit in hits.get("hits", {}).get("hits", []):
            docs.append(format_doc(hit, schema, dates))
        return ElasticCursor(hits, docs)

    def _es_args(self, resource, refresh=None, source_projections=None):
        """Get index and doctype args."""
        args = {"index": self._resource_index(resource)}

        if source_projections:
            args["_source"] = source_projections
        if refresh:
            args["refresh"] = refresh

        return args

    def _get_parent_type(self, resource):
        resource_config = self.app.config["DOMAIN"][resource] or {}
        return resource_config.get("datasource", {}).get("elastic_parent", {})

    def get_parent_id(self, resource, document):
        """Get the Parent Id of the document

        :param resource: resource name
        :param document: document containing the parent id
        """
        parent_type = self._get_parent_type(resource)
        if parent_type and document:
            return document.get(parent_type.get("field"))

        return None

    def _update_parent_args(self, resource, args, document):
        parent_type = self._get_parent_type(resource)
        parent = self.get_parent_id(resource, document)
        if parent_type and parent:
            args["parent"] = parent

    def _fields(self, resource):
        """Get projection fields for given resource."""
        datasource = self.get_datasource(resource)
        keys = datasource[2].keys()
        return ",".join(keys) + ",".join([config.LAST_UPDATED, config.DATE_CREATED])

    def _default_sort(self, resource):
        datasource = self.get_datasource(resource)
        return datasource[3]

    def _resource_index(self, resource):
        """Get index for given resource.

        by default it will be `self.index`, but it can be overriden via app.config

        :param resource: resource name
        """
        datasource = self.get_datasource(resource)
        indexes = self._resource_config(resource, "INDEXES") or {}
        default_index = "{}_{}".format(
            self._resource_config(resource, "INDEX"), datasource[0]
        )
        return indexes.get(datasource[0], default_index)

    def _refresh_resource_index(self, resource):
        """Refresh index for given resource.

        :param resource: resource name
        """
        if self._resource_config(resource, "FORCE_REFRESH", True):
            self.elastic(resource).indices.refresh(self._resource_index(resource))

    def _resource_prefix(self, resource=None):
        """Get elastic prefix for given resource.

        Resource can specify ``elastic_prefix`` which behaves same like ``mongo_prefix``.
        """
        px = "ELASTICSEARCH"
        if resource and config.DOMAIN[resource].get("elastic_prefix"):
            px = config.DOMAIN[resource].get("elastic_prefix")
        return px

    def _resource_config(self, resource, key, default=None):
        """Get config using resource elastic prefix (if any)."""
        px = self._resource_prefix(resource)
        return self.app.config.get("%s_%s" % (px, key), default)

    def elastic(self, resource):
        """Get ElasticSearch instance for given resource."""
        px = self._resource_prefix(resource)

        if px not in self.elastics:
            url = self._resource_config(resource, "URL")
            assert url, "no url for %s" % px
            self.elastics[px] = get_es(url, **self.kwargs)

        return self.elastics[px]

    def _get_retry_on_conflict(self):
        """ Get the retry on settings"""
        return self.app.config.get("ELASTICSEARCH_RETRY_ON_CONFLICT", 5)

    def drop_index(self):
        for resource in self._get_elastic_resources():
            try:
                alias = self._resource_index(resource)
                alias_info = self.elastic(resource).indices.get_alias(name=alias)
                for index in alias_info:
                    print("delete", index, alias)
                    self.elastic(resource).indices.delete(index)
            except elasticsearch.exceptions.NotFoundError:
                try:
                    self.elastic(resource).indices.delete(alias)
                except elasticsearch.exceptions.NotFoundError:
                    pass

    def search(self, query, resources, params=None):
        """Search multiple resources at the same time.

        They must use all same elastic instance and should be same schema.
        """
        if params is None:
            params = {}
        if isinstance(resources, str):
            resources = resources.split(",")
        index = [self._resource_index(resource) for resource in resources]
        hits = self.elastic(resources[0]).search(body=query, index=index, **params)
        return self._parse_hits(hits, resources[0])


def build_elastic_query(doc):
    """
    Build a query which follows ElasticSearch syntax from doc.

    1. Converts {"q":"cricket"} to the below elastic query::

        {
            "query": {
                "filtered": {
                    "query": {
                        "query_string": {
                            "query": "cricket",
                            "lenient": false,
                            "default_operator": "AND"
                        }
                    }
                }
            }
        }

    2. Converts a faceted query::

        {"q":"cricket", "type":['text'], "source": "AAP"}

    to the below elastic query::

        {
            "query": {
                "filtered": {
                    "filter": {
                        "and": [
                            {"terms": {"type": ["text"]}},
                            {"term": {"source": "AAP"}}
                        ]
                    },
                    "query": {
                        "query_string": {
                            "query": "cricket",
                            "lenient": false,
                            "default_operator": "AND"
                        }
                    }
                }
            }
        }

    :param doc: A document object which is inline with the syntax specified in the examples.
                It's the developer responsibility to pass right object.
    :returns ElasticSearch query
    """
    elastic_query, filters = {"query": {"bool": {"must": []}}}, []

    for key in doc.keys():
        if key == "q":
            elastic_query["query"]["bool"]["must"].append(_build_query_string(doc["q"]))
        else:
            _value = doc[key]
            filters.append(
                {"terms": {key: _value}}
                if isinstance(_value, list)
                else {"term": {key: _value}}
            )

    set_filters(elastic_query, filters)
    return elastic_query


def _build_query_string(q, default_field=None, default_operator="AND"):
    """
    Build ``query_string`` object from ``q``.

    :param q: q of type String
    :param default_field: default_field
    :return: dictionary object.
    """

    def _is_phrase_search(query_string):
        clean_query = query_string.strip()
        return clean_query and clean_query.startswith('"') and clean_query.endswith('"')

    def _get_phrase(query_string):
        return query_string.strip().strip('"')

    if _is_phrase_search(q) and default_field:
        query = {"match_phrase": {default_field: _get_phrase(q)}}
    else:
        query = {
            "query_string": {
                "query": q,
                "default_operator": default_operator,
                "lenient": True,
            }
        }
        if default_field:
            query["query_string"]["default_field"] = default_field

    return query


def _build_lookup_filter(lookup):
    return [{"term": {key: val}} for key, val in lookup.items()]
