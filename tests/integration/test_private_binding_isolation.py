"""Private connector bindings must remain isolated across users and agent revocation."""

from app.services import agent_service, auth_service, connector_service, credential_service


def mcp(client, token, tool, params):
    return client.post('/mcp', headers={'Authorization': f'Bearer {token}'},
                       json={'tool': tool, 'params': params})


def test_private_binding_denies_second_human_and_revokes_with_agent(test_client):
    for user in ('owner-one', 'second-human'):
        auth_service.create_user(user, f'{user}@test.local', 'testpassword123', user, 'user')
    _, coordinator_token=agent_service.create_agent(
        'coordinator-fixture','Coordinator fixture','owner-one',default_user_id='owner-one',
        read_scopes=[],write_scopes=[])
    _, second_token=agent_service.create_agent(
        'second-agent','Second human fixture','second-human',default_user_id='second-human',
        read_scopes=[],write_scopes=[])
    connector_service.create_connector_type(
        connector_type_id='example_read',display_name='Example Read',auth_type='api_key',
        supported_actions=[{'name':'list_reports','side_effect':'read'}],backend_type='generic_http')
    credential=credential_service.create_credential(
        'agent:coordinator-fixture','Example read fixture','fixture-secret',created_by='owner-one')
    binding=connector_service.create_binding(
        'example_read','Example Read','agent:coordinator-fixture',
        credential_id=credential['id'],logical_alias='private',created_by='owner-one')

    owner=mcp(test_client,coordinator_token,'connectors_resolve',
              {'connector_type_id':'example_read','logical_alias':'private'})
    assert owner.status_code==200,owner.json()
    assert owner.json()['data']['binding']['id']==binding['id']
    other=mcp(test_client,second_token,'connectors_bindings_list',{})
    assert other.status_code==200,other.json()
    assert binding['id'] not in {item['id'] for item in other.json()['data']['bindings']}
    other_scope=mcp(test_client,second_token,'connectors_bindings_list',{'scope':'agent:coordinator-fixture'})
    assert other_scope.status_code==403,other_scope.json()
    other_resolve=mcp(test_client,second_token,'connectors_resolve',
                      {'connector_type_id':'example_read','logical_alias':'private'})
    assert other_resolve.json()['ok'] is False
    assert other_resolve.json().get('data',{}).get('binding') is None

    assert agent_service.deactivate_agent('coordinator-fixture')
    revoked=mcp(test_client,coordinator_token,'connectors_resolve',
                {'connector_type_id':'example_read','logical_alias':'private'})
    assert revoked.status_code==401,revoked.json()
