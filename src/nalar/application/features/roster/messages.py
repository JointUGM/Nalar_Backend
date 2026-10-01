from uuid import UUID

ROSTER_KIND = "roster_import"


def roster_message(import_id: UUID, job_id: UUID) -> dict[str, str]:
    return {"kind": ROSTER_KIND, "import_id": str(import_id), "job_id": str(job_id)}
