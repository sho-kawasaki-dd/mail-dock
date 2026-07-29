# Test support

Reusable test helpers live here. The Dovecot UIDVALIDITY helper is intended for
integration tests that can access the container's Maildir through a bind mount
or `docker compose exec`:

```python
from tests.support.dovecot_uidvalidity import force_uidvalidity_change_in_container

force_uidvalidity_change_in_container(Path("tests/docker/compose.yaml"))
```

The next mailbox open creates a new UIDVALIDITY generation while preserving the
message files. For a bind-mounted Maildir, `force_uidvalidity_change()` can be
used directly without Docker.

The Phase 1 A-2 helpers are also available here:

```python
from tests.support.eml_builder import AttachmentSpec, build_eml, write_corpus
from tests.support.fake_fetcher import FakeFetcher
from tests.support.in_memory_repository import InMemoryMessageRepository

raw = build_eml(attachments=[AttachmentSpec("sample.txt", b"sample")])
fetcher = FakeFetcher(eml_bytes={("INBOX", 1): raw})
repository = InMemoryMessageRepository()
write_corpus(tmp_path / "eml")
```

`FakeFetcher` supports deterministic UID ordering, cancellation, header-only
downloads, and injected transient or permanent failures. `write_corpus()`
creates the generated fixtures in a temporary directory; checked-in fixtures
and their expected cases are documented in `tests/fixtures/eml/README.md`.
