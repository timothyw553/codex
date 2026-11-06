# Unity Catalog context

Use these lessons to guide investigation. The current public contracts and code determine
the required behavior. Historical review comments can describe an earlier design.

- Check the actual wire response and persisted state. A passing helper test does not prove
  that the server stores or returns the right value.
- Authorize the same resource that the request changes. Check both URL and body targets.
  Keep metadata permissions separate from data credentials.
- Keep the native, Delta, and Iceberg API contracts distinct. Their status codes and retry
  rules can differ on purpose. See [PR 1797](https://github.com/unitycatalog/unitycatalog/pull/1797)
  and [PR 1828](https://github.com/unitycatalog/unitycatalog/pull/1828).
- Test changes against existing databases. Do not assume automatic schema updates widen
  existing columns. Preserve stored names when renaming Java fields.
  See [PR 1824](https://github.com/unitycatalog/unitycatalog/pull/1824).
- Read dependency implementations before wrapping them. Check connection ownership,
  autocommit, lazy streams, expiry checks, and disposal. Method names do not establish those
  contracts. See [PR 1863](https://github.com/unitycatalog/unitycatalog/pull/1863),
  [PR 1865](https://github.com/unitycatalog/unitycatalog/pull/1865), and
  [PR 2007](https://github.com/unitycatalog/unitycatalog/pull/2007).
- Test cancellation, shutdown order, stale workers, and independent concurrent requests
  where relevant. State what a timeout actually bounds. A cooperative cancellation budget
  is not a hard deadline. See [PR 1870](https://github.com/unitycatalog/unitycatalog/pull/1870)
  and [PR 1918](https://github.com/unitycatalog/unitycatalog/pull/1918).
- Use real dependencies when their behavior is the claim. Test actual client construction,
  database writes, and credential expiry. Report environmental limits and skipped tests.
  See [PR 1886](https://github.com/unitycatalog/unitycatalog/pull/1886) and
  [PR 1963](https://github.com/unitycatalog/unitycatalog/pull/1963).
- Keep test expectations independent of the code under test. Check that an assertion fails
  when the behavior is broken. Broad exception handlers can hide that failure.
  See [PR 1967](https://github.com/unitycatalog/unitycatalog/pull/1967).
- Fix a shared cause once. Reuse fixtures and existing lifecycle tests. Keep unrelated
  refactors and extra configuration out of a focused behavior change.
- Check the supported Spark, Delta, database, and Python versions that the change affects.
  State which combinations actually ran.
- Write the PR description for the final patch. Follow the current template and title
  convention. Give the problem, a concrete example, the fix, compatibility, and actual tests.

These notes come from reading 150 merged PRs ending at commit
`d31fa8b4725156a0f08d0e8f15604ac51a714621`. They are examples, not additional requirements.
