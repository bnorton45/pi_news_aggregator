"""DB-writer service (PLAN §3.1/§3.3 inference/db-writer split, §8).

The ONLY pod in zone-process with database write credentials. It consumes
schema-validated EnrichedItem records from NATS (never raw attacker text) and writes
them to Postgres — so a runtime exploit in an inference worker (classifier, embedder,
NER, Ollama) reaches neither the DB nor the internet (PLAN §3.3). See main.py.
"""
