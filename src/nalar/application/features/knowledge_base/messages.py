from uuid import UUID

DETECT_KIND = "kb_detect_sections"
BUILD_KIND = "kb_build_section"


def detect_message(material_id: UUID, job_id: UUID) -> dict[str, str]:
    return {"kind": DETECT_KIND, "material_id": str(material_id), "job_id": str(job_id)}


def build_message(section_id: UUID, job_id: UUID) -> dict[str, str]:
    return {"kind": BUILD_KIND, "section_id": str(section_id), "job_id": str(job_id)}
