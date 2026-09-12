"""El runner de jobs: un loop in-process.

# Por que no hay Celery, ni ARQ, ni Redis

El volumen es de ~20 documentos por mes entre cinco usuarios. Un broker seria mas
infraestructura que trabajo. Lo que si se hace desde el dia uno es tomar los jobs
con `for update skip locked` (dentro de `app.claim_next_job`), porque es lo unico
que despues hace trivial escalar: correr `python -m app.worker` como proceso
aparte no requiere cambiar una linea de logica, dos runners nunca se llevan el
mismo job.

# Dos disparadores

- **El aviso directo**: la ruta que encola llama a `wake()` y el loop arranca al
  instante. Es lo que hace que subir un PDF se sienta inmediato.
- **El polling cada `JOB_POLL_SECONDS`**: la red de seguridad. Cubre los jobs que
  encolo otro proceso, los que el reaper devolvio a la cola y los reintentos con
  backoff, que estan agendados en el futuro y nadie va a avisar.

# Sobre el apagado

`stop()` espera a que terminen los jobs en curso, con un limite. Los que no
lleguen quedan en `running` y los recicla el reaper: es preferible un job que se
repite a uno que se pierde, porque los handlers son idempotentes (el upsert de
`document_texts` pisa la fila, no la duplica).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import Final

from app.infra import db
from app.jobs import handlers, reaper
from app.logging_config import get_logger
from app.repositories import documents as documents_repo
from app.repositories import jobs as jobs_repo
from app.settings import Settings

log = get_logger(__name__)

# Cada cuantos ciclos del loop se pasa el reaper. Con poll de 5 s son ~2 minutos.
REAP_EVERY_CYCLES: Final = 24

# Cuanto se espera a los jobs en curso al apagar.
SHUTDOWN_GRACE_SECONDS: Final = 20.0


def worker_name() -> str:
    """Queda en `locked_by`. Con varias maquinas, dice cual lo tomo."""
    return f"{socket.gethostname()}:{os.getpid()}"


class JobRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._name = worker_name()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(settings.job_concurrency)
        self._stopping = False

    # ─── Ciclo de vida ──────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="job-runner")
        log.info("runner de jobs iniciado", worker=self._name)

    async def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._running:
            log.info("esperando a los jobs en curso", count=len(self._running))
            _done, pending = await asyncio.wait(self._running, timeout=SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                # Quedan en `running` y los devuelve a la cola el reaper.
                log.warning("jobs cortados por el apagado", count=len(pending))

        log.info("runner de jobs detenido")

    def wake(self) -> None:
        """Aviso de que hay trabajo nuevo. Se llama desde la ruta que encola."""
        self._wakeup.set()

    # ─── Loop ───────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        cycles = 0
        while not self._stopping:
            try:
                if cycles % REAP_EVERY_CYCLES == 0:
                    await reaper.reap_once(self._settings)
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # El loop no se puede morir: si se muere, la cola deja de
                # avanzar y nada lo avisa hasta que un usuario mira la UI.
                log.error("error en el loop del runner", error=type(exc).__name__, detail=str(exc))

            cycles += 1
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._settings.job_poll_seconds)

    async def run_once(self) -> bool:
        """Toma y ejecuta un job, esperando a que termine. Devuelve si hubo uno.

        Es el mismo camino que usa el loop, sin el loop: lo usan los tests y
        serviria para un worker de una sola pasada.
        """
        async with db.runtime_tx() as conn:
            job = await jobs_repo.claim_next(conn, self._name)

        if job is None:
            return False

        await self._run(job)
        return True

    async def _drain(self) -> None:
        """Toma jobs mientras haya y quede capacidad."""
        while not self._stopping:
            if len(self._running) >= self._settings.job_concurrency:
                return

            async with db.runtime_tx() as conn:
                job = await jobs_repo.claim_next(conn, self._name)

            if job is None:
                return

            task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _run(self, job: jobs_repo.ClaimedJob) -> None:
        async with self._semaphore:
            handler = handlers.HANDLERS.get(job.job_type)
            if handler is None:
                # Un tipo de job sin handler es un error de programacion, no un
                # fallo transitorio: reintentarlo no lo va a hacer aparecer.
                await self._fail(job, f"no hay handler para {job.job_type}", permanent=True)
                return

            log.info("job tomado", job_id=job.id, job_type=job.job_type, attempt=job.attempts)
            try:
                result = await handler(job)
            except handlers.PermanentJobError as exc:
                await self._fail(job, exc.message, permanent=True, reason=exc.reason)
            except asyncio.CancelledError:
                # Apagado: se deja en `running` a proposito para que el reaper lo
                # devuelva a la cola.
                raise
            except Exception as exc:
                await self._fail(job, f"{type(exc).__name__}: {exc}")
            else:
                async with db.system_tx(job.user_id) as conn:
                    await jobs_repo.mark_succeeded(conn, job.id, result)
                log.info("job terminado", job_id=job.id, job_type=job.job_type)

    async def _fail(
        self,
        job: jobs_repo.ClaimedJob,
        error: str,
        *,
        permanent: bool = False,
        reason: str | None = None,
    ) -> None:
        async with db.system_tx(job.user_id) as conn:
            status = await jobs_repo.mark_failed(conn, job.id, error, permanent=permanent)

            # El documento solo se marca fallido cuando ya no hay reintentos: si
            # no, la UI mostraria "fallo" y un minuto despues "listo".
            if status == "failed" and job.document_id:
                await documents_repo.set_status(
                    conn, job.document_id, "failed", failure_reason=reason or "job_failed"
                )

        log.warning(
            "job fallado", job_id=job.id, job_type=job.job_type, status=status, detail=error
        )


_runner: JobRunner | None = None


def get_runner() -> JobRunner | None:
    """El runner del proceso, si el lifespan lo arranco.

    Devuelve `None` en los tests que instancian la app sin lifespan: encolar
    sigue funcionando y el job simplemente espera a que haya un runner.
    """
    return _runner


async def start_runner(settings: Settings) -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner(settings)
        await _runner.start()
    return _runner


async def stop_runner() -> None:
    global _runner
    if _runner is not None:
        await _runner.stop()
        _runner = None
