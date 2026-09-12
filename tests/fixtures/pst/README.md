# PST integration fixtures

The Phase 4.5 integration tests use real PST files but do not commit personal
mail archives to the repository. Provide a small test archive through the
`MAILDOCK_PST_FIXTURE` environment variable:

```powershell
$env:MAILDOCK_PST_FIXTURE = 'C:\pstpoc\sample.pst'
uv run pytest -m pst tests/integration/test_pst_import.py tests/integration/test_pst_reindex.py
```

The fixture should contain Japanese folder/message text, at least one
attachment, a nested folder hierarchy, and enough messages to exercise more
than one Stage B batch. The tests also accept the conventional local path
`tests/fixtures/pst/sample.pst`; absent fixtures are skipped.

Set `MAILDOCK_PST_HOSTILE` to an Outlook-generated fixture from
`tools/pst_poc/make_hostile_pst.ps1` to run the Windows folder-name and staging
containment check. That fixture intentionally contains names which make
readpst fail on Windows; the test verifies the dedicated converter error and
that any partial output remains below staging.

The readpst executable and its runtime files must be present under
`vendor/readpst/`. Tests skip when the bundled converter cannot be executed.