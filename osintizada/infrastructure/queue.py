"""Fila persistente de jobs (RQ sobre Redis).

Por que RQ (e não Celery/Dramatiq):
  * o projeto já usa Redis e só precisa de "entregar um job_id a um worker": RQ faz isso com
    uma dependência pequena, sem broker extra nem configuração de exchanges/rotas;
  * serialização JSON (``JSONSerializer``) — nada de pickle na fila;
  * workers separados (``osintizada worker``) e monitoramento pronto (``rq info``, registries);
  * testável em processo (``SimpleWorker`` + ``fakeredis``).
Limitações cobertas pelo desenho: o RQ não reentrega sozinho um job cujo worker morreu —
quem garante isso é o banco (heartbeat) + ``JobRecoveryService``. O retry de JOB também é
nosso (banco), não do RQ: o histórico de tentativas fica no banco.

A fila só transporta o id do Job. Estado, tentativas e resultado vivem no banco.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

TASK_PATH = "osintizada.jobs.worker.execute_job"


class JobQueue(ABC):
    name: str

    @abstractmethod
    def enqueue(self, job_id: str) -> None: ...

    @abstractmethod
    def exists(self, job_id: str) -> bool:
        """Há mensagem pendente/em execução para este Job na fila?"""

    @abstractmethod
    def size(self) -> int: ...


class RQJobQueue(JobQueue):
    def __init__(self, redis, name: str = "osintizada", job_timeout: int = 4 * 3600) -> None:
        from rq import Queue
        from rq.serializers import JSONSerializer

        self.redis = redis
        self.name = name
        self.queue = Queue(name, connection=redis, serializer=JSONSerializer, default_timeout=job_timeout)

    @staticmethod
    def message_id(job_id: str) -> str:
        return f"osz-{job_id}"

    def enqueue(self, job_id: str) -> None:
        self.queue.enqueue(TASK_PATH, job_id, job_id=self.message_id(job_id), result_ttl=86400,
                           failure_ttl=7 * 86400, description=f"OSINTIZADA job {job_id}")

    def exists(self, job_id: str) -> bool:
        from rq.job import Job, JobStatus
        from rq.serializers import JSONSerializer

        try:
            job = Job.fetch(self.message_id(job_id), connection=self.redis, serializer=JSONSerializer)
        except Exception:  # noqa: BLE001 - NoSuchJobError / Redis sem o hash
            return False
        return job.get_status(refresh=False) in (JobStatus.QUEUED, JobStatus.STARTED, JobStatus.DEFERRED,
                                                 JobStatus.SCHEDULED)

    def size(self) -> int:
        return len(self.queue)
