from datetime import timedelta
from enum import Enum


class AttendanceStatus(str, Enum):
    AGUARDANDO = "AGUARDANDO"
    EM_ATENDIMENTO = "EM_ATENDIMENTO"
    AGUARDANDO_CLIENTE = "AGUARDANDO_CLIENTE"
    AGUARDANDO_INTERNO = "AGUARDANDO_INTERNO"
    ENCERRADO = "ENCERRADO"


class ActorRole(str, Enum):
    ATENDENTE = "ATENDENTE"
    SUPERVISOR = "SUPERVISOR"
    ADMIN = "ADMIN"


class MessageDirection(str, Enum):
    ENTRADA = "ENTRADA"
    SAIDA = "SAIDA"


class SenderType(str, Enum):
    CLIENTE = "CLIENTE"
    ATENDENTE = "ATENDENTE"
    IA = "IA"
    BOT = "BOT"
    SISTEMA = "SISTEMA"


class DeliveryStatus(str, Enum):
    RECEBIDA = "RECEBIDA"
    PENDENTE = "PENDENTE"
    ENVIADA_AO_MIDDLEWARE = "ENVIADA_AO_MIDDLEWARE"
    ACEITA_PELO_PROVEDOR = "ACEITA_PELO_PROVEDOR"
    ENTREGUE = "ENTREGUE"
    LIDA = "LIDA"
    FALHA = "FALHA"


class EventType(str, Enum):
    CRIADO = "CRIADO"
    MENSAGEM_RECEBIDA = "MENSAGEM_RECEBIDA"
    MENSAGEM_ENVIADA = "MENSAGEM_ENVIADA"
    ASSUMIDO = "ASSUMIDO"
    STATUS_ALTERADO = "STATUS_ALTERADO"
    TRANSFERIDO = "TRANSFERIDO"


class ClosureReason(str, Enum):
    RESOLVIDO = "RESOLVIDO"
    SEM_RETORNO = "SEM_RETORNO"
    DUPLICADO = "DUPLICADO"
    FORA_ESCOPO = "FORA_ESCOPO"
    OUTRO = "OUTRO"


class ContactStage(str, Enum):
    NAO_CLASSIFICADO = "NAO_CLASSIFICADO"
    NOVO_CONTATO = "NOVO_CONTATO"
    CLIENTE_POTENCIAL = "CLIENTE_POTENCIAL"
    CLIENTE_ATIVO = "CLIENTE_ATIVO"
    INATIVO = "INATIVO"


ACTIVE_STATUSES = (
    AttendanceStatus.AGUARDANDO,
    AttendanceStatus.EM_ATENDIMENTO,
    AttendanceStatus.AGUARDANDO_CLIENTE,
    AttendanceStatus.AGUARDANDO_INTERNO,
)


STALE_THRESHOLDS = {
    AttendanceStatus.AGUARDANDO: timedelta(minutes=15),
    AttendanceStatus.EM_ATENDIMENTO: timedelta(hours=4),
    AttendanceStatus.AGUARDANDO_CLIENTE: timedelta(hours=24),
    AttendanceStatus.AGUARDANDO_INTERNO: timedelta(hours=24),
}


ALLOWED_STATUS_TRANSITIONS = {
    AttendanceStatus.EM_ATENDIMENTO: {
        AttendanceStatus.AGUARDANDO_CLIENTE,
        AttendanceStatus.AGUARDANDO_INTERNO,
        AttendanceStatus.ENCERRADO,
    },
    AttendanceStatus.AGUARDANDO_CLIENTE: {AttendanceStatus.EM_ATENDIMENTO},
    AttendanceStatus.AGUARDANDO_INTERNO: {AttendanceStatus.EM_ATENDIMENTO},
}
