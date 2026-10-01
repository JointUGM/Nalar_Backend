from uuid import UUID

FINALIZE_KIND = "publication_finalize"


def finalize_message(publication_id: UUID, job_id: UUID) -> dict[str, str]:
    return {"kind": FINALIZE_KIND, "publication_id": str(publication_id), "job_id": str(job_id)}
