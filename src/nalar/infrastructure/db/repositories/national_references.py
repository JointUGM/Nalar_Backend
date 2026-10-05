import json
from uuid import UUID

import asyncpg

from nalar.application.errors import Conflict
from nalar.application.ports.national_references import ReferenceRow
from nalar.infrastructure.db.pool import DbConnection


class PgNationalReferencesRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def insert(self, values: ReferenceRow) -> None:
        try:
            await self._conn.execute(
                "insert into national_reference_documents"
                " (id,kind,title,issuer,source_url,sha256,storage_path,uploaded_by)"
                " values($1,$2,$3,$4,$5,$6,$7,$8)",
                *(
                    values[k]
                    for k in (
                        "id",
                        "kind",
                        "title",
                        "issuer",
                        "source_url",
                        "sha256",
                        "storage_path",
                        "uploaded_by",
                    )
                ),
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("REFERENCE_EXISTS") from exc

    async def get(self, document_id: UUID, *, lock: bool = False) -> ReferenceRow | None:
        row = await self._conn.fetchrow(
            "select * from national_reference_documents where id=$1"
            + (" for update" if lock else ""),
            document_id,
        )
        if row is None:
            return None
        return dict(row) | {"review": json.loads(row["review"]) if row["review"] else None}

    async def documents(self, *, published_only: bool) -> list[ReferenceRow]:
        rows = await self._conn.fetch(
            "select id,kind,title,issuer,source_url,sha256,status,revision,created_at,"
            " published_at,curriculum_version_id,job_id,error_code"
            " from national_reference_documents"
            " where (not $1::boolean or status='published') order by created_at desc,id limit 100",
            published_only,
        )
        return [dict(row) for row in rows]

    async def queue(self, document_id: UUID, job_id: UUID, status: str) -> None:
        await self._conn.execute(
            "update national_reference_documents set job_id=$2,status=$3,lease_token=null,"
            " lease_until=null,error_code=null where id=$1 and status <> 'published'",
            document_id,
            job_id,
            status,
        )

    async def claim(self, document_id: UUID, job_id: UUID, token: UUID, lease_s: int) -> bool:
        return (
            await self._conn.fetchval(
                "update national_reference_documents set lease_token=$3,"
                " lease_until=now()+make_interval(secs=>$4) where id=$1 and job_id=$2"
                " and status in ('extracting','indexing')"
                " and (lease_until is null or lease_until <= now()) returning id",
                document_id,
                job_id,
                token,
                lease_s,
            )
            is not None
        )

    async def pages(self, document_id: UUID) -> dict[int, str]:
        rows = await self._conn.fetch(
            "select page_number,text from national_reference_pages where document_id=$1"
            " order by page_number",
            document_id,
        )
        return {row["page_number"]: row["text"] for row in rows}

    async def extracted(self, document_id: UUID, token: UUID, pages: dict[int, str]) -> bool:
        updated = await self._conn.fetchval(
            "update national_reference_documents set status='review',revision=revision+1,"
            " lease_token=null,lease_until=null where id=$1 and lease_token=$2"
            " and status='extracting' and lease_until > now() returning id",
            document_id,
            token,
        )
        if updated is None:
            return False
        await self._conn.execute(
            "delete from national_reference_pages where document_id=$1", document_id
        )
        await self._conn.executemany(
            "insert into national_reference_pages(document_id,page_number,text) values($1,$2,$3)",
            [(document_id, number, text) for number, text in pages.items()],
        )
        return True

    async def review(self, document_id: UUID, revision: int, draft: ReferenceRow) -> int:
        updated: int | None = await self._conn.fetchval(
            "update national_reference_documents set review=$3::jsonb,revision=revision+1,"
            " status='review',error_code=null,job_id=null,lease_token=null,lease_until=null"
            " where id=$1 and revision=$2 and status in ('review','failed') returning revision",
            document_id,
            revision,
            json.dumps(draft),
        )
        if updated is None:
            raise Conflict("REFERENCE_REVISION_CONFLICT")
        return updated

    async def publish(
        self,
        document_id: UUID,
        token: UUID,
        actor_id: UUID,
        vectors: list[list[float]],
        model: str,
    ) -> bool:
        row = await self.get(document_id, lock=True)
        if (
            row is None
            or row["lease_token"] != token
            or row["status"] != "indexing"
            or not await self._conn.fetchval(
                "select lease_until > now() from national_reference_documents where id=$1",
                document_id,
            )
        ):
            return False
        version_id = None
        if row["kind"] == "curriculum":
            draft = row["review"]["curriculum"]
            await self._conn.execute(
                "select pg_advisory_xact_lock(hashtextextended('cp-current',0))"
            )
            try:
                if draft["is_current"]:
                    await self._conn.execute(
                        "update cp_versions set is_current=false where is_current"
                    )
                version_id = await self._conn.fetchval(
                    "insert into cp_versions"
                    " (title,decree_code,effective_on,status,published_at,is_current)"
                    " values($1,$2,$3::text::date,'published',now(),$4) returning id",
                    draft["name"],
                    draft["decree_code"],
                    draft["effective_on"],
                    draft["is_current"],
                )
                vector_index = 0
                for subject in draft["subjects"]:
                    subject_id = await self._conn.fetchval(
                        "insert into cp_subjects(cp_version_id,name,phase)"
                        " values($1,$2,$3) returning id",
                        version_id,
                        subject["name"],
                        subject["phase"],
                    )
                    for ordinal, element in enumerate(subject["elements"]):
                        parent_id = await self._conn.fetchval(
                            "insert into cp_learning_outcomes"
                            " (cp_subject_id,element,description,ordinal,"
                            " grain,source_document_id,source_page_start,source_page_end)"
                            " values($1,$2,$3,$4,'element',$5,$6,$7) returning id",
                            subject_id,
                            element["element"],
                            element["description"],
                            ordinal,
                            document_id,
                            element["page_start"],
                            element["page_end"],
                        )
                        for statement in element["statements"]:
                            await self._conn.execute(
                                "insert into cp_learning_outcomes"
                                " (cp_subject_id,element,description,grain,"
                                " parent_id,embedding,embedding_model,source_document_id,"
                                " source_page_start,"
                                " source_page_end) values($1,$2,$3,'statement',$4,$5,$6,$7,$8,$9)",
                                subject_id,
                                element["element"],
                                statement["description"],
                                parent_id,
                                vectors[vector_index],
                                model,
                                document_id,
                                statement["page_start"],
                                statement["page_end"],
                            )
                            vector_index += 1
            except asyncpg.UniqueViolationError as exc:
                raise Conflict("CURRICULUM_EXISTS") from exc
        await self._conn.execute(
            "update national_reference_documents set status='published',reviewed_by=$3,"
            " reviewed_at=now(),published_at=now(),curriculum_version_id=$4,lease_token=null,"
            " lease_until=null,error_code=null where id=$1 and lease_token=$2",
            document_id,
            token,
            actor_id,
            version_id,
        )
        return True

    async def release(self, document_id: UUID, token: UUID, error_code: str | None) -> bool:
        return (
            await self._conn.fetchval(
                "update national_reference_documents"
                " set lease_token=null,lease_until=null,error_code=$3,"
                " status=case when $3::text is null then status else 'failed' end"
                " where id=$1 and lease_token=$2 and status <> 'published' returning id",
                document_id,
                token,
                error_code,
            )
            is not None
        )

    async def adoption(self, kb_id: UUID, document_id: UUID) -> ReferenceRow | None:
        await self._conn.execute(
            "select pg_advisory_xact_lock(hashtextextended($1,0))",
            f"reference:{kb_id}:{document_id}",
        )
        row = await self._conn.fetchrow(
            "select m.id as material_id,m.knowledge_base_id,j.id as job_id"
            " from teaching_materials m join jobs j on j.entity_id=m.id"
            " and j.kind='kb_detect_sections'"
            " where m.knowledge_base_id=$1 and m.national_reference_id=$2"
            " order by j.created_at desc limit 1",
            kb_id,
            document_id,
        )
        return dict(row) if row else None

    async def attach(self, material_id: UUID, document_id: UUID, pages: list[int]) -> None:
        await self._conn.execute(
            "update teaching_materials set national_reference_id=$2,national_source_pages=$3"
            " where id=$1",
            material_id,
            document_id,
            pages,
        )
