# Incremental/pagination regression tests for the snscrape CLI.
#
# These tests use in-memory fake scrapers only: they make no network access and
# exercise the CLI-level incremental semantics (deduplication, page-level --since
# termination, counting after commit, distinguishable failure states).
