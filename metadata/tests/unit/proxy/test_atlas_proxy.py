# Copyright Contributors to the Amundsen project.
# SPDX-License-Identifier: Apache-2.0

import copy
import unittest
from typing import Any, Dict, Optional, cast
from unittest.mock import MagicMock, patch

from amundsen_common.models.lineage import Lineage, LineageItem
from amundsen_common.models.popular_table import PopularTable
from amundsen_common.models.table import (Badge, Column,
                                          ProgrammaticDescription, Reader,
                                          ResourceReport, Stat, Table, User)
from apache_atlas.model.instance import AtlasRelatedObjectId
from apache_atlas.model.relationship import AtlasRelationship
from apache_atlas.utils import type_coerce
from werkzeug.exceptions import BadRequest

from metadata_service import create_app
from metadata_service.entity.dashboard_detail import DashboardDetail
from metadata_service.entity.dashboard_query import DashboardQuery
from metadata_service.entity.resource_type import ResourceType
from metadata_service.entity.tag_detail import TagDetail
from metadata_service.exception import NotFoundException
from metadata_service.util import UserResourceRel
from tests.unit.proxy.fixtures.atlas_test_data import Data, DottedDict


class TestAtlasProxy(unittest.TestCase, Data):
    def setUp(self) -> None:
        self.app = create_app(config_module_class='metadata_service.config.LocalConfig')
        self.app.config['PROGRAMMATIC_DESCRIPTIONS_EXCLUDE_FILTERS'] = ['spark.*']
        self.app.config['WATERMARK_DATE_FORMATS'] = ''
        self.app.config['POPULAR_TABLE_MINIMUM_READER_COUNT'] = 0
        self.app_context = self.app.app_context()
        self.app_context.push()

        with patch('metadata_service.proxy.atlas_proxy.AtlasClient'):
            # Importing here to make app context work before
            # importing `current_app` indirectly using the AtlasProxy
            from metadata_service.proxy.atlas_proxy import AtlasProxy
            self.proxy = AtlasProxy(host='DOES_NOT_MATTER', port=0000)
            self.proxy.client = MagicMock()

    def to_class(self, entity: Dict) -> Any:
        class ObjectView(object):
            def __init__(self, dictionary: Dict):
                self.__dict__ = dictionary

        return ObjectView(entity)

    def _mock_get_table_entity(self, entity: Optional[Any] = None) -> Any:
        entity = cast(dict, entity or self.entity1)
        mocked_entity = MagicMock()
        mocked_entity.entity = entity
        if mocked_entity.entity == self.entity1:
            mocked_entity.referredEntities = {
                self.test_column['guid']: self.test_column
            }
        else:
            mocked_entity.referredEntities = {}
        self.proxy._get_table_entity = MagicMock(return_value=mocked_entity)  # type: ignore
        return mocked_entity

    def _mock_get_create_glossary_term(self, tag: str, assigned_ent: Optional[Any] = None, guid: str = None) -> Any:
        term = MagicMock()
        term.guid = guid or 123
        if assigned_ent:
            term.attributes = {"assignedEntities": [assigned_ent]}
        self.proxy._get_create_glossary_term = MagicMock(return_value=term)
        return term

    def _mock_get_bookmark_entity(self, entity: Optional[Any] = None) -> Any:
        entity = entity or self.entity1
        mocked_entity = MagicMock()
        mocked_entity.entity = entity
        self.proxy._get_bookmark_entity = MagicMock(return_value=mocked_entity)  # type: ignore
        return mocked_entity

    def test_get_table_entity(self) -> None:
        unique_attr_response = MagicMock()

        self.proxy.client.entity.get_entity_by_attribute = MagicMock(
            return_value=unique_attr_response)
        ent = self.proxy._get_table_entity(table_uri=self.table_uri)

        self.assertEqual(ent.__repr__(), unique_attr_response.__repr__())

    def _create_mocked_report_entities_collection(self) -> None:
        mocked_report_entities_collection = MagicMock()
        mocked_report_entities_collection.entities = []
        for entity in self.report_entities:
            mocked_report_entity = MagicMock()
            mocked_report_entity.status = entity['status']
            mocked_report_entity.attributes = entity['attributes']
            mocked_report_entities_collection.entities.append(mocked_report_entity)

        self.report_entity_collection = mocked_report_entities_collection

    def test_get_sorted_reports(self) -> None:
        self._create_mocked_report_entities_collection()
        self.report_entity_collection.entities.sort(key=lambda x: x.attributes['name'], reverse=True)
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=self.report_entity_collection)
        reports_guid = cast(dict, self.entity1)['attributes']['reports']
        sorted_reports = self.proxy._get_reports(reports_guid)
        expected = [ResourceReport(name="test_report", url="http://test"),
                    ResourceReport(name="test_report3", url="http://test3")]
        self.assertEqual(sorted_reports, expected)

    def _get_table(self, custom_stats_format: bool = False) -> None:
        if custom_stats_format:
            test_exp_col = self.test_exp_col_stats_formatted
        else:
            test_exp_col = self.test_exp_col_stats_raw
        ent_attrs = cast(dict, self.entity1['attributes'])
        self._mock_get_table_entity()
        self._create_mocked_report_entities_collection()
        self.proxy._get_owners = MagicMock(return_value=[User(email=ent_attrs['owner'])])  # type: ignore
        response = self.proxy.get_table(table_uri=self.table_uri)

        classif_name = self.classification_entity['classifications'][0]['typeName']

        col_attrs = cast(dict, self.test_column['attributes'])
        exp_col_stats = list()

        for stats in test_exp_col:
            exp_col_stats.append(
                Stat(
                    stat_type=stats['attributes']['stat_name'],
                    stat_val=stats['attributes']['stat_val'],
                    start_epoch=stats['attributes']['start_epoch'],
                    end_epoch=stats['attributes']['end_epoch'],
                )
            )

        exp_col = Column(name=col_attrs['name'],
                         description='column description',
                         col_type='Managed',
                         sort_order=col_attrs['position'],
                         stats=exp_col_stats,
                         badges=[Badge(category='default', badge_name='active_col_badge')])

        expected = Table(database=self.entity_type,
                         cluster=self.cluster,
                         schema=self.db,
                         name=ent_attrs['name'],
                         tags=[],
                         badges=[Badge(badge_name=classif_name, category="default")],
                         description=ent_attrs['description'],
                         owners=[User(email=ent_attrs['owner'])],
                         resource_reports=[],
                         last_updated_timestamp=int(str(self.entity1['updateTime'])[:10]),
                         columns=[exp_col] * self.active_columns,
                         watermarks=[],
                         programmatic_descriptions=[ProgrammaticDescription(source='test parameter key a',
                                                                            text='testParameterValueA'),
                                                    ProgrammaticDescription(source='test parameter key b',
                                                                            text='testParameterValueB')
                                                    ],
                         is_view=False)

        self.assertEqual(str(expected), str(response))

    def test_get_table_without_custom_stats_format(self) -> None:
        self._get_table()

    def test_get_table_with_custom_stats_format(self) -> None:
        statistics_format_spec = {'min': {'new_name': 'minimum', 'format': '{:,.2f}'},
                                  'max': {'drop': True}}

        with patch.object(self.proxy, 'STATISTICS_FORMAT_SPEC', statistics_format_spec):
            self._get_table(custom_stats_format=True)

    def test_get_table_not_found(self) -> None:
        with self.assertRaises(NotFoundException):
            self.proxy.client.entity.get_entity_by_attribute = MagicMock(side_effect=Exception('Boom!'))
            self.proxy.get_table(table_uri=self.table_uri)

    def test_get_table_missing_info(self) -> None:
        with self.assertRaises(BadRequest):
            local_entity = copy.deepcopy(self.entity1)
            local_entity.pop('attributes')
            unique_attr_response = MagicMock()
            unique_attr_response.entity = local_entity

            self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=unique_attr_response)
            self.proxy.get_table(table_uri=cast(str, self.table_uri))

    def test_get_popular_tables(self) -> None:
        ent1 = self.to_class(self.entity1)
        ent2 = self.to_class(self.entity2)

        table_collection = MagicMock()

        table_collection.entities = [ent1, ent2]

        result = MagicMock(return_value=table_collection)

        with patch.object(self.proxy.client.discovery, 'faceted_search', result):
            response = self.proxy.get_popular_tables(num_entries=2)

            ent1_attrs = cast(dict, self.entity1['attributes'])
            ent2_attrs = cast(dict, self.entity2['attributes'])

            expected = [
                PopularTable(database=self.entity_type, cluster=self.cluster, schema=self.db,
                             name=ent1_attrs['name'], description=ent1_attrs['description']),
                PopularTable(database=self.entity_type, cluster=self.cluster, schema=self.db,
                             name=ent2_attrs['name'], description=ent1_attrs['description']),
            ]

            self.assertEqual(expected.__repr__(), response.__repr__())

    def test_get_table_description(self) -> None:
        self._mock_get_table_entity()
        response = self.proxy.get_table_description(table_uri=self.table_uri)
        attributes = cast(dict, self.entity1['attributes'])
        self.assertEqual(response, attributes['description'])

    def test_put_table_description(self) -> None:
        self._mock_get_table_entity()
        self.proxy.put_table_description(table_uri=self.table_uri,
                                         description="DOESNT_MATTER")

    def test_get_tags(self) -> None:
        mocked_term = MagicMock()
        mocked_term.attributes = {
            "name": "PII",
            "assignedEntities": ["a", "b"]
        }

        result = MagicMock()
        result.entities = [mocked_term]

        self.proxy.client.discovery.faceted_search = MagicMock(return_value=result)

        response = self.proxy.get_tags()

        expected = [TagDetail(tag_name='PII', tag_count=2)]
        self.assertEqual(expected.__repr__(), response.__repr__())

    def test_add_tag(self) -> None:
        tag = "TAG"
        self._mock_get_table_entity()
        term = self._mock_get_create_glossary_term(tag)

        with patch.object(self.proxy.client.glossary, 'assign_term_to_entities') as mock_execute:
            self.proxy.add_tag(id=self.table_uri, tag=tag, tag_type='default')
            mock_execute.assert_called_with(
                term.guid,
                [AtlasRelatedObjectId({'guid': self.entity1['guid'],
                                       "typeName": "Table"})]
            )

    def test_delete_tag(self) -> None:
        tag = "TAG"
        ent = self._mock_get_table_entity()
        term = self._mock_get_create_glossary_term(tag, ent.entity)
        self.proxy.client.glossary.get_entities_assigned_with_term = MagicMock(return_value=[ent.entity])

        with patch.object(self.proxy.client.glossary, 'disassociate_term_from_entities') as mock_execute:
            self.proxy.delete_tag(id=self.table_uri, tag=tag, tag_type='default')
            mock_execute.assert_called_with(term.guid,
                                            [AtlasRelatedObjectId(self.entity1)])

    def test_add_owner(self) -> None:
        owner = "OWNER"
        user_guid = 123
        self._mock_get_table_entity()
        mocked_user_entity = MagicMock()
        mocked_user_entity.guidAssignments = dict(user_guid=user_guid)
        self.proxy.client.entity.create_entity = MagicMock(return_value=mocked_user_entity)

        with patch.object(self.proxy.client.relationship, 'create_relationship') as mock_execute:
            self.proxy.add_owner(table_uri=self.table_uri, owner=owner)
            mock_execute.assert_called_with(
                relationship=type_coerce({'typeName': 'DataSet_Users_Owner',
                                          'end1': {'guid': self.entity1['guid'], 'typeName': 'Table'},
                                          'end2': {'guid': user_guid, 'typeName': 'User'}}, AtlasRelationship)

            )

    def test_get_column(self) -> None:
        self._mock_get_table_entity()
        response = self.proxy._get_column(
            table_uri=self.table_uri,
            column_name=cast(dict, self.test_column['attributes'])['name'])
        self.assertDictEqual(response, self.test_column)

    def test_get_column_wrong_name(self) -> None:
        with self.assertRaises(NotFoundException):
            self._mock_get_table_entity()
            self.proxy._get_column(table_uri=self.table_uri, column_name='FAKE')

    def test_get_column_no_referred_entities(self) -> None:
        with self.assertRaises(NotFoundException):
            local_entity: Dict = self.entity2
            local_entity['attributes']['columns'] = [{'guid': 'ent_2_col'}]
            self._mock_get_table_entity(local_entity)
            self.proxy._get_column(table_uri=self.table_uri, column_name='FAKE')

    def test_get_column_description(self) -> None:
        self._mock_get_table_entity()
        attributes = cast(dict, self.test_column['attributes'])
        response = self.proxy.get_column_description(
            table_uri=self.table_uri,
            column_name=attributes['name'])
        self.assertEqual(response, attributes.get('description'))

    def test_put_column_description(self) -> None:
        self._mock_get_table_entity()
        attributes = cast(dict, self.test_column['attributes'])
        self.proxy.put_column_description(table_uri=self.table_uri,
                                          column_name=attributes['name'],
                                          description='DOESNT_MATTER')

    def test_get_table_by_user_relation_follow(self) -> None:
        bookmark1 = copy.deepcopy(self.bookmark_entity1)
        bookmark1 = self.to_class(bookmark1)
        bookmark_collection = MagicMock()
        bookmark_collection.entities = [bookmark1]

        self.proxy.client.discovery.faceted_search = MagicMock(return_value=bookmark_collection)
        res = self.proxy.get_table_by_user_relation(user_email='test_user_id',
                                                    relation_type=UserResourceRel.follow)

        expected = [PopularTable(database=Data.entity_type, cluster=Data.cluster, schema=Data.db,
                                 name=Data.name, description=None)]

        self.assertEqual(res, {'table': expected})

    def test_get_table_by_user_relation_own(self) -> None:
        unique_attr_response = MagicMock()
        unique_attr_response.entity = Data.user_entity_2
        self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=unique_attr_response)

        entity_bulk_result = MagicMock()
        entity_bulk_result.entities = [DottedDict(self.entity1)]
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=entity_bulk_result)

        res = self.proxy.get_table_by_user_relation(user_email='test_user_id',
                                                    relation_type=UserResourceRel.own)

        self.assertEqual(len(res.get("table")), 1)  # type: ignore

        ent1_attrs = cast(dict, self.entity1['attributes'])

        expected = [PopularTable(database=self.entity_type, cluster=self.cluster, schema=self.db,
                                 name=ent1_attrs['name'], description=ent1_attrs['description'])]

        self.assertEqual({'table': expected}, res)

    def test_get_resources_owned_by_user_success(self) -> None:
        unique_attr_response = MagicMock()
        unique_attr_response.entity = Data.user_entity_2
        self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=unique_attr_response)

        entity_bulk_result = MagicMock()
        entity_bulk_result.entities = [DottedDict(self.entity1)]
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=entity_bulk_result)

        res = self.proxy._get_resources_owned_by_user(user_id='test_user_2',
                                                      resource_type=ResourceType.Table.name)

        self.assertEqual(len(res), 1)

        ent1_attrs = cast(dict, self.entity1['attributes'])

        expected = [PopularTable(database=self.entity_type, cluster=self.cluster, schema=self.db,
                                 name=ent1_attrs['name'], description=ent1_attrs['description'])]

        self.assertEqual(expected, res)

    def test_get_resources_owned_by_user_no_user(self) -> None:
        unique_attr_response = MagicMock()
        unique_attr_response.entity = None
        self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=unique_attr_response)
        with self.assertRaises(NotFoundException):
            self.proxy._get_resources_owned_by_user(user_id='test_user_2',
                                                    resource_type=ResourceType.Table.name)

    def test_get_resources_owned_by_user_default_owner(self) -> None:
        unique_attr_response = MagicMock()
        unique_attr_response.entity = Data.user_entity_2
        self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=unique_attr_response)

        basic_search_result = MagicMock()
        basic_search_result.entities = self.reader_entities

        entity2 = MagicMock()
        entity2.guid = self.entity2['guid']

        basic_search_response = MagicMock()
        basic_search_response.entities = [entity2]

        self.proxy.client.discovery.faceted_search = MagicMock(return_value=basic_search_response)

        entity_bulk_result = MagicMock()
        entity_bulk_result.entities = [DottedDict(self.entity1)]
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=entity_bulk_result)

        res = self.proxy._get_resources_owned_by_user(user_id='test_user_2',
                                                      resource_type=ResourceType.Table.name)

        self.assertEqual(len(res), 1)

    def test_add_resource_relation_by_user(self) -> None:
        bookmark_entity = self._mock_get_bookmark_entity()
        with patch.object(bookmark_entity, 'update') as mock_execute:
            self.proxy.add_resource_relation_by_user(id=self.table_uri,
                                                     user_id="test_user_id",
                                                     relation_type=UserResourceRel.follow,
                                                     resource_type=ResourceType.Table)
            mock_execute.assert_called_with()

    def test_delete_resource_relation_by_user(self) -> None:
        bookmark_entity = self._mock_get_bookmark_entity()
        with patch.object(bookmark_entity, 'update') as mock_execute:
            self.proxy.delete_resource_relation_by_user(id=self.table_uri,
                                                        user_id="test_user_id",
                                                        relation_type=UserResourceRel.follow,
                                                        resource_type=ResourceType.Table)
            mock_execute.assert_called_with()

    def test_get_readers(self) -> None:
        entity_bulk_result = MagicMock()
        entity_bulk_result.entities = self.reader_entities
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=entity_bulk_result)

        res = self.proxy._get_readers(dict(relationshipAttributes=dict(readers=[dict(guid=1, entityStatus='ACTIVE',
                                                                                     relationshipStatus='ACTIVE')])),
                                      Reader, 1)

        expected = [Reader(user=User(email='test_user_2', user_id='test_user_2'), read_count=150)]

        self.assertEqual(expected, res)

    def test_get_frequently_used_tables(self) -> None:
        entity_unique_attribute_result = MagicMock()
        entity_unique_attribute_result.entity = DottedDict(self.user_entity_2)
        self.proxy.client.entity.get_entity_by_attribute = MagicMock(return_value=entity_unique_attribute_result)

        entity_bulk_result = MagicMock()
        entity_bulk_result.entities = [DottedDict(self.reader_entity_1)]
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=entity_bulk_result)

        expected = {'table': [PopularTable(cluster=self.cluster,
                                           name='Table1',
                                           schema=self.db,
                                           database=self.entity_type)]}

        res = self.proxy.get_frequently_used_tables(user_email='dummy')

        self.assertEqual(expected, res)

    def test_get_latest_updated_ts_when_exists(self) -> None:
        self.proxy.client.admin.get_metrics = MagicMock(return_value=self.metrics_data)
        result = self.proxy.get_latest_updated_ts()
        assert result == 1598342400

    def test_get_latest_updated_ts_when_not_exists(self) -> None:
        self.proxy.client.admin.get_metrics = MagicMock()
        result = self.proxy.get_latest_updated_ts()
        assert result == 0

    def test_get_user_detail_default(self) -> None:
        user_id = "dummy@email.com"
        user_details = self.proxy._get_user_details(user_id=user_id)
        self.assertDictEqual(user_details, {'email': user_id, 'user_id': user_id})

    def test_get_user_detail_config_method(self) -> None:
        user_id = "dummy@email.com"
        response = {'email': user_id, 'user_id': user_id, 'first_name': 'First', 'last_name': 'Last'}

        def custom_function(id: str) -> Dict[str, Any]:
            return response

        self.app.config['USER_DETAIL_METHOD'] = custom_function

        user_details = self.proxy._get_user_details(user_id=user_id)
        self.assertDictEqual(user_details, response)
        self.app.config['USER_DETAIL_METHOD'] = None

    def test_get_owners_details_no_owner_no_fallback(self) -> None:
        res = self.proxy._get_owners(data_owners=list(), fallback_owner=None)
        self.assertEqual(len(res), 0)

    def test_get_owners_details_only_fallback(self) -> None:
        self.app.config['USER_DETAIL_METHOD'] = None
        user_id = "dummy@email.com"
        res = self.proxy._get_owners(data_owners=list(), fallback_owner=user_id)
        self.assertEqual(1, len(res))
        self.assertListEqual(res, [User(**{'email': user_id, 'user_id': user_id})])

    def test_get_owners_details_only_active(self) -> None:
        self.app.config['USER_DETAIL_METHOD'] = None
        data_owners = cast(dict, self.entity1)["relationshipAttributes"]["ownedBy"]
        # pass both active and inactive as parameter
        self.assertEqual(len(data_owners), 2)

        res = self.proxy._get_owners(data_owners=data_owners)
        # _get_owners should return only active
        self.assertEqual(1, len(res))
        self.assertEqual(res[0].user_id, 'active_owned_by')

    def test_get_owners_details_owner_and_fallback(self) -> None:
        self.app.config['USER_DETAIL_METHOD'] = None
        user_id = "dummy@email.com"

        data_owners = cast(dict, self.entity1)["relationshipAttributes"]["ownedBy"]
        # pass both active and inactive as parameter
        self.assertEqual(len(data_owners), 2)

        res = self.proxy._get_owners(data_owners=data_owners, fallback_owner=user_id)
        # _get_owners should return only active AND the fallback_owner
        self.assertEqual(2, len(res))
        self.assertEqual(res[1].user_id, user_id)

    def test_get_owners_details_owner_and_fallback_duplicates(self) -> None:
        self.app.config['USER_DETAIL_METHOD'] = None
        data_owners = cast(dict, self.entity1)["relationshipAttributes"]["ownedBy"]
        user_id = data_owners[0]["displayText"]
        self.assertEqual(len(data_owners), 2)

        res = self.proxy._get_owners(data_owners=data_owners, fallback_owner=user_id)
        # _get_owners should return only active AND the fallback_owner,
        # but in case where it is duplicate, should return only 1
        self.assertEqual(1, len(res))

    def test_get_table_watermarks(self) -> None:
        params = [(['%Y%m%d'], 2, '2020-09'),
                  (['%Y,%m'], 2, '2020-08'),
                  (['%Y-%m-%d'], 0, None),
                  ([], 0, None)]

        for supported_formats, expected_result_length, low_date_prefix in params:
            with self.subTest():
                self.app.config['WATERMARK_DATE_FORMATS'] = supported_formats

                result = self.proxy._get_table_watermarks(cast(dict, self.entity1))

                assert len(result) == expected_result_length

                if low_date_prefix:
                    low, _ = result

                    assert low.partition_value.startswith(low_date_prefix)

    def test_get_dashboard(self) -> None:
        self.proxy._get_dashboard = MagicMock(return_value=self.dashboard_data)  # type: ignore
        self.proxy._get_dashboard_group = MagicMock(return_value=self.dashboard_group_data)  # type: ignore
        self.proxy.client.entity.get_entities_by_guids = MagicMock(return_value=DottedDict({
            'entities': [DottedDict(self.entity1)]}))

        expected = DashboardDetail(uri='superset_dashboard://datalab.prod/1',
                                   cluster='datalab',
                                   group_name='prod superset',
                                   group_url='https://superset.prod',
                                   product='superset',
                                   name='Prod Usage',
                                   url='https://prod.superset/dashboards/1',
                                   description='Robs famous dashboard',
                                   created_timestamp=1619517099,
                                   updated_timestamp=1619626531,
                                   last_successful_run_timestamp=1619517099,
                                   last_run_timestamp=1619517150,
                                   last_run_state='failed',
                                   owners=[User(user_id='lisa_salinas', email='lisa_salinas', first_name=None,
                                                last_name=None, full_name=None, display_name=None, is_active=True,
                                                github_username=None, team_name=None, slack_id=None, employee_type=None,
                                                manager_fullname=None, manager_email=None, manager_id=None,
                                                role_name=None, profile_url=None, other_key_values={})],
                                   frequent_users=[],
                                   chart_names=['Count Users by Time', 'Total Count'],
                                   query_names=['User Count By Time', 'Total Count'],
                                   queries=[DashboardQuery(name='User Count By Time',
                                                           url='https://prod.superset/dashboards/1/query/1',
                                                           query_text='SELECT date, COUNT(1) FROM db.table GROUP BY 1'),
                                            DashboardQuery(name='Total Count',
                                                           url='https://prod.superset/dashboards/1/query/2',
                                                           query_text='SELECT COUNT(1) FROM db.table')],
                                   tables=[PopularTable(database='hive_table', cluster='TEST_CLUSTER', schema='TEST_DB',
                                                        name='Table1', description='Dummy Description')],
                                   tags=[],
                                   badges=[],
                                   recent_view_count=0)

        result = self.proxy.get_dashboard(id='superset_dashboard://datalab.prod/1')

        self.assertEqual(expected, result)

    def test_get_lineage_table(self) -> None:
        key = 'hive_table://demo.sample/table_2'
        resource_type = ResourceType.Table
        direction = 'both'
        depth = 3
        source = 'hive_table'

        expected_upstream = [LineageItem(key='hive_table://demo.sample/table_0',
                                         parent='',
                                         level=2, source=source, badges=[], usage=0),
                             LineageItem(key='hive_table://demo.sample/table_1',
                                         parent='hive_table://demo.sample/table_0',
                                         level=1, source=source, badges=[], usage=0),
                             LineageItem(key='hive_table://demo.sample/table_4',
                                         parent='',
                                         level=1, source=source, badges=[], usage=0)]

        expected_downstream = [LineageItem(key='hive_table://demo.sample/table_3',
                                           parent='hive_table://demo.sample/table_2',
                                           level=1, source=source, badges=[], usage=0)]

        expected = Lineage(key=key, direction=direction, depth=depth,
                           upstream_entities=expected_upstream, downstream_entities=expected_downstream)

        self.proxy.client.lineage.get_lineage_info = MagicMock(side_effect=[self.lineage_upstream_table_2,
                                                                            self.lineage_downstream_table_2])

        result = self.proxy.get_lineage(id=key,
                                        resource_type=resource_type,
                                        direction='both',
                                        depth=3)

        self.assertIsInstance(result, Lineage)
        self.assertEqual(expected, result)


if __name__ == '__main__':
    unittest.main()
