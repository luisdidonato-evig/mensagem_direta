from conftest import login_headers


def create_attendance(client, inbound_payload):
    response = client.post("/api/v1/inbox/messages", json=inbound_payload)
    assert response.status_code == 201
    return response.json()["attendance"]


def test_panel_is_served(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Central de atendimento" in response.text


def test_inbound_message_is_idempotent(client, inbound_payload):
    first = client.post("/api/v1/inbox/messages", json=inbound_payload)
    second = client.post("/api/v1/inbox/messages", json=inbound_payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["message"]["id"] == second.json()["message"]["id"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


def test_only_one_agent_can_claim(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)

    winner = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )
    loser = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=login_headers(client, "agente-2"),
    )

    assert winner.status_code == 200
    assert winner.json()["assignee_id"] == "agente-1"
    assert winner.json()["status"] == "EM_ATENDIMENTO"
    assert loser.status_code == 409
    assert loser.json()["detail"] == "Atendimento já assumido"


def test_vertical_slice(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()

    sent = client.post(
        f"/api/v1/attendances/{attendance['id']}/messages",
        json={"client_message_id": "client-msg-1", "content": "Como posso ajudar?"},
        headers=agent_headers,
    )
    assert sent.status_code == 201
    assert sent.json()["delivery_status"] == "PENDENTE"

    waiting = client.patch(
        f"/api/v1/attendances/{attendance['id']}/status",
        json={"status": "AGUARDANDO_CLIENTE", "expected_version": claimed["version"]},
        headers=agent_headers,
    )
    assert waiting.status_code == 200

    reply = dict(inbound_payload)
    reply.update(
        external_event_id="evt-2",
        external_message_id="msg-2",
        content="Meu pedido não chegou",
    )
    resumed = client.post("/api/v1/inbox/messages", json=reply)
    assert resumed.status_code == 201
    assert resumed.json()["attendance"]["status"] == "EM_ATENDIMENTO"

    current = resumed.json()["attendance"]
    closed = client.patch(
        f"/api/v1/attendances/{attendance['id']}/status",
        json={
            "status": "ENCERRADO",
            "expected_version": current["version"],
            "closure_reason": "RESOLVIDO",
        },
        headers=agent_headers,
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "ENCERRADO"

    detail = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    )
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 4  # inclui pesquisa de satisfação
    assert [event["type"] for event in detail.json()["events"]] == [
        "CRIADO",
        "MENSAGEM_RECEBIDA",
        "ASSUMIDO",
        "MENSAGEM_ENVIADA",
        "STATUS_ALTERADO",
        "STATUS_ALTERADO",
        "MENSAGEM_RECEBIDA",
        "STATUS_ALTERADO",
    ]


def test_agent_cannot_operate_another_agents_attendance(
    client, inbound_payload, agent_headers
):
    attendance = create_attendance(client, inbound_payload)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()

    response = client.patch(
        f"/api/v1/attendances/{attendance['id']}/status",
        json={
            "status": "ENCERRADO",
            "expected_version": claimed["version"],
            "closure_reason": "RESOLVIDO",
        },
        headers=login_headers(client, "agente-2"),
    )

    assert response.status_code == 403


def test_supervisor_can_transfer(
    client, inbound_payload, agent_headers, supervisor_headers
):
    attendance = create_attendance(client, inbound_payload)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()

    transferred = client.post(
        f"/api/v1/attendances/{attendance['id']}/transfer",
        json={"target_actor_id": "agente-2", "expected_version": claimed["version"]},
        headers=supervisor_headers,
    )

    assert transferred.status_code == 200
    assert transferred.json()["assignee_id"] == "agente-2"


def test_assigned_agent_updates_contact_profile(
    client, inbound_payload, agent_headers
):
    attendance = create_attendance(client, inbound_payload)
    client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )

    updated = client.put(
        f"/api/v1/contacts/{inbound_payload['contact_id']}",
        json={
            "display_name": "Cliente Demonstração",
            "phone": "+55 11 99999-0000",
            "tags": ["Prioridade", "retorno", "prioridade"],
        },
        headers=agent_headers,
    )

    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Cliente Demonstração"
    assert updated.json()["tags"] == ["Prioridade", "retorno"]
    summary = client.get("/api/v1/attendances", headers=agent_headers).json()[0]
    assert summary["contact_display_name"] == "Cliente Demonstração"
    assert summary["contact_phone"] == "+55 11 99999-0000"
    assert summary["last_message"] == inbound_payload["content"]


def test_unassigned_agent_cannot_update_contact(
    client, inbound_payload, agent_headers
):
    create_attendance(client, inbound_payload)

    response = client.put(
        f"/api/v1/contacts/{inbound_payload['contact_id']}",
        json={"display_name": "Alteração indevida", "tags": []},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_supervisor_updates_unassigned_contact(
    client, inbound_payload, supervisor_headers
):
    create_attendance(client, inbound_payload)

    response = client.put(
        f"/api/v1/contacts/{inbound_payload['contact_id']}",
        json={"display_name": "Contato revisado", "tags": ["Novo"]},
        headers=supervisor_headers,
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Contato revisado"


def test_internal_notes_are_shared_and_protected(
    client, inbound_payload, agent_headers
):
    attendance = create_attendance(client, inbound_payload)
    client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )

    created = client.post(
        f"/api/v1/attendances/{attendance['id']}/notes",
        json={"content": "Cliente pediu retorno amanhã."},
        headers=agent_headers,
    )
    forbidden = client.post(
        f"/api/v1/attendances/{attendance['id']}/notes",
        json={"content": "Nota de outro agente"},
        headers=login_headers(client, "agente-2"),
    )

    assert created.status_code == 201
    assert forbidden.status_code == 403
    detail = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    ).json()
    assert detail["notes"][0]["content"] == "Cliente pediu retorno amanhã."
    assert detail["notes"][0]["actor_id"] == "agente-1"


def test_closure_requires_reason_and_updates_metrics(
    client, inbound_payload, agent_headers
):
    attendance = create_attendance(client, inbound_payload)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()
    client.post(
        f"/api/v1/attendances/{attendance['id']}/messages",
        json={"client_message_id": "metric-response", "content": "Resposta"},
        headers=agent_headers,
    )

    missing_reason = client.patch(
        f"/api/v1/attendances/{attendance['id']}/status",
        json={"status": "ENCERRADO", "expected_version": claimed["version"]},
        headers=agent_headers,
    )
    closed = client.patch(
        f"/api/v1/attendances/{attendance['id']}/status",
        json={
            "status": "ENCERRADO",
            "expected_version": claimed["version"],
            "closure_reason": "RESOLVIDO",
            "closure_note": "Solicitação concluída",
        },
        headers=agent_headers,
    )

    assert missing_reason.status_code == 422
    assert closed.status_code == 200
    detail = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    ).json()
    assert detail["closure"]["reason"] == "RESOLVIDO"
    assert detail["closure"]["note"] == "Solicitação concluída"

    metrics = client.get("/api/v1/metrics/summary", headers=agent_headers)
    assert metrics.status_code == 200
    assert metrics.json()["total_open"] == 0
    assert metrics.json()["closed_today"] == 1
    assert metrics.json()["average_first_response_seconds"] is not None
    assert metrics.json()["average_resolution_seconds"] is not None


def test_new_contact_defaults_to_unclassified_stage(client, inbound_payload, agent_headers):
    create_attendance(client, inbound_payload)

    contact = client.get(
        f"/api/v1/contacts/{inbound_payload['contact_id']}", headers=agent_headers
    ).json()

    assert contact["stage"] == "NAO_CLASSIFICADO"
    assert contact["last_attendance_id"] is not None
    assert contact["last_attendance_status"] == "AGUARDANDO"


def test_list_contacts_filters_by_stage_and_tag(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)
    client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )
    client.put(
        f"/api/v1/contacts/{inbound_payload['contact_id']}",
        json={"display_name": "Cliente Lead", "tags": ["prioridade"]},
        headers=agent_headers,
    )
    client.patch(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/stage",
        json={"stage": "CLIENTE_POTENCIAL"},
        headers=agent_headers,
    )

    all_contacts = client.get("/api/v1/contacts", headers=agent_headers).json()
    by_stage = client.get(
        "/api/v1/contacts?stage=CLIENTE_POTENCIAL", headers=agent_headers
    ).json()
    other_stage = client.get(
        "/api/v1/contacts?stage=INATIVO", headers=agent_headers
    ).json()
    by_tag = client.get(
        "/api/v1/contacts?tag=prioridade", headers=agent_headers
    ).json()
    missing_tag = client.get(
        "/api/v1/contacts?tag=nao-existe", headers=agent_headers
    ).json()

    assert len(all_contacts) == 1
    assert all_contacts[0]["stage"] == "CLIENTE_POTENCIAL"
    assert len(by_stage) == 1
    assert other_stage == []
    assert len(by_tag) == 1
    assert missing_tag == []


def test_unassigned_agent_cannot_update_contact_stage(client, inbound_payload, agent_headers):
    create_attendance(client, inbound_payload)

    response = client.patch(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/stage",
        json={"stage": "CLIENTE_ATIVO"},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_supervisor_updates_contact_stage_without_claim(
    client, inbound_payload, supervisor_headers
):
    create_attendance(client, inbound_payload)

    response = client.patch(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/stage",
        json={"stage": "CLIENTE_ATIVO"},
        headers=supervisor_headers,
    )

    assert response.status_code == 200
    assert response.json()["stage"] == "CLIENTE_ATIVO"


def test_contact_stage_update_requires_existing_contact(client, supervisor_headers):
    response = client.patch(
        "/api/v1/contacts/contato-inexistente/stage",
        json={"stage": "CLIENTE_ATIVO"},
        headers=supervisor_headers,
    )

    assert response.status_code == 404


def test_editing_contact_without_stage_keeps_previous_stage(
    client, inbound_payload, agent_headers
):
    attendance = create_attendance(client, inbound_payload)
    client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )
    client.patch(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/stage",
        json={"stage": "CLIENTE_ATIVO"},
        headers=agent_headers,
    )

    updated = client.put(
        f"/api/v1/contacts/{inbound_payload['contact_id']}",
        json={"display_name": "Só o nome mudou", "tags": []},
        headers=agent_headers,
    )

    assert updated.status_code == 200
    assert updated.json()["stage"] == "CLIENTE_ATIVO"


def test_new_attendance_has_no_tags_and_is_not_stale(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)

    assert attendance["tags"] == []
    assert attendance["stale"] is False

    listed = client.get("/api/v1/attendances", headers=agent_headers).json()[0]
    assert listed["tags"] == []
    assert listed["stale"] is False


def test_assigned_agent_sets_attendance_tags(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)
    client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )

    updated = client.put(
        f"/api/v1/attendances/{attendance['id']}/tags",
        json={"tags": ["Urgente", "financeiro", "urgente"]},
        headers=agent_headers,
    )
    detail = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    ).json()

    assert updated.status_code == 200
    assert updated.json()["tags"] == ["Urgente", "financeiro"]
    assert detail["tags"] == ["Urgente", "financeiro"]


def test_unassigned_agent_cannot_set_attendance_tags(client, inbound_payload, agent_headers):
    attendance = create_attendance(client, inbound_payload)

    response = client.put(
        f"/api/v1/attendances/{attendance['id']}/tags",
        json={"tags": ["indevido"]},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_quick_reply_crud_requires_supervisor(client, agent_headers, supervisor_headers):
    forbidden = client.post(
        "/api/v1/quick-replies",
        json={"title": "Saudação", "content": "Olá! Como posso ajudar?"},
        headers=agent_headers,
    )
    created = client.post(
        "/api/v1/quick-replies",
        json={"title": "Saudação", "content": "Olá! Como posso ajudar?"},
        headers=supervisor_headers,
    )
    listed = client.get("/api/v1/quick-replies", headers=agent_headers)
    forbidden_delete = client.delete(
        f"/api/v1/quick-replies/{created.json()['id']}", headers=agent_headers
    )
    deleted = client.delete(
        f"/api/v1/quick-replies/{created.json()['id']}", headers=supervisor_headers
    )

    assert forbidden.status_code == 403
    assert created.status_code == 201
    assert created.json()["title"] == "Saudação"
    assert len(listed.json()) == 1
    assert forbidden_delete.status_code == 403
    assert deleted.status_code == 204
    assert client.get("/api/v1/quick-replies", headers=agent_headers).json() == []


def test_outbox_list_and_retry_require_supervisor(client, agent_headers):
    listed = client.get("/api/v1/integrations/outbox", headers=agent_headers)
    retried = client.post(
        "/api/v1/integrations/outbox/qualquer-id/retry", headers=agent_headers
    )

    assert listed.status_code == 403
    assert retried.status_code == 403
