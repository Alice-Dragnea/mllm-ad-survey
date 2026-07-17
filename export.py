import duckdb

duckdb.connect('annotations.duckdb').execute("""
    COPY (
        SELECT i.filename, a.action_answer, a.reason_answer, a.submitted_at
        FROM annotations a
        JOIN images i ON i.id = a.image_id
        ORDER BY i.id
    ) TO 'annotations.csv' (HEADER, DELIMITER ',')
""")

print("Export complete: annotations.csv")