import csv
from io import StringIO
from typing import Any

from fastapi import Response


def csv_response(filename: str, fields: list[str], rows: list[dict[str, Any]]) -> Response:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        values = []
        for field in fields:
            value = row.get(field)
            text = "" if value is None else str(value)
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(
                ("\t", "\r", "\n")
            ):
                text = "'" + text
            values.append(text)
        writer.writerow(values)
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
