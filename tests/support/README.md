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

## Phase 5.1 TLS mode / Docker services

`tests/docker/compose.yaml` provides four IMAP services for
`@pytest.mark.docker` tests, reached through `tests.support.imap_integration.service(name)`:

| `service()` name | Port | Scenario |
| :--- | :--- | :--- |
| `greenmail` | 3993 (implicit TLS) | Regression, non-Dovecot server |
| `dovecot` | 3994 (implicit TLS) | Regression, existing behavior |
| `dovecot_starttls` | 3144 (plain, STARTTLS required) | `tls_mode="starttls"`; the shared `dovecot` service sets `disable_plaintext_auth = yes`, so bare `LOGIN`/`AUTHENTICATE` on this port is rejected until `STARTTLS` completes. Port 3994 on the same container is unaffected because it is already TLS-secured at the socket level. |
| `dovecot_logindisabled` | 3995 (implicit TLS) | A dedicated `dovecot-logindisabled` service sets `imap_capability = +LOGINDISABLED`, so `GenericImapFetcher` must fall back to `AUTHENTICATE PLAIN` |
| `dovecot_ca` | 3996 (implicit TLS) | A dedicated `dovecot-ca` service is issued a certificate signed by a throwaway test CA (instead of self-signed). `make_fetcher()` uses real certificate verification (no bypass) with `ca_cert_path` pointing at the CA certificate exported to `tests/docker/dovecot/ca/mail-dock-ca.crt` |

`service(name)` accepts overrides via environment variables:
`MAILDOCK_{NAME}_HOST`, `MAILDOCK_{NAME}_IMAPS_PORT`, `MAILDOCK_{NAME}_USERNAME`,
`MAILDOCK_{NAME}_PASSWORD`, and `MAILDOCK_{NAME}_CA_CERT_PATH` (only meaningful
for `dovecot_ca`), where `{NAME}` is the upper-cased service name (e.g.
`MAILDOCK_DOVECOT_CA_CERT_PATH`).

`make_fetcher(settings)` builds a real `GenericImapFetcher` for a given
`ImapService`: when `ca_cert_path` is set it performs genuine certificate
verification; otherwise it uses `insecure_ssl_context()` to bypass validation
of the self-signed regression certificates, matching prior behavior.

Bring the services up before running `@pytest.mark.docker` tests:

```sh
docker compose -f tests/docker/compose.yaml up -d --wait
MAILDOCK_DOCKER=1 uv run pytest -m docker
docker compose -f tests/docker/compose.yaml down -v
```

Note: `tests/docker/dovecot/ca/` receives a file written by the `dovecot-ca`
container (running as root), so the exported certificate may end up owned by
`root` on the host. This is expected; the file is world-readable and tests
only need to read it, not write it. If the bind mount fails to be created
automatically, create the directory once with `mkdir -p tests/docker/dovecot/ca`
before the first `docker compose up`.

