#! /usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of IVRE.
# Copyright 2011 - 2019 Pierre LALET <pierre.lalet@cea.fr>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.

"""This sub-module contains functions to interact with the ElasticSearch
databases.

"""

import json
import re
try:
    from urllib.parse import unquote
except ImportError:
    from urllib import unquote


from elasticsearch import Elasticsearch, helpers
from elasticsearch_dsl import Q
from elasticsearch_dsl.query import Query
from future.utils import viewitems
from past.builtins import basestring

from ivre.db import DB, DBActive, DBView
from ivre import utils, xmlnmap


PAGESIZE = 250


class ElasticDB(DB):

    nested_fields = []

    # filters
    flt_empty = Q()

    def __init__(self, url):
        super(ElasticDB, self).__init__()
        self.username = ''
        self.password = ''
        self.hosts = None
        if '@' in url.netloc:
            username, hostname = url.netloc.split('@', 1)
            if ':' in username:
                self.username, self.password = (unquote(val) for val in
                                                username.split(':', 1))
            else:
                self.username = unquote(username)
            if hostname:
                self.hosts = [hostname]
        elif url.netloc:
            self.hosts = [url.netloc]
        index_prefix = url.path.lstrip('/')
        if index_prefix:
            self.index_prefix = index_prefix + '-'
        else:
            self.index_prefix = 'ivre-'
        self.params = dict(x.split('=', 1) if '=' in x else (x, None)
                           for x in url.query.split('&') if x)

    def init(self):
        """Initializes the mappings."""
        for idxnum, mapping in enumerate(self.mappings):
            idxname = self.indexes[idxnum]
            self.db_client.indices.delete(
                index=idxname,
                ignore=[400, 404],
            )
            self.db_client.indices.create(
                index=idxname,
                body={
                    "mappings": {
                        "properties": mapping,
                        # Since we do not need full text searches, use
                        # type "keyword" for strings (unless otherwise
                        # specified in mapping) instead of default
                        # (text + keyword)
                        "dynamic_templates": [
                            {"strings": {
                                "match_mapping_type": "string",
                                "mapping": {"type": "keyword"},
                            }},
                        ],
                    }
                },
            )

    @property
    def db_client(self):
        """The DB connection."""
        try:
            return self._db_client
        except AttributeError:
            self._db_client = Elasticsearch(
                hosts=self.hosts,
                http_auth=(self.username, self.password)
            )
            return self._db_client

    @property
    def server_info(self):
        """Server information."""
        try:
            return self._server_info
        except AttributeError:
            self._server_info = self.db_client.info()
            return self._server_info

    @staticmethod
    def to_binary(data):
        return utils.encode_b64(data).decode()

    @staticmethod
    def from_binary(data):
        return utils.decode_b64(data.encode())

    @staticmethod
    def ip2internal(addr):
        return addr

    @staticmethod
    def internal2ip(addr):
        return addr

    @staticmethod
    def searchnonexistent():
        return Q('match', _id=0)

    @classmethod
    def searchhost(cls, addr, neg=False):
        """Filters (if `neg` == True, filters out) one particular host
        (IP address).
        """
        return Q('match', addr=addr)

    @classmethod
    def searchhosts(cls, hosts, neg=False):
        pass

    @staticmethod
    def _get_pattern(regexp):
        # The equivalent to a MongoDB or PostgreSQL search for regexp
        # /Test/ would be /.*Test.*/ in Elasticsearch, while /Test/ in
        # Elasticsearch is equivalent to /^Test$/ in MongoDB or
        # PostgreSQL.
        pattern, flags = utils.regexp2pattern(regexp)
        if flags & ~re.UNICODE:
            # is a flag, other than re.UNICODE, is set, issue a
            # warning as it will not be used
            utils.LOGGER.warning(
                'Elasticsearch does not support flags in regular '
                'expressions [%r with flags=%r]',
                pattern, flags
            )
        return pattern

    @staticmethod
    def _flt_and(cond1, cond2):
        return cond1 & cond2

    @staticmethod
    def _flt_or(cond1, cond2):
        return cond1 | cond2

    @staticmethod
    def flt2str(flt):
        return json.dumps(flt.to_dict())


def _create_mappings(nested, all_mappings):
    res = {}
    for fld in nested:
        cur = res
        curkey = None
        for subkey in fld.split('.')[:-1]:
            if curkey is not None:
                subkey = "%s.%s" % (curkey, subkey)
            if cur.get(subkey, {}).get('type') == 'nested':
                cur = cur[subkey].setdefault('properties', {})
                curkey = None
            else:
                curkey = subkey
        subkey = fld.rsplit('.', 1)[-1]
        if curkey is not None:
            subkey = "%s.%s" % (curkey, subkey)
        cur[subkey] = {"type": 'nested'}
    for fldtype, fldnames in all_mappings:
        for fld in fldnames:
            cur = res
            curkey = None
            for subkey in fld.split('.')[:-1]:
                if curkey is not None:
                    subkey = "%s.%s" % (curkey, subkey)
                if cur.get(subkey, {}).get('type') == 'nested':
                    cur = cur[subkey].setdefault('properties', {})
                    curkey = None
                else:
                    curkey = subkey
            subkey = fld.rsplit('.', 1)[-1]
            if curkey is not None:
                subkey = "%s.%s" % (curkey, subkey)
            cur[subkey] = {"type": fldtype}
    return res


class ElasticDBActive(ElasticDB, DBActive):

    nested_fields = [
        "ports",
        "ports.scripts",
    ]
    mappings = [
        _create_mappings(
            nested_fields,
            [
                ("nested", nested_fields),
                ("ip", DBActive.ipaddr_fields),
                ("date", DBActive.datetime_fields),
                ("geo_point", ["infos.coordinates"]),
            ]
        ),
    ]
    index_hosts = 0

    def store_or_merge_host(self, host):
        raise NotImplementedError

    def store_host(self, host):
        if 'coordinates' in host.get('infos', {}):
            host['infos']['coordinates'] = host['infos']['coordinates'][::-1]
        self.db_client.index(index=self.indexes[0],
                             body=host)

    def count(self, flt):
        return self.db_client.search(
            body={"query": flt.to_dict()},
            index=self.indexes[0],
            size=0,
            ignore_unavailable=True,
        )['hits']['total']['value']

    def get(self, spec, fields=None, **kargs):
        """Queries the active index."""
        query = {"query": spec.to_dict()}
        if fields is not None:
            query['_source'] = fields
        for rec in helpers.scan(self.db_client,
                                query=query,
                                index=self.indexes[0],
                                ignore_unavailable=True):
            host = dict(rec['_source'], _id=rec['_id'])
            if 'coordinates' in host.get('infos', {}):
                host['infos']['coordinates'] = host['infos'][
                    'coordinates'
                ][::-1]
            for field in self.datetime_fields:
                if field in host:
                    host[field] = utils.all2datetime(host[field])
            yield host

    def remove(self, host):
        """Removes the host from the active column. `host` must be the record as
        returned by .get().

        """
        self.db_client.delete(
            id=host['_id'],
            index=self.indexes[0],
        )

    def distinct(self, field, flt=None, sort=None, limit=None, skip=None):
        if flt is None:
            flt = self.flt_empty
        if field == 'infos.coordinates':
            def fix_result(value):
                return tuple(float(v) for v in value.split(', '))
            base_query = {"script": {
                "lang": "painless",
                "source": "doc['infos.coordinates'].value",
            }}
            flt = self.flt_and(flt, self.searchhaslocation())
        else:
            base_query = {"field": field}
            if field in self.datetime_fields:
                def fix_result(value):
                    return utils.all2datetime(value / 1000)
            else:
                def fix_result(value):
                    return value
        # https://techoverflow.net/2019/03/17/how-to-query-distinct-field-values-in-elasticsearch/
        query = {"size": PAGESIZE,
                 "sources": [{field: {"terms": base_query}}]}
        while True:
            result = self.db_client.search(
                body={"query": flt.to_dict(),
                      "aggs": {"values": {"composite": query}}},
                index=self.indexes[0],
                ignore_unavailable=True,
                size=0
            )
            for value in result["aggregations"]["values"]["buckets"]:
                yield fix_result(value['key'][field])
            if 'after_key' not in result["aggregations"]["values"]:
                break
            query["after"] = result["aggregations"]["values"]["after_key"]

    def getlocations(self, flt):
        query = {"size": PAGESIZE,
                 "sources": [{"coords": {"terms": {"script": {
                     "lang": "painless",
                     "source": "doc['infos.coordinates'].value",
                 }}}}]}
        flt = self.flt_and(flt & self.searchhaslocation())
        while True:
            result = self.db_client.search(
                body={"query": flt.to_dict(),
                      "aggs": {"values": {"composite": query}}},
                index=self.indexes[0],
                ignore_unavailable=True,
                size=0
            )
            for value in result["aggregations"]["values"]["buckets"]:
                yield {'_id': tuple(float(v) for v in
                                    value['key']["coords"].split(', ')),
                       'count': value['doc_count']}
            if 'after_key' not in result["aggregations"]["values"]:
                break
            query["after"] = result["aggregations"]["values"]["after_key"]

    def topvalues(self, field, flt=None, topnbr=10, sort=None, least=False):
        """
        This method uses an aggregation to produce top values for a given
        field or pseudo-field. Pseudo-fields are:
          - category / asnum / country / net[:mask]
          - port
          - port:open / :closed / :filtered / :<servicename>
          - portlist:open / :closed / :filtered
          - countports:open / :closed / :filtered
          - service / service:<portnbr>
          - product / product:<portnbr>
          - cpe / cpe.<part> / cpe:<cpe_spec> / cpe.<part>:<cpe_spec>
          - devicetype / devicetype:<portnbr>
          - script:<scriptid> / script:<port>:<scriptid>
            / script:host:<scriptid>
          - cert.* / smb.* / sshkey.* / ike.*
          - httphdr / httphdr.{name,value} / httphdr:<name>
          - modbus.* / s7.* / enip.*
          - mongo.dbs.*
          - vulns.*
          - screenwords
          - file.* / file.*:scriptid
          - hop

        """
        outputproc = None
        nested = None
        if flt is None:
            flt = self.flt_empty
        if field == "asnum":
            flt = self.flt_and(flt, Q("exists", field="infos.as_num"))
            field = {"field": "infos.as_num"}
        elif field == "as":
            def outputproc(value):
                return tuple(val if i else int(val)
                             for i, val in enumerate(value.split(', ', 1)))
            flt = self.flt_and(flt, Q("exists", field="infos.as_num"))
            field = {"script": {
                "lang": "painless",
                "source":
                "doc['infos.as_num'].value + ', ' + "
                "doc['infos.as_name'].value",
            }}
        elif field == "port" or field.startswith("port:"):
            def outputproc(value):
                return tuple(int(val) if i else val
                             for i, val in enumerate(value.split('/', 1)))
            if field == "port":
                flt = self.flt_and(flt,
                                   Q('nested', path='ports',
                                     query=Q('exists', field="ports.port")))
                field = {"script": {
                    "lang": "painless",
                    "source": """List result = new ArrayList();
for(item in params._source.ports) {
    if(item.port != -1) {
        result.add(item.protocol + '/' + item.port);
    }
}
return result;
""",
                }}
            else:
                info = field[5:]
                if info in ['open', 'filtered', 'closed']:
                    flt = self.flt_and(flt,
                                       Q('nested', path='ports',
                                         query=Q('match',
                                                 ports__state_state=info)))
                    matchfield = "state_state"
                else:
                    flt = self.flt_and(flt,
                                       Q('nested', path='ports',
                                         query=Q('match',
                                                 ports__service_name=info)))
                    matchfield = "service_name"
                field = {"script": {
                    "lang": "painless",
                    "source": """List result = new ArrayList();
for(item in params._source.ports) {
    if(item[params.field] == params.value) {
        result.add(item.protocol + '/' + item.port);
    }
}
return result;
""",
                    "params": {
                        "field": matchfield,
                        "value": info,
                    }
                }}
        elif field == 'httphdr':
            def outputproc(value):
                return tuple(value.split(':', 1))
            flt = self.flt_and(flt, self.searchscript(name="http-headers"))
            field = {"script": {
                "lang": "painless",
                "source": """List result = new ArrayList();
for(item in params._source.ports) {
    if (item.containsKey('scripts')) {
        for(script in item.scripts) {
            if(script.id == 'http-headers' &&
               script.containsKey('http-headers')) {
                for(hdr in script['http-headers']) {
                    result.add(hdr.name + ':' + hdr.value);
                }
            }
        }
    }
}
return result;
""",
            }}
        elif field.startswith('httphdr.'):
            flt = self.flt_and(flt, self.searchscript(name="http-headers"))
            field = {"field": "ports.scripts.http-headers.%s" % field[8:]}
            nested = {
                "nested": {"path": "ports"},
                "aggs": {"patterns": {
                    "nested": {"path": "ports.scripts"},
                    "aggs": {"patterns": {"terms": field}},
                }},
            }
        elif field.startswith('httphdr:'):
            subfield = field[8:].lower()
            flt = self.flt_and(flt,
                               self.searchscript(name="http-headers",
                                                 values={"name": subfield}))
            field = {"script": {
                "lang": "painless",
                "source": """List result = new ArrayList();
for(item in params._source.ports) {
    if (item.containsKey('scripts')) {
        for(script in item.scripts) {
            if(script.id == 'http-headers' &&
               script.containsKey('http-headers')) {
                for(hdr in script['http-headers']) {
                    if (hdr.name == params.name) {
                        result.add(hdr.value);
                    }
                }
            }
        }
    }
}
return result;
""",
                "params": {"name": subfield}
            }}
        else:
            field = {"field": field}
        body = {"query": flt.to_dict()}
        if nested is None:
            body["aggs"] = {"patterns": {"terms": dict(field, size=topnbr)}}
        else:
            body["aggs"] = {"patterns": nested}
        result = self.db_client.search(
            body=body,
            index=self.indexes[0],
            ignore_unavailable=True,
            size=0
        )
        result = result["aggregations"]
        while 'patterns' in result:
            result = result['patterns']
        result = result['buckets']
        if outputproc is None:
            for res in result:
                yield {'_id': res['key'], 'count': res['doc_count']}
        else:
            for res in result:
                yield {'_id': outputproc(res['key']),
                       'count': res['doc_count']}

    @staticmethod
    def searchhaslocation(neg=False):
        res = Q('exists', field='infos.coordinates')
        if neg:
            return ~res
        return res

    @staticmethod
    def searchcountry(country, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        country, or a list of countries.

        """
        country = utils.country_unalias(country)
        if isinstance(country, list):
            res = Q("terms", infos__country_code=country)
        else:
            res = Q("match", infos__country_code=country)
        if neg:
            return ~res
        return res

    @staticmethod
    def searchasnum(asnum, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS number(s).

        """
        if not isinstance(asnum, basestring) and hasattr(asnum, '__iter__'):
            res = Q("terms", infos__as_num=[int(val) for val in asnum])
        else:
            res = Q("match", infos__as_num=int(asnum))
        if neg:
            return ~res
        return res

    @classmethod
    def searchasname(cls, asname, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS.

        """
        if isinstance(asname, utils.REGEXP_T):
            res = Q("regexp", infos__as_name=cls._get_pattern(asname))
        else:
            res = Q("match", infos__as_name=asname)
        if neg:
            return ~res
        return res

    @classmethod
    def searchscript(cls, name=None, output=None, values=None, neg=False):
        """Search a particular content in the scripts results.

        """
        req = []
        if name is not None:
            if isinstance(name, utils.REGEXP_T):
                req.append(("regexp", "id", cls._get_pattern(name)))
            else:
                req.append(("match", "id", name))
        if output is not None:
            if isinstance(output, utils.REGEXP_T):
                req.append(("regexp", "output", cls._get_pattern(output)))
            else:
                req.append(("match", "output", output))
        if values is not None:
            if name is None:
                raise TypeError(".searchscript() needs a `name` arg "
                                "when using a `values` arg")
            subfield = xmlnmap.ALIASES_TABLE_ELEMS.get(name, name)
            if isinstance(values, Query):
                req.append(values)
            elif isinstance(values, basestring):
                req.append(("match", subfield, values))
            elif isinstance(values, utils.REGEXP_T):
                req.append(("regexp", subfield, cls._get_pattern(values)))
            else:
                for field, value in viewitems(values):
                    if isinstance(value, utils.REGEXP_T):
                        req.append(("regexp",
                                    "%s.%s" % (subfield, field),
                                    cls._get_pattern(value)))
                    else:
                        req.append(("match",
                                    "%s.%s" % (subfield, field),
                                    value))
        if not req:
            res = Q('nested', path='ports',
                    query=Q('nested', path='ports.scripts',
                            query=Q("exists", field="ports.scripts")))
        else:
            query = Q()
            for subreq in req:
                if isinstance(subreq, Query):
                    query &= subreq
                else:
                    query &= Q(subreq[0],
                               **{"ports.scripts.%s" % subreq[1]: subreq[2]})
            res = Q("nested", path="ports",
                    query=Q("nested", path="ports.scripts", query=query))
        if neg:
            return ~res
        return res


class ElasticDBView(ElasticDBActive, DBView):

    def __init__(self, url):
        super(ElasticDBView, self).__init__(url)
        self.indexes = ['%s%s' % (self.index_prefix,
                                  self.params.pop('indexname_hosts', 'views'))]

    def store_or_merge_host(self, host):
        if not self.merge_host(host):
            self.store_host(host)
