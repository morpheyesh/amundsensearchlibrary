import logging

from atlasclient.client import Atlas
from atlasclient.exceptions import BadRequest
from atlasclient.models import Entity, EntityCollection
# default search page size
from atlasclient.utils import parse_table_qualified_name
from flask import current_app as app
from typing import Any, List, Dict

from search_service.models.search_result import SearchResult
from search_service.models.table import Table
from search_service.proxy import BaseProxy
from search_service.proxy.statsd_utilities import timer_with_counter

LOGGER = logging.getLogger(__name__)


class AtlasProxy(BaseProxy):
    TABLE_ENTITY = app.config['ATLAS_TABLE_ENTITY']
    DB_ATTRIBUTE = app.config['ATLAS_DB_ATTRIBUTE']
    NAME_ATTRIBUTE = app.config['ATLAS_NAME_ATTRIBUTE']
    ATTRS_KEY = 'attributes'
    REL_ATTRS_KEY = 'relationshipAttributes'
    QN_KEY = 'qualifiedName'
    ACTIVE_ENTITY_STATE = 'ACTIVE'

    """
    AtlasSearch connection handler
    """
    atlas: Atlas

    def __init__(self, *,
                 host: str = None,
                 user: str = '',
                 password: str = '',
                 client: Atlas = None,
                 page_size: int = 10) -> None:
        self.atlas = client or Atlas(host, username=user, password=password)
        self.page_size = page_size

    @staticmethod
    def _entities(collections: EntityCollection) -> List[Entity]:
        """
        Helper method for flattening all collections from {collections}
        :return: list of all entities
        """
        entities: List[Entity] = []
        for collection in collections:
            entities.extend(collection.entities)
        return entities

    def _parse_results(self, response: EntityCollection) -> List[Table]:
        """
        based on an atlas {response} with table entities, we map the required information
        :return: list of tables
        """
        table_results = []
        ids = list()
        for hit in response:
            ids.append(hit.guid)
        # Receive all entities, with attributes
        # FixMe: Can ask for the Description and Qualified Name
        # FixMe: in DSL query above, once it uses indexes
        entities = self._entities(self.atlas.entity_bulk(guid=ids, ignoreRelationships=True))

        for table in entities:
            table_attrs = table.attributes

            table_qn = parse_table_qualified_name(
                qualified_name=table_attrs.get(self.QN_KEY)
            )

            table_name = table_qn.get("table_name") or table_attrs.get('name')
            db_name = table_qn.get("db_name", '')
            db_cluster = table_qn.get("cluster_name", '')

            tags = []
            # Using or in case, if the key 'classifications' is there with attrs None
            for classification in table_attrs.get("classifications") or list():
                tags.append(
                    classification.get('typeName')
                )

            # TODO: Implement columns: Not sure if we need this for the search results.
            columns: List[str] = []
            # for column in attrs.get('columns') or list():
            #     col_entity = entity.referredEntities[column['guid']]
            #     col_attrs = col_entity['attributes']
            #     columns.append(col_attrs.get(self.NAME_KEY))
            # table_name = attrs.get(self.NAME_ATTRIBUTE)
            table = Table(name=table_name,
                          key=f"{table.typeName}://{db_cluster}.{db_name}/{table_name}",
                          description=table_attrs.get('description'),
                          cluster=db_cluster,
                          database=table.typeName,
                          schema_name=db_name,
                          column_names=columns,
                          tags=tags,
                          last_updated_epoch=table_attrs.get('updateTime'))

            table_results.append(table)

        return table_results

    @timer_with_counter
    def fetch_table_search_results_with_field(self, *,
                                              query_term: str,
                                              field_name: str,
                                              field_value: str,
                                              page_index: int = 0,
                                              index: str = '') -> SearchResult:
        """
        Query Atlas and return results as list of Table objects.
        Per field name we have a count query and a query for the tables.
        https://atlas.apache.org/Search-Advanced.html

        :param query_term: search query term
        :param field_name: field name to do the searching(e.g schema_name, tag_names)
        :param field_value: value for the field for filtering
        :param page_index: index of search page user is currently on
        :param index: search index (different resource corresponding to different index
        :return: SearchResult Object
        :return:
        """

        sql = f"Table from Table where false"
        count_sql = f"{sql} select count()"
        if field_name == 'tag':
            sql = f"from Table where __state = '{self.ACTIVE_ENTITY_STATE}' and Table is '{field_value}'"
            count_sql = f"{sql} select count()"
        elif field_name == 'schema':
            sql = f"from Table where  __state = '{self.ACTIVE_ENTITY_STATE}' and db.name like '{field_value}'"
            count_sql = f"{sql} select count()"
        elif field_name == 'table':
            sql = f"from Table where  __state = '{self.ACTIVE_ENTITY_STATE}' and name like '{field_value}'"
            count_sql = f"{sql} select count()"
        elif field_name == 'column':
            sql = f"hive_column where  __state = '{self.ACTIVE_ENTITY_STATE}' and" \
                  f" name like '{field_value}' select table"
            # TODO nanne: count tables instead of columns
            count_sql = f"hive_column where  __state = '{self.ACTIVE_ENTITY_STATE}' " \
                        f"and name like '{field_value}' select count()"

        LOGGER.debug(f"Used following sql query: {sql}")
        tables: List[Table] = []
        count_value = 0
        try:
            # count results
            count_params = {'query': count_sql}
            count_results = list(self.atlas.search_dsl(**count_params))[0]
            count_value = count_results._data['attributes']['values'][0][0]

            params = {'query': f"{sql} limit {self.page_size} offset {page_index * self.page_size}"}
            search_results = self.atlas.search_dsl(**params)
            if count_value > 0 and page_index * self.page_size <= count_value:
                # unpack all collections (usually just one collection though)
                for collection in search_results:
                    if hasattr(collection, 'entities'):
                        tables.extend(self._parse_results(response=collection.entities))
        except BadRequest:
            LOGGER.error("Atlas Search DSL error with the following query:", sql)

        return SearchResult(total_results=count_value, results=tables)

    @timer_with_counter
    def fetch_table_search_results(self, *,
                                   query_term: str,
                                   page_index: int = 0,
                                   index: str = '') -> SearchResult:
        """
        Query Atlas and return results as list of Table objects
        We use the Atlas DSL for querying the tables.
        https://atlas.apache.org/Search-Advanced.html

        :param query_term: search query term
        :param page_index: index of search page user is currently on
        :param index: search index (different resource corresponding to different index)
        :return: SearchResult Object
        """

        if not query_term:
            # return empty result for blank query term
            return SearchResult(total_results=0, results=[])

        # define query
        sql = f"Table from Table " \
            f"where __state = '{self.ACTIVE_ENTITY_STATE}' and name like '*{query_term}*' or " \
            f"description like '*{query_term}*' "

        # count amount of tables
        count_params = {'query': f"{sql} select count()"}
        count_results = list(self.atlas.search_dsl(**count_params))[0]
        count_value = count_results._data['attributes']['values'][0][0]

        # select tables
        params = {
            'query': f"{sql} "
            f"limit {self.page_size} "
            f"offset {page_index * self.page_size}"}
        search_results = self.atlas.search_dsl(**params)

        # retrieve results
        tables = []
        if 0 < count_value >= page_index * self.page_size:
            for s in search_results:
                tables.extend(self._parse_results(response=s.entities))

        return SearchResult(total_results=count_value, results=tables)

    def fetch_user_search_results(self, *,
                                  query_term: str,
                                  page_index: int = 0,
                                  index: str = '') -> SearchResult:
        pass

    def update_document(self, *, data: List[Dict[str, Any]], index: str = '') -> str:
        raise NotImplementedError()

    def create_document(self, *, data: List[Dict[str, Any]], index: str = '') -> str:
        raise NotImplementedError()

    def delete_document(self, *, data: List[str], index: str = '') -> str:
        raise NotImplementedError()
