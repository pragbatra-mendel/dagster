import logging
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable

from sqlalchemy.pool import NullPool

from dagster._core.storage.event_log.base import EventLogCursor
from dagster._core.storage.sql import create_engine, get_alembic_config, stamp_alembic_rev
from dagster._core.storage.sqlite import create_in_memory_conn_string
from dagster._serdes import ConfigurableClass

from .schema import SqlEventLogStorageMetadata
from .sql_event_log import SqlEventLogStorage


class InMemoryEventLogStorage(SqlEventLogStorage, ConfigurableClass):
    """
    In memory only event log storage. Used by ephemeral DagsterInstance or for testing purposes.

    WARNING: Dagit and other core functionality will not work if this is used on a real DagsterInstance
    """

    def __init__(self, inst_data=None, preload=None):
        self._inst_data = inst_data
        self._conn = self._create_connection()
        self._handlers = defaultdict(set)
        self._storage_id = 0  # mirror the storage id, to mimic watching cursors

        self.reindex_events()
        self.reindex_assets()

        if preload:
            for payload in preload:
                for event in payload.event_list:
                    self.store_event(event)

    def _create_connection(self):
        engine = create_engine(create_in_memory_conn_string("event_log"), poolclass=NullPool)
        conn = engine.connect()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        SqlEventLogStorageMetadata.create_all(conn)
        alembic_config = get_alembic_config(__file__, "sqlite/alembic/alembic.ini")
        stamp_alembic_rev(alembic_config, conn)
        return conn

    @contextmanager
    def run_connection(self, run_id=None):
        yield self._conn

    @contextmanager
    def index_connection(self):
        yield self._conn

    @property
    def inst_data(self):
        return self._inst_data

    @classmethod
    def config_type(cls):
        return {}

    @classmethod
    def from_config_value(cls, inst_data, config_value):
        return cls(inst_data)

    def upgrade(self):
        pass

    def store_event(self, event):
        super(InMemoryEventLogStorage, self).store_event(event)
        self._storage_id += 1

        handlers = list(self._handlers[event.run_id])
        for handler in handlers:
            try:
                handler(event, str(EventLogCursor.from_storage_id(self._storage_id)))
            except Exception:
                logging.exception("Exception in callback for event watch on run %s.", event.run_id)

    def watch(self, run_id: str, cursor: str, callback: Callable):
        self._handlers[run_id].add(callback)

    def end_watch(self, run_id: str, handler: Callable):
        if handler in self._handlers[run_id]:
            self._handlers[run_id].remove(handler)

    @property
    def is_persistent(self) -> bool:
        return False
