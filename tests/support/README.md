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
