# coding=utf-8
"""
API for dataset indexing, access and search.
"""
from __future__ import absolute_import

import logging

import cachetools

from datacube import compat
from datacube.utils import InvalidDocException, check_doc_unchanged, jsonify_document
from datacube.model import Dataset, DatasetType, MetadataType
from .exceptions import DuplicateRecordError
from . import fields

_LOG = logging.getLogger(__name__)


class MetadataTypeResource(object):
    def __init__(self, db):
        """
        :type db: datacube.index.postgres._api.PostgresDb
        """
        self._db = db

    def add(self, definition, allow_table_lock=False):
        """
        :type definition: dict
        :param allow_table_lock:
            Allow an exclusive lock to be taken on the table while creating the indexes.
            This will halt other user's requests until completed.

            If false, creation will be slightly slower and cannot be done in a transaction.
        :rtype: datacube.model.MetadataType
        """
        # This column duplication is getting out of hand:
        MetadataType.validate(definition)

        name = definition['name']

        existing = self._db.get_metadata_type_by_name(name)
        if existing:
            # They've passed us the same one again. Make sure it matches what is stored.
            # TODO: Support for adding/updating search fields?
            check_doc_unchanged(
                existing.definition,
                definition,
                'Metadata Type {}'.format(name)
            )
        else:
            self._db.add_metadata_type(
                name=name,
                definition=definition,
                concurrently=not allow_table_lock
            )
        return self.get_by_name(name)

    @cachetools.cached(cachetools.TTLCache(100, 60))
    def get(self, id_):
        """
        :rtype datacube.model.MetadataType
        """
        return self._make(self._db.get_metadata_type(id_))

    @cachetools.cached(cachetools.TTLCache(100, 60))
    def get_by_name(self, name):
        """
        :rtype datacube.model.MetadataType
        """
        record = self._db.get_metadata_type_by_name(name)
        if not record:
            return None
        return self._make(record)

    def check_field_indexes(self, allow_table_lock=False, rebuild_all=False):
        """
        Create or replace per-field indexes and views.
        :param allow_table_lock:
            Allow an exclusive lock to be taken on the table while creating the indexes.
            This will halt other user's requests until completed.

            If false, creation will be slightly slower and cannot be done in a transaction.
        """
        self._db.check_dynamic_fields(concurrently=not allow_table_lock, rebuild_all=rebuild_all)

    def _make_many(self, query_rows):
        return (self._make(c) for c in query_rows)

    def _make(self, query_row):
        """
        :rtype list[datacube.model.MetadataType]
        """
        definition = query_row['definition']
        dataset_ = definition['dataset']
        return MetadataType(
            query_row['name'],
            dataset_,
            dataset_search_fields=self._db.get_dataset_fields(query_row),
            id_=query_row['id']
        )


class DatasetTypeResource(object):
    """
    :type _db: datacube.index.postgres._api.PostgresDb
    :type metadata_type_resource: MetadataTypeResource
    """
    def __init__(self, db, metadata_type_resource):
        """
        :type db: datacube.index.postgres._api.PostgresDb
        :type metadata_type_resource: MetadataTypeResource
        """
        self._db = db
        self.metadata_type_resource = metadata_type_resource

    def from_doc(self, definition):
        """
        :type definition: dict
        :rtype: datacube.model.DatasetType
        """
        # This column duplication is getting out of hand:
        DatasetType.validate(definition)

        metadata_type = definition['metadata_type']

        # They either specified the name of a metadata type, or specified a metadata type.
        # Is it a name?
        if isinstance(metadata_type, compat.string_types):
            metadata_type = self.metadata_type_resource.get_by_name(metadata_type)
        else:
            # Otherwise they embedded a document, add it if needed:
            metadata_type = self.metadata_type_resource.add(metadata_type, allow_table_lock=False)

        if not metadata_type:
            raise InvalidDocException('Unknown metadata type: %r' % definition['metadata_type'])

        return DatasetType(metadata_type, definition)

    def add(self, type_):
        """
        :type type_: datacube.model.DatasetType
        :rtype: datacube.model.DatasetType
        """
        DatasetType.validate(type_.definition)

        existing = self._db.get_dataset_type_by_name(type_.name)
        if existing:
            # TODO: Support for adding/updating match rules?
            # They've passed us the same collection again. Make sure it matches what is stored.
            check_doc_unchanged(
                existing.definition,
                type_.definition,
                'Dataset type {}'.format(type_.name)
            )
        else:
            self._db.add_dataset_type(
                name=type_.name,
                metadata=type_.metadata,
                metadata_type_id=type_.metadata_type.id,
                definition=type_.definition
            )
        return self.get_by_name(type_.name)

    def add_document(self, definition):
        """
        :type definition: dict
        :rtype: datacube.model.DatasetType
        """
        type_ = self.from_doc(definition)
        return self.add(type_)

    def add_many(self, definitions):
        """
        :type definitions: list[dict]
        """
        for definition in definitions:
            self.add_document(definition)

    @cachetools.cached(cachetools.TTLCache(100, 60))
    def get(self, id_):
        """
        :rtype datacube.model.DatasetType
        """
        return self._make(self._db.get_dataset_type(id_))

    @cachetools.cached(cachetools.TTLCache(100, 60))
    def get_by_name(self, name):
        """
        :rtype datacube.model.DatasetType
        """
        result = self._db.get_dataset_type_by_name(name)
        if not result:
            return None
        return self._make(result)

    def get_with_fields(self, field_names):
        """
        Return dataset types that have all the given fields.
        :type field_names: tuple[str]
        :rtype: __generator[DatasetType]
        """
        for type_ in self.get_all():
            for name in field_names:
                if name not in type_.metadata_type.dataset_fields:
                    break
            else:
                yield type_

    def get_all(self):
        """
        :rtype: iter[datacube.model.DatasetType]
        """
        return (self._make(record) for record in self._db.get_all_dataset_types())

    def _make_many(self, query_rows):
        return (self._make(c) for c in query_rows)

    def _make(self, query_row):
        """
        :rtype datacube.model.DatasetType
        """
        return DatasetType(
            definition=query_row['definition'],
            metadata_type=self.metadata_type_resource.get(query_row['metadata_type_ref']),
            id_=query_row['id'],
        )


class DatasetResource(object):
    """
    :type _db: datacube.index.postgres._api.PostgresDb
    :type types: datacube.index._datasets.DatasetTypeResource
    """
    def __init__(self, db, dataset_type_resource):
        """
        :type db: datacube.index.postgres._api.PostgresDb
        :type dataset_type_resource: datacube.index._datasets.DatasetTypeResource
        """
        self._db = db
        self.types = dataset_type_resource

    def get(self, id_, include_sources=False):
        """
        Get dataset by id

        :param include_sources: get the full provenance graph?
        :rtype: datacube.model.Dataset
        """
        if not include_sources:
            return self._make(self._db.get_dataset(id_))

        datasets = {result['id']: (self._make(result), result) for result in self._db.get_dataset_sources(id_)}
        for dataset, result in datasets.values():
            dataset.metadata_doc['lineage']['source_datasets'] = {
                classifier: datasets[str(source)][0].metadata_doc
                for source, classifier in zip(result['sources'], result['classes']) if source
                }
            dataset.sources = {
                classifier: datasets[str(source)][0]
                for source, classifier in zip(result['sources'], result['classes']) if source
            }
        return datasets[id_][0]

    def get_derived(self, id_):
        return [self._make(result) for result in self._db.get_derived_datasets(id_)]

    def has(self, dataset):
        """
        Have we already indexed this dataset?

        :type dataset: datacube.model.Dataset
        :rtype: bool
        """
        return self._db.contains_dataset(dataset.id)

    def add(self, dataset, skip_sources=False):
        """
        Ensure a dataset is in the index. Add it if not present.

        :type dataset: datacube.model.Dataset
        :param bool skip_sources: use when sources are already indexed
        :rtype: datacube.model.Dataset
        """
        if not skip_sources:
            for source in dataset.sources.values():
                self.add(source)

        was_inserted = False
        sources_tmp = dataset.type.dataset_reader(dataset.metadata_doc).sources
        dataset.type.dataset_reader(dataset.metadata_doc).sources = {}
        try:
            _LOG.info('Indexing %s', dataset.id)
            with self._db.begin() as transaction:
                try:
                    was_inserted = self._db.insert_dataset(dataset.metadata_doc, dataset.id, dataset.type.id)
                    for classifier, source_dataset in dataset.sources.items():
                        self._db.insert_dataset_source(classifier, dataset.id, source_dataset.id)
                except DuplicateRecordError as e:
                    _LOG.warning(str(e))

            if not was_inserted:
                existing = self.get(dataset.id)
                if existing:
                    check_doc_unchanged(
                        existing.metadata_doc,
                        jsonify_document(dataset.metadata_doc),
                        'Dataset {}'.format(dataset.id)
                    )
        finally:
            dataset.type.dataset_reader(dataset.metadata_doc).sources = sources_tmp

        if dataset.local_uri:
            try:
                self._db.ensure_dataset_location(dataset.id, dataset.local_uri)
            except DuplicateRecordError as e:
                _LOG.warning(str(e))

        return dataset

    def replace(self, old_datasets, new_datasets):
        """
        :type old_datasets: list[datacube.model.Dataset]
        :type new_datasets: list[datacube.model.Dataset]
        """
        with self._db.begin() as transaction:
            for unit in old_datasets:
                self._db.archive_storage_unit(unit.id)

            for unit in new_datasets:
                unit = self.add(unit)
                _LOG.debug('Indexed dataset %s @ %s', unit.id, unit.local_uri)

    def get_field_names(self, type_name=None):
        """
        :type type_name: str
        :rtype: __generator[str]
        """
        if type_name is None:
            types = self.types.get_all()
        else:
            types = [self.types.get_by_name(type_name)]

        for type_ in types:
            for name in type_.metadata_type.dataset_fields:
                yield name

    def get_locations(self, dataset):
        """
        :type dataset: datacube.model.Dataset
        :rtype: list[str]
        """
        return self._db.get_locations(dataset.id)

    def _make(self, dataset_res):
        """
        :rtype datacube.model.Dataset
        """
        return Dataset(
            self.types.get(dataset_res.dataset_type_ref),
            dataset_res.metadata,
            dataset_res.local_uri
        )

    def _make_many(self, query_result):
        """
        :rtype list[datacube.model.Dataset]
        """
        return (self._make(dataset) for dataset in query_result)

    def search_by_metadata(self, metadata):
        """
        Perform a search using arbitrary metadata, returning results as Dataset objects.

        Caution – slow! This will usually not use indexes.

        :type metadata: dict
        :rtype: list[datacube.model.Dataset]
        """
        return self._make_many(self._db.search_datasets_by_metadata(metadata))

    def search(self, **query):
        """
        Perform a search, returning results as Dataset objects.
        :type query: dict[str,str|float|datacube.model.Range]
        :rtype: __generator[datacube.model.Dataset]
        """
        return self._make_many(self._do_search(query))

    def count(self, **query):
        """
        Perform a search, returning count of results.
        :type query: dict[str,str|float|datacube.model.Range]
        :rtype: int
        """
        return self._do_count(query)

    def _get_dataset_types(self, q):
        types = set()
        if 'product' in q.keys():
            types.add(self.types.get_by_name(q['product']))
        else:
            # Otherwise search any metadata type that has all the given search fields.
            types = self.types.get_with_fields(tuple(q.keys()))
            if not types:
                raise ValueError('No type of dataset has fields: %r', tuple(q.keys()))

        return types

    def _do_search(self, query, return_fields=False, with_source_ids=False):
        q = dict(query)
        dataset_types = self._get_dataset_types(q)

        # We don't need to match product name as we're searching via product (ie 'dataset_type')
        if 'product' in q:
            del q['product']

        # Perform one search per type.
        for dataset_type in dataset_types:
            q['dataset_type_id'] = dataset_type.id
            dataset_fields = dataset_type.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            select_fields = None
            if return_fields:
                select_fields = tuple(dataset_fields.values())
            for dataset in self._db.search_datasets(query_exprs,
                                                    select_fields=select_fields,
                                                    with_source_ids=with_source_ids):
                yield dataset

    def _do_count(self, query):
        q = dict(query)
        dataset_types = self._get_dataset_types(q)
        result = 0

        # We don't need to match product name as we're searching via product (ie 'dataset_type')
        if 'product' in q:
            del q['product']

        for dataset_type in dataset_types:
            q['dataset_type_id'] = dataset_type.id
            dataset_fields = dataset_type.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            result += self._db.count_datasets(query_exprs)
        return result

    def search_summaries(self, **query):
        """
        Perform a search, returning just the search fields of each dataset.

        :type query: dict[str,str|float|datacube.model.Range]
        :rtype: dict
        """
        return (
            dict(fs) for fs in
            self._do_search(
                query,
                return_fields=True
            )
        )

    def search_eager(self, **query):
        """
        :type query: dict[str,str|float|datacube.model.Range]
        :rtype: list[datacube.model.Dataset]
        """
        return list(self.search(**query))
