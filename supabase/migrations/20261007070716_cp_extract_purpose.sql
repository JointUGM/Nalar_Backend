-- AI-6: CP draft extraction calls are costed under their own purpose label.
-- The label lives in its own file because a new enum value cannot be used in the transaction that adds it.
alter type ai_purpose add value if not exists 'cp_extract';
