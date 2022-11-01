# pylint: disable=anomalous-backslash-in-string
import json
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Mapping, Optional, Sequence, Set, Tuple, Union

import toposort

import dagster._check as check
from dagster._annotations import experimental
from dagster._core.storage.pipeline_run import IN_PROGRESS_RUN_STATUSES, RunsFilter
from dagster._utils.merger import merge_dicts

from .asset_selection import AssetSelection
from .events import AssetKey
from .run_request import RunRequest
from .sensor_definition import DefaultSensorStatus, SensorDefinition
from .utils import check_valid_name

if TYPE_CHECKING:
    from dagster._core.definitions import AssetsDefinition, SourceAsset
    from dagster._core.instance import DagsterInstance
    from dagster._core.storage.event_log.base import EventLogRecord


def _get_upstream_mapping(
    selection,
    assets,
    source_assets,
) -> Mapping[AssetKey, Set[AssetKey]]:
    """Computes a mapping of assets in self._selection to their parents in the asset graph"""
    upstream = defaultdict(set)
    selection_resolved = list(selection.resolve([*assets, *source_assets]))
    for a in selection_resolved:
        a_parents = list(
            AssetSelection.keys(a).upstream(depth=1).resolve([*assets, *source_assets])
        )
        # filter out a because upstream() includes the assets in the original AssetSelection
        upstream[a] = {p for p in a_parents if p != a}
    return upstream


def _get_parent_updates(
    current_asset: AssetKey,
    parent_assets: Set[AssetKey],
    cursor: Optional[int],
    will_materialize_set: Set[AssetKey],
    wait_for_in_progress_runs: bool,
    instance_queryer: "CachingInstanceQueryer",
) -> Mapping[AssetKey, Tuple[bool, Optional[int]]]:
    """The bulk of the logic in the sensor is in this function. At the end of the function we return a
    dictionary that maps each asset to a Tuple. The Tuple contains a boolean, indicating if the asset
    has materialized or will materialize, and a storage ID
    the parent asset would update the cursor to if it is the most recent materialization of a parent asset.
    In some cases we set the tuple to (0.0, 0) so that the tuples of other parent materializations will take precedent.

    Args:
        current_asset: We want to determine if this asset should materialize, so we gather information about
            if its parents have materialized.
        parent_assets: the parents of current_asset.
        cursor: In the cursor for the sensor, we store the timestamp and storage id of the most recent materialization
            of all of current_asset's parents. This allows us to see if any of the parents have been materialized
            more recently.
        will_materialize_set: A set of all of the assets the sensor has already determined it will materialize.
            We check if the parent assets are in this list when determining their materialization status
        wait_for_in_progress_runs: If the user wants the sensor to wait for in progress runs of parent
            assets to complete before materializing current_asset.

    Here's how we get there:

    We want to get the materialization information for all of the parents of an asset to determine
    if we want to materialize the asset in this sensor tick. We also need determine the new cursor
    value for the asset so that we don't process the same materialization events for the parent
    assets again.

    We iterate through each parent of the asset and determine its materialization info. The parent
    asset's materialization status can be one of three options:
    1. The parent has materialized since the last time the child was materialized.
    2. The parent is slated to be materialized (i.e. included in will_materialize_set)
    3. The parent has not been materialized and will not be materialized by the sensor.

    In cases 1 and 2 we indicate that the parent has been updated by setting its value in
    parent_asset_event_records to True. For case 3 we set its value to False.

    If wait_for_in_progress_runs=True, there is another condition we want to check for.
    If any of the parents is currently being materialized we want to wait to materialize current_asset
    until the parent materialization is complete so that the asset can have the most up to date data.
    So, for each parent asset we check if it has a planned asset materialization event in a run that
    is currently in progress. If this is the case, we don't want current_asset to materialize, so we
    set parent_asset_event_records to False for all parents (so that if the sensor is set to
    materialize if any of the parents are updated, the sensor will still choose to not materialize
    the asset) and immediately return.
    """
    parent_asset_event_records: Dict[AssetKey, Tuple[bool, Optional[int]]] = {}

    for p in parent_assets:
        if p in will_materialize_set:
            # if p will be materialized by this sensor, then we can also materialize current_asset
            # we don't know what time asset p will be materialized so we set the cursor val to (0.0, 0)
            parent_asset_event_records[p] = (True, None)

        # TODO - when source asset versioning lands, add a check here that will see if the version has
        # updated if p is a source asset
        else:
            if wait_for_in_progress_runs:
                # if p is currently being materialized, then we don't want to materialize current_asset
                materialization_planned_event_record = (
                    instance_queryer.get_latest_planned_materialization_record(p)
                )

                if materialization_planned_event_record:
                    # see if the most recent planned materialization is part of an in progress run
                    if instance_queryer.is_run_in_progress(
                        materialization_planned_event_record.run_id
                    ):
                        # we don't want to materialize current_asset because p is
                        # being materialized. We'll materialize the asset on the next tick when the
                        # materialization of p is complete
                        parent_asset_event_records = {pp: (False, None) for pp in parent_assets}

                        return parent_asset_event_records

            parent_materialization_record = instance_queryer.get_latest_materialization_record(
                p, cursor
            )

            if parent_materialization_record:
                # if the run for the materialization of p also materialized current_asset, we
                # don't consider p "updated" when determining if current_asset should materialize.
                # we still update the cursor for p so this materialization isn't considered
                # on the next sensor tick.
                parent_updated = not instance_queryer.run_planned_to_materialize_asset(
                    current_asset, parent_materialization_record.run_id
                )
                parent_asset_event_records[p] = (
                    parent_updated,
                    parent_materialization_record.storage_id,
                )
            else:
                # p has not been materialized and will not be materialized by the sensor
                parent_asset_event_records[p] = (False, None)

    return parent_asset_event_records


def _make_sensor(
    selection: AssetSelection,
    name: str,
    wait_for_all_upstream: bool,
    wait_for_in_progress_runs: bool,
    minimum_interval_seconds: Optional[int],
    description: Optional[str],
    default_status: DefaultSensorStatus,
    run_tags: Optional[Mapping[str, str]],
) -> SensorDefinition:
    """Creates the sensor that will monitor the parents of all provided assets and determine
    which assets should be materialized (ie their parents have been updated).

    The cursor for this sensor is a dictionary mapping stringified AssetKeys to a tuple of timestamp and storage_id (float, int). For each
    asset we keep track of the timestamps and storage_id of the most recent materialization of a parent asset. For example
    if asset X has parents A, B, and C where A was materialized at 5:00 w/ storage_id 1, B at 5: 15  w/ storage_id 2 and
    C at 5:16 w/ storage_id 3. When the sensor runs, the cursor for X will be set to (5:16, 3). This way, the next time
    the sensor runs, we can ignore the materializations prior to time 5:16 and storage_id 3. If asset A materialized
    again at 5:20 w/ storage_id 4, we would know that this materialization has not been incorporated into the child asset yet.
    """

    def sensor_fn(context):
        latest_consumed_storage_ids_by_asset_key_str = (
            _deserialize_cursor_dict(context.cursor) if context.cursor else {}
        )
        run_requests, newly_consumed_storage_ids_by_asset_key_str = reconcile(
            repository_def=context._repository_def,  # pylint: disable=protected-access
            asset_selection=selection,
            wait_for_all_upstream=wait_for_all_upstream,
            wait_for_in_progress_runs=wait_for_in_progress_runs,
            instance=context.instance,
            latest_consumed_storage_ids_by_asset_key_str=latest_consumed_storage_ids_by_asset_key_str,
            run_tags=run_tags,
        )

        context.update_cursor(
            _serialize_cursor_dict(
                merge_dicts(
                    latest_consumed_storage_ids_by_asset_key_str,
                    newly_consumed_storage_ids_by_asset_key_str,
                )
            )
        )
        context._cursor_has_been_updated = True  # pylint: disable=protected-access
        return run_requests

    return SensorDefinition(
        evaluation_fn=sensor_fn,
        name=name,
        job_name="__ASSET_JOB",
        minimum_interval_seconds=minimum_interval_seconds,
        description=description,
        default_status=default_status,
    )


def reconcile(
    repository_def,
    asset_selection: AssetSelection,
    instance: "DagsterInstance",
    wait_for_in_progress_runs: bool,
    wait_for_all_upstream: bool,
    latest_consumed_storage_ids_by_asset_key_str: Mapping[str, int],
    run_tags: Mapping[str, str],
) -> Tuple[Sequence[RunRequest], Mapping[str, int]]:
    asset_defs_by_key = repository_def._assets_defs_by_key  # pylint: disable=protected-access
    source_asset_defs_by_key = (
        repository_def.source_assets_by_key  # pylint: disable=protected-access
    )
    upstream: Mapping[AssetKey, Set[AssetKey]] = _get_upstream_mapping(
        selection=asset_selection,
        assets=asset_defs_by_key.values(),
        source_assets=source_asset_defs_by_key.values(),
    )

    should_materialize: Set[AssetKey] = set()
    newly_consumed_storage_ids_by_asset_key_str: Dict[str, int] = {}

    # sort the assets topologically so that we process them in order
    toposort_assets = list(toposort.toposort(upstream))
    # unpack the list of sets into a list and only keep the ones we are monitoring
    toposort_assets = [
        asset for layer in toposort_assets for asset in layer if asset in upstream.keys()
    ]

    instance_queryer = CachingInstanceQueryer(instance)

    # determine which assets should materialize based on the materialization status of their
    # parents
    for current_asset_key in toposort_assets:
        current_asset_cursor = latest_consumed_storage_ids_by_asset_key_str.get(
            str(current_asset_key)
        )

        parent_update_records = _get_parent_updates(
            current_asset=current_asset_key,
            parent_assets=upstream[current_asset_key],
            cursor=current_asset_cursor,
            will_materialize_set=should_materialize,
            wait_for_in_progress_runs=wait_for_in_progress_runs,
            instance_queryer=instance_queryer,
        )

        condition = all if wait_for_all_upstream else any
        if condition(
            materialization_status for materialization_status, _ in parent_update_records.values()
        ):
            should_materialize.add(current_asset_key)

            # get the cursor value by selecting the max of all the candidates.
            cursor_update_candidates = [
                cursor_val for _, cursor_val in parent_update_records.values() if cursor_val
            ] + ([current_asset_cursor] if current_asset_cursor else [])
            if cursor_update_candidates:
                newly_consumed_storage_ids_by_asset_key_str[str(current_asset_key)] = max(
                    cursor_update_candidates
                )

    run_requests = []
    if len(should_materialize) > 0:
        run_requests = [
            RunRequest(run_key=None, asset_selection=list(should_materialize), tags=run_tags)
        ]
    else:
        run_requests = []

    return run_requests, newly_consumed_storage_ids_by_asset_key_str


def _serialize_cursor_dict(cursor_dict: Dict[str, int]) -> str:
    return json.dumps(cursor_dict)


def _deserialize_cursor_dict(cursor: str) -> Dict[str, int]:
    return json.loads(cursor)


class CachingInstanceQueryer:
    def __init__(self, instance: "DagsterInstance"):
        self._instance = instance
        self._latest_materialization_record_cache: Dict[AssetKey, "EventLogRecord"] = {}
        # if we try to fetch the latest materialization record after a given cursor and don't find
        # anything, we can keep track of that fact, so that the next time try to fetch the latest
        # materialization record for a >= cursor, we don't need to query the instance
        self._no_materializations_after_cursor_cache: Dict[AssetKey, int] = {}
        self._latest_planned_materialization_cache: Dict[AssetKey, "EventLogRecord"] = {}
        self._run_planned_materializations_cache: Dict[str, Set[AssetKey]] = {}
        self._is_run_in_progress_cache: Dict[str, bool] = {}

    def get_latest_materialization_record(
        self, asset_key: AssetKey, after_cursor
    ) -> Optional["EventLogRecord"]:
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        if asset_key in self._latest_materialization_record_cache:
            cached_record = self._latest_materialization_record_cache[asset_key]
            if after_cursor is None or after_cursor < cached_record.storage_id:
                return cached_record
            else:
                return None
        elif asset_key in self._no_materializations_after_cursor_cache:
            if (
                after_cursor is not None
                and after_cursor >= self._no_materializations_after_cursor_cache[asset_key]
            ):
                return None

        materialization_records = self._instance.get_event_records(
            EventRecordsFilter(
                event_type=DagsterEventType.ASSET_MATERIALIZATION,
                asset_key=asset_key,
                after_cursor=after_cursor,
            ),
            ascending=False,
            limit=1,
        )

        if materialization_records:
            self._latest_materialization_record_cache[asset_key] = materialization_records[0]
            return materialization_records[0]
        else:
            if after_cursor is not None:
                self._no_materializations_after_cursor_cache[asset_key] = min(
                    after_cursor,
                    self._no_materializations_after_cursor_cache.get(asset_key, after_cursor),
                )
            return None

    def get_latest_planned_materialization_record(
        self, asset_key: AssetKey
    ) -> Optional["EventLogRecord"]:
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        if asset_key in self._latest_planned_materialization_cache.keys():
            return self._latest_planned_materialization_cache[asset_key]
        else:
            materialization_planned_event_records = self._instance.get_event_records(
                EventRecordsFilter(
                    event_type=DagsterEventType.ASSET_MATERIALIZATION_PLANNED,
                    asset_key=asset_key,
                ),
                ascending=False,
                limit=1,
            )

            if materialization_planned_event_records:
                self._latest_planned_materialization_cache[
                    asset_key
                ] = materialization_planned_event_records[0]
                return materialization_planned_event_records[0]

        return None

    def run_planned_to_materialize_asset(self, asset_key: AssetKey, run_id: str) -> bool:
        from dagster._core.events import DagsterEventType

        if run_id not in self._run_planned_materializations_cache:
            materialization_planned_records = self._instance.get_records_for_run(
                run_id=run_id,
                of_type=DagsterEventType.ASSET_MATERIALIZATION_PLANNED,
            ).records
            self._run_planned_materializations_cache[run_id] = set(
                event.asset_key for event in materialization_planned_records
            )

        return asset_key in self._run_planned_materializations_cache[run_id]

    def is_run_in_progress(self, run_id: str) -> bool:
        if run_id not in self._is_run_in_progress_cache:
            self._is_run_in_progress_cache[run_id] = bool(
                self._instance.get_runs(
                    filters=RunsFilter(run_ids=[run_id], statuses=IN_PROGRESS_RUN_STATUSES)
                )
            )

        return self._is_run_in_progress_cache[run_id]


@experimental
def build_asset_reconciliation_sensor(
    asset_selection: AssetSelection,
    name: str,
    wait_for_all_upstream: bool = False,
    wait_for_in_progress_runs: bool = True,
    minimum_interval_seconds: Optional[int] = None,
    description: Optional[str] = None,
    default_status: DefaultSensorStatus = DefaultSensorStatus.STOPPED,
    run_tags: Optional[Mapping[str, str]] = None,
) -> SensorDefinition:
    """Constructs a sensor that will monitor the parents of the provided assets and materialize an asset
    based on the materialization of its parents. This will keep the monitored assets up to date with the
    latest data available to them. The sensor defaults to materializing an asset when all of
    its parents have materialized, but it can be set to materialize an asset when any of its
    parents have materialized.

    **Note:** Currently, this sensor only works for non-partitioned assets.

    Args:
        asset_selection (AssetSelection): The group of assets you want to keep up-to-date
        name (str): The name to give the sensor.
        wait_for_all_upstream (bool): If True, the sensor will only materialize an asset when
            all of its parents have materialized. If False, the sensor will materialize an asset when
            any of its parents have materialized. Defaults to False.
        wait_for_in_progress_runs (bool): If True, the sensor will not materialize an
            asset if there is an in-progress run that will materialize any of the asset's parents.
            Defaults to True.
        minimum_interval_seconds (Optional[int]): The minimum amount of time that should elapse between sensor invocations.
        description (Optional[str]): A description for the sensor.
        default_status (DefaultSensorStatus): Whether the sensor starts as running or not. The default
            status can be overridden from Dagit or via the GraphQL API.
        run_tags (Optional[Mapping[str, str]): Dictionary of tags to pass to the RunRequests launched by this sensor

    Returns:
        A SensorDefinition that will monitor the parents of the provided assets to determine when
        the provided assets should be materialized

    Example:
        If you have the following asset graph:

        .. code-block:: python

            a       b       c
             \     / \     /
                d       e
                 \     /
                    f

        and create the sensor:

        .. code-block:: python

            build_asset_reconciliation_sensor(
                AssetSelection.assets(d, e, f),
                name="my_reconciliation_sensor",
                wait_for_all_upstream=True,
                wait_for_in_progress_runs=True
            )

        You will observe the following behavior:
            * If ``a``, ``b``, and ``c`` are all materialized, then on the next sensor tick, the sensor will see that ``d`` and ``e`` can
              be materialized. Since ``d`` and ``e`` will be materialized, ``f`` can also be materialized. The sensor will kick off a
              run that will materialize ``d``, ``e``, and ``f``.
            * If on the next sensor tick, ``a``, ``b``, and ``c`` have not been materialized again the sensor will not launch a run.
            * If before the next sensor tick, just asset ``a`` and ``b`` have been materialized, the sensor will launch a run to
              materialize ``d``.
            * If asset ``c`` is materialized by the next sensor tick, the sensor will see that ``e`` can be materialized (since ``b`` and
              ``c`` have both been materialized since the last materialization of ``e``). The sensor will also see that ``f`` can be materialized
              since ``d`` was updated in the previous sensor tick and ``e`` will be materialized by the sensor. The sensor will launch a run
              the materialize ``e`` and ``f``.
            * If by the next sensor tick, only asset ``b`` has been materialized. The sensor will not launch a run since ``d`` and ``e`` both have
              a parent that has not been updated.
            * If during the next sensor tick, there is a materialization of ``a`` in progress, the sensor will not launch a run to
              materialize ``d``. Once ``a`` has completed materialization, the next sensor tick will launch a run to materialize ``d``.

        **Other considerations:**
            If an asset has a SourceAsset as a parent, and that source asset points to an external data source (ie the
            source asset does not point to an asset in another repository), the sensor will not know when to consider
            the source asset "materialized". If you have the asset graph:

            .. code-block:: python

                x   external_data_source
                 \       /
                     y

            and create the sensor:

            .. code-block:: python

                build_asset_reconciliation_sensor(
                    AssetSelection.assets(y),
                    name="my_reconciliation_sensor",
                    wait_for_all_upstream=True,
                    wait_for_in_progress_runs=True
                )

            ``y`` will never be updated because ``external_data_source`` is never considered "materialized. In this case you should create the
            sensor

            .. code-block:: python

                build_asset_reconciliation_sensor(
                    AssetSelection.assets(y),
                    name="my_reconciliation_sensor",
                    wait_for_all_upstream=False,
                    wait_for_in_progress_runs=True
                )

            which will cause ``y`` to be materialized when ``x`` is materialized.
    """
    check_valid_name(name)
    check.opt_dict_param(run_tags, "run_tags", key_type=str, value_type=str)
    return _make_sensor(
        selection=asset_selection,
        name=name,
        wait_for_all_upstream=wait_for_all_upstream,
        wait_for_in_progress_runs=wait_for_in_progress_runs,
        minimum_interval_seconds=minimum_interval_seconds,
        description=description,
        default_status=default_status,
        run_tags=run_tags,
    )
