import pytest
import responses
from dagster_fivetran import fivetran_resource
from dagster_fivetran.asset_defs import load_assets_from_fivetran_instance
from dagster_fivetran_tests.utils import (
    DEFAULT_CONNECTOR_ID,
    get_complex_sample_connector_schema_config,
    get_sample_connectors_response,
    get_sample_groups_response,
)

from dagster import AssetKey, build_init_resource_context
from dagster._core.definitions.metadata import MetadataValue
from dagster._core.definitions.metadata.table import TableColumn, TableSchema


@responses.activate
@pytest.mark.parametrize("connector_to_group_fn", [None, lambda x: f"{x[0]}_group"])
@pytest.mark.parametrize("filter_connector", [True, False])
def test_load_from_instance(connector_to_group_fn, filter_connector):

    ft_resource = fivetran_resource(
        build_init_resource_context(
            config={
                "api_key": "some_key",
                "api_secret": "some_secret",
            }
        )
    )
    ft_instance = fivetran_resource.configured(
        {
            "api_key": "some_key",
            "api_secret": "some_secret",
        }
    )

    with responses.RequestsMock() as rsps:
        rsps.add(
            method=rsps.GET,
            url=ft_resource.api_base_url + "groups",
            json=get_sample_groups_response(),
            status=200,
        )
        rsps.add(
            method=rsps.GET,
            url=ft_resource.api_base_url + "groups/some_group/connectors",
            json=get_sample_connectors_response(),
            status=200,
        )
        rsps.add(
            rsps.GET,
            f"{ft_resource.api_connector_url}{DEFAULT_CONNECTOR_ID}/schemas",
            json=get_complex_sample_connector_schema_config(),
        )

        if connector_to_group_fn:
            ft_cacheable_assets = load_assets_from_fivetran_instance(
                ft_instance,
                connector_to_group_fn=connector_to_group_fn,
                connector_filter=(lambda _: False) if filter_connector else None,
            )
        else:
            ft_cacheable_assets = load_assets_from_fivetran_instance(
                ft_instance,
                connector_filter=(lambda _: False) if filter_connector else None,
            )
        ft_assets = ft_cacheable_assets.build_definitions(
            ft_cacheable_assets.compute_cacheable_data()
        )

    if filter_connector:
        assert len(ft_assets) == 0
        return

    tables = {
        AssetKey(["xyz1", "abc2"]),
        AssetKey(["xyz1", "abc1"]),
        AssetKey(["abc", "xyz"]),
        AssetKey(["qwerty", "fed"]),
        AssetKey(["qwerty", "bar"]),
    }

    # # Check schema metadata is added correctly to asset def

    assert any(
        out.metadata.get("table_schema")
        == MetadataValue.table_schema(
            TableSchema(
                columns=[
                    TableColumn(name="column_1", type="any"),
                    TableColumn(name="column_2", type="any"),
                    TableColumn(name="column_3", type="any"),
                ]
            )
        )
        for out in ft_assets[0].node_def.output_defs
    )

    assert ft_assets[0].keys == tables
    assert all(
        [
            ft_assets[0].group_names_by_key.get(t)
            == (
                connector_to_group_fn("some_service.some_name")
                if connector_to_group_fn
                else "some_service_some_name"
            )
            for t in tables
        ]
    )
    assert len(ft_assets[0].op.output_defs) == len(tables)
