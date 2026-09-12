"""Recuperacion de jobs huerfanos.

Un job queda en `running` para siempre si el proceso que lo tomo muere: un
redeploy de Fly, un OOM, un `kill -9`. Sin esto, el documento se queda en
"procesando" en la UI y no hay forma de destrabarlo — ni reintentando, porque el
indice `jobs_active_uq` impide encolar otro igual mientras el viejo siga activo.

El criterio es el tiempo desde `locked_at`: si un job lleva mas de
`JOB_STALE_MINUTES` tomado, quien lo tenia ya no existe. Los que todavia tienen
intentos vuelven a `queued`; los que los agotaron pasan a `failed`, porque
reencolar indefinidamente algo que falla siempre es un bucle, no una
recuperacion.

La logica vive en `app.reap_stale_jobs`, una funcion `security definer`: es una
de las dos unicas operaciones del sistema que cruzan usuarios (ver la migracion
`job_dispatch`).
"""

from __future__ import annotations

from app.infra import db
from app.logging_config import get_logger
from app.repositories import jobs as jobs_repo
from app.settings import Settings

log = get_logger(__name__)


async def reap_once(settings: Settings) -> int:
    """Recicla los jobs vencidos. Devuelve cuantos toco."""
    async with db.runtime_tx() as conn:
        reaped = await jobs_repo.reap_stale(conn, settings.job_stale_minutes)

    if reaped:
        log.warning("jobs huerfanos reciclados", count=reaped)
    return reaped
